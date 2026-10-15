"""Supervised NMF separation with learned per-stem spectral templates (dictionaries).

Final algorithm (templates learned from the 40 TRAIN stems by nmf_fit.py -> nmf_stats.npz):
 1. STFT 4096 / hop 1024. Split the stereo signal into 4 "views" that add back up to the mix:
    mid and side, each cut into a percussive and a harmonic part by median-filter HPSS soft masks
    (11 frames x 47 bins, power 2; the side reuses its own HPSS masks).
 2. Each frame's feature vector = the 4 view magnitudes, compressed to 280 bands (linear bins below 1.5 kHz,
    36 bands/octave above), plus coarse 60-band magnitudes of the frames 4 and 2 before/after (context).
 3. KL-NMF with FIXED dictionaries: 64 templates per stem (vocals, drums, other); only the activations are
    fitted to the mixture (100 multiplicative updates). Three dictionary sets (different seeds / train
    remixes) are fitted separately and their stem models averaged.
 4. Wiener-style soft masks model_s / sum(models) per view and band, expanded back to STFT bins and applied
    to each complex view; no voice below 200 Hz (that share goes to "other"). vocals+drums+other = mix.
Templates: per-stem KL-NMF on the train stems plus 80 synthetic remixes (stems from random train songs,
+-2 semitone speed change, +-6 dB), then 10 rounds of discriminative refinement (activations taken from the
train MIXTURES, templates nudged so each stem's part reproduces the true stem).
"""
from pathlib import Path

import numpy as np
import librosa
from scipy.ndimage import median_filter

EPS = 1e-9
STATS_PATH = Path(__file__).with_name("nmf_stats.npz")
STEMS = ("vocals", "drums", "other")

P = dict(
    n_fft=4096, hop=1024,
    views=("midP", "midH", "sideP", "sideH"),   # mid/side x percussive/harmonic
    f_lin=1500.0, bpo=36,                       # band compression of the features
    hp_time=11, hp_freq=47,                     # HPSS median kernels (frames, bins)
    ctx=(-4, -2, 2, 4), ctx_bpo=6, ctx_flin=200.0,   # context frames, coarse bands for them
    n_iter=100,                                 # activation updates at separation time
    voc_hp=200.0,                               # Hz, voice high-pass (rest -> other)
)

_STATS = None


def load_stats():
    global _STATS
    if _STATS is None:
        with np.load(STATS_PATH) as z:
            _STATS = {k: z[k].astype(np.float32) for k in z.files}
    return _STATS


# ----------------------------------------------------------------------------------------------- NMF core
def update_h(V, W, H, n_iter):
    """KL-divergence multiplicative updates of the activations H only (templates W fixed, unit-sum columns)."""
    Wt = np.ascontiguousarray(W.T)
    wsum = W.sum(0)[:, None] + EPS
    for _ in range(n_iter):
        H *= (Wt @ (V / (W @ H + EPS))) / wsum
    return H


# ------------------------------------------------------------------------------------- representation
def stft(x, p):
    return librosa.stft(x, n_fft=p["n_fft"], hop_length=p["hop"])


def band_matrix(sr, n_fft, f_lin, bpo):
    """(bands, F) 0/1 matrix summing STFT bins into bands: one band per bin below f_lin, bpo per octave above."""
    F = n_fft // 2 + 1
    f = np.arange(F) * sr / n_fft
    band = np.arange(F)
    hi = f >= f_lin
    band[hi] = int(np.ceil(f_lin * n_fft / sr)) + np.floor(bpo * np.log2(f[hi] / f_lin)).astype(int)
    _, band = np.unique(band, return_inverse=True)
    C = np.zeros((band.max() + 1, F), np.float32)
    C[band, np.arange(F)] = 1.0
    return C


def matrices(sr, p):
    return (band_matrix(sr, p["n_fft"], p["f_lin"], p["bpo"]),
            band_matrix(sr, p["n_fft"], p["ctx_flin"], p["ctx_bpo"]))


def hpss_masks(X, p):
    """Percussive share (power-2 soft mask) of the mid and of the side channel of the stereo STFT X."""
    out = {}
    for b, Y in (("mid", 0.5 * (X[0] + X[1])), ("side", 0.5 * (X[0] - X[1]))):
        mag = np.abs(Y)
        h = median_filter(mag, size=(1, p["hp_time"]), mode="reflect") ** 2
        q = median_filter(mag, size=(p["hp_freq"], 1), mode="reflect") ** 2
        out[b] = (q / (q + h + EPS)).astype(np.float32)
    return out


def views_of(X, p, masks=None):
    """Complex view spectrograms (each (F, T)) of a stereo STFT X: mixture, or a train stem split with its
    MIXTURE's masks. Returns (views, masks)."""
    masks = hpss_masks(X, p) if masks is None else masks
    base = {"mid": 0.5 * (X[0] + X[1]), "side": 0.5 * (X[0] - X[1])}
    out = []
    for v in p["views"]:
        b, k = v[:-1], v[-1]
        out.append(base[b] * (masks[b] if k == "P" else 1.0 - masks[b]))
    return out, masks


def features(views, C, Cc, p):
    """Stacked band magnitudes of all views, plus coarse bands of the context frames -> (D, T)."""
    mags = [np.abs(y) for y in views]
    out = [C @ m for m in mags]
    coarse = [Cc @ m for m in mags]
    T = mags[0].shape[1]
    for o in p["ctx"]:
        idx = np.clip(np.arange(T) + o, 0, T - 1)
        out += [c[:, idx] for c in coarse]
    return np.concatenate(out, 0).astype(np.float32)


def to_stereo(view_name, Y):
    """Contribution of a (masked) mid or side view to the left/right STFT."""
    return np.stack([Y, Y]) if view_name.startswith("mid") else np.stack([Y, -Y])


# ----------------------------------------------------------------------------------------------- models
def stem_models(V, Ws, p):
    """Fixed templates Ws = [W_vocals, W_drums, W_other]; activations fitted to the mixture features V."""
    ks = np.cumsum([0] + [w.shape[1] for w in Ws])
    W = np.concatenate(Ws, 1)
    H = np.full((W.shape[1], V.shape[1]), V.sum(0).mean() / W.shape[1] + EPS, np.float32)
    H = update_h(V, W, H, p["n_iter"])
    return [W[:, ks[i]:ks[i + 1]] @ H[ks[i]:ks[i + 1]] for i in range(3)]


def dictionary_sets(st):
    """The stats hold one (W_<stem>) or several (W_<stem>_<e>) dictionary sets."""
    n = sum(1 for k in st if k.startswith("W_vocals_"))
    if not n:
        return [[st[f"W_{s}"] for s in STEMS]]
    return [[st[f"W_{s}_{e}"] for s in STEMS] for e in range(n)]


# ---------------------------------------------------------------------------------------------- separate
def separate(mix, sr, p=None, stats=None):
    p = {**P, **(p or {})}
    sets = dictionary_sets(stats if stats is not None else load_stats())
    X = stft(mix.astype(np.float32), p)                              # (2, F, T)
    C, Cc = matrices(sr, p)
    views, _ = views_of(X, p)
    V = features(views, C, Cc, p)
    models = [0.0, 0.0, 0.0]
    for Ws in sets:                                                  # ensemble: average the stem models
        models = [a + b / len(sets) for a, b in zip(models, stem_models(V, Ws, p))]
    tot = sum(models) + EPS
    Fc = C.shape[0]
    f = np.arange(C.shape[1]) * sr / p["n_fft"]
    hp = (1.0 / (1.0 + (p["voc_hp"] / np.maximum(f, 1.0)) ** 4))[:, None]
    Ys = [np.zeros_like(X) for _ in STEMS]
    for j, (vname, y) in enumerate(zip(p["views"], views)):
        rows = slice(j * Fc, (j + 1) * Fc)
        mk = [C.T @ (m[rows] / tot[rows]) for m in models]           # band masks -> STFT bins
        mk[2] = mk[2] + mk[0] * (1.0 - hp)                           # no voice below voc_hp
        mk[0] = mk[0] * hp
        for Y, m in zip(Ys, mk):
            Y += to_stereo(vname, y * m)
    return {s: librosa.istft(Y, hop_length=p["hop"], length=mix.shape[1]).astype(np.float32)
            for s, Y in zip(STEMS, Ys)}
