"""Frequency-only separation: one fixed EQ curve per stem, learned from the train stems ("frequency-only Wiener").

Final algorithm:
  1. STFT of each channel, n_fft 16384 (0.37 s Hann window, 2.7 Hz bins), hop 4096.
  2. Every bin f gets the same gain for the whole song: vocals/drums/other = mask_s(f) * X(f, t).
     mask_s(f) = sum_c w_c Re(S_c,s X_c*)(f) / sum_c w_c |X_c|^2(f) over the 40 MUSDB train clips, i.e. the
     average share of stem s at that frequency (w_c = 1 / clip mix energy, so every clip counts equally),
     clipped to [0, 1] and renormalised so the three stems add back to the mix. Built by
     freq_bands_fit.py into freq_bands_stats.npz (3 x 8193 numbers); this is the least-squares-best
     time-invariant EQ, the ceiling of the "judge by frequency" idea.
  3. Inverse STFT. Same curve for left and right, no timing or stereo information is used.
Because the bins are fine, the curve also learned a comb on the A=440 semitone grid (bins on in-tune note
frequencies lean to "other"), worth ~0.1-0.15 dB over a smooth 1/12-octave curve.

Variants (train median SDR mean, evaluate.py): hand hard bands -0.26, hand soft bands 2.55, tuned hard edges
1.28, learned smooth 1/12-oct 2.95, learned per-bin 4096 2.99, this 3.16 (leave-one-out estimate ~2.8),
per-bin 32768 3.21 (same leave-one-out). Song-adaptive EQs (template gains, nearest-neighbour songs, peak
contrast, tuning-shifted comb) did not beat it in leave-one-out tests.
"""
from pathlib import Path

import numpy as np
import librosa

STATS = Path(__file__).resolve().parent / "freq_bands_stats.npz"
FIT_SR = 44100
STEMS = ("vocals", "drums", "other")
_CACHE = {}


def _mask(sr):
    if sr not in _CACHE:
        z = np.load(STATS)
        mask, n_fft = z["mask"].astype(np.float32), int(z["n_fft"])
        if sr != FIT_SR:  # same curve in Hz on the new bin grid
            f_fit = librosa.fft_frequencies(sr=FIT_SR, n_fft=n_fft)
            f_new = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
            mask = np.stack([np.interp(f_new, f_fit, m) for m in mask]).astype(np.float32)
        _CACHE[sr] = (mask, n_fft)
    return _CACHE[sr]


def separate(mix, sr):
    mask, n_fft = _mask(sr)
    hop = n_fft // 4
    n = mix.shape[1]
    X = librosa.stft(np.asarray(mix, np.float32), n_fft=n_fft, hop_length=hop)   # (2, F, T)
    return {s: librosa.istft(X * mask[i][None, :, None], hop_length=hop, length=n).astype(np.float32)
            for i, s in enumerate(STEMS)}
