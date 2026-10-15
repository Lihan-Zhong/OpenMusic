"""Learn the frequency-only Wiener EQ for experiments/methods/freq_bands.py from the TRAIN stems.

For every STFT frequency bin f and stem s it measures, on the 40 MUSDB train clips (data/gt/train only),

    B_c,s(f) = sum over frames and channels of Re(S X*)   (stem-vs-mix cross power of clip c)
    C_c(f)   = sum over frames and channels of |X|^2      (mix power of clip c)

and stores the time-invariant gain  mask_s(f) = sum_c w_c B_c,s(f) / sum_c w_c C_c(f),  clipped to [0, 1]
and renormalised so the three stems add back to the mix. With w_c = 1 / (total mix energy of clip c) every
clip counts equally; since sum_s S = X this is, up to the stem-cross terms, E|S|^2 / E|X|^2: the least-squares
optimal fixed EQ per stem (the "frequency-only Wiener filter").

    .venv/bin/python experiments/methods/freq_bands_fit.py                  # writes freq_bands_stats.npz
    .venv/bin/python experiments/methods/freq_bands_fit.py --n-fft 4096 --bpo 12 --out outputs/freq_bands/scratch/x.npz
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import librosa

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from common import STEMS, load_track, tracks  # noqa: E402


def collect(n_fft):
    hop = n_fft // 4
    Bs, Cs = [], []
    for t in tracks("train"):
        mix, refs = load_track(t)
        X = librosa.stft(mix, n_fft=n_fft, hop_length=hop)
        Cs.append((np.abs(X) ** 2).sum(axis=(0, 2)))
        Bs.append([np.real(librosa.stft(refs[s], n_fft=n_fft, hop_length=hop) * np.conj(X)).sum(axis=(0, 2))
                   for s in STEMS])
    return np.array(Bs), np.array(Cs)          # (clips, 3, F), (clips, F)


def band_basis(freqs, bpo):
    """Triangular (hat) bands spaced 1/bpo octave between 30 Hz and 16 kHz, shape (F, bands)."""
    lf = np.log2(np.clip(freqs, 30.0, 16000.0) / 30.0) * bpo
    i0 = np.floor(lf).astype(int)
    fr = lf - i0
    nb = int(np.ceil(lf.max())) + 1
    i1 = np.minimum(i0 + 1, nb - 1)
    Phi = np.zeros((len(freqs), nb))
    Phi[np.arange(len(freqs)), i0] += 1 - fr
    Phi[np.arange(len(freqs)), i1] += fr
    return Phi


def fit(Bs, Cs, n_fft, weight="clip", bpo=0):
    w = 1.0 / Cs.sum(-1) if weight == "clip" else np.ones(len(Cs))
    b = (w[:, None, None] * Bs).sum(0)          # (3, F)
    c = (w[:, None] * Cs).sum(0)                # (F,)
    if bpo:  # smooth EQ: weighted least squares on a 1/bpo-octave triangular basis
        Phi = band_basis(librosa.fft_frequencies(sr=44100, n_fft=n_fft), bpo)
        G = Phi.T @ (c[:, None] * Phi)
        G += 1e-6 * np.trace(G) / len(G) * np.eye(len(G))
        mask = (Phi @ np.linalg.solve(G, Phi.T @ b.T)).T
    else:
        mask = b / np.maximum(c, 1e-20)
    mask = np.clip(mask, 0.0, 1.0)
    mask /= np.maximum(mask.sum(0, keepdims=True), 1e-9)
    return mask.astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-fft", type=int, default=16384)
    ap.add_argument("--weight", default="clip", choices=["clip", "pool"])
    ap.add_argument("--bpo", type=int, default=0, help="0 = one gain per FFT bin; else smooth, bands per octave")
    ap.add_argument("--out", default=str(HERE / "freq_bands_stats.npz"))
    a = ap.parse_args()
    Bs, Cs = collect(a.n_fft)
    mask = fit(Bs, Cs, a.n_fft, a.weight, a.bpo)
    np.savez(a.out, mask=mask, n_fft=a.n_fft, stems=np.array(STEMS))
    print("wrote", a.out, mask.shape)


if __name__ == "__main__":
    main()
