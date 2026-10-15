"""Stereo / spatial separation: centre extraction + per-band spatial gains (no neural network).

1. STFT both channels (4096-point Hann window, hop 1024). Form mid M = (L+R)/2 and side S = (L-R)/2.
2. Centre estimate (upmixer trick): anything that is not dead-centre puts as much power into the side as into
   the mid, so the centred share of the mid is c = (P_mid - P_side) / P_mid, with powers averaged over a
   3-bin x 5-frame neighbourhood; c is squared to keep only clearly centred bins.
3. Three spatial components that add back to the mix exactly: centre Mc = c*M (same on L and R),
   the not-centred part of the mid Mw = (1-c)*M, and the side +S / -S.
4. Each stem = a*Mc + b*Mw + d*S, with one (a, b, d) per stem in each of 24 log-spaced frequency bands.
   The gains are a least-squares fit on the 40 MUSDB train clips (embedded below, 216 numbers); in every
   band they sum to 1 over the three stems, so vocals + drums + other = mix.

Variants tried on train (median SDR mean): karaoke mid/side + hard bands 0.89; soft upmix + hand-set band
weights 2.68; multichannel-Wiener centre 2.64; ADRess azimuth window 2.43; this learned-gain version: see report.
"""
import numpy as np
from scipy.ndimage import uniform_filter
from scipy.signal import istft, stft

N_FFT, HOP = 4096, 1024
SMOOTH = (3, 5)     # (frequency bins, frames) neighbourhood for the local mid/side powers
POWER = 2.0         # sharpen the centre fraction
N_BANDS = 24        # band edges: 0, then 23 log-spaced edges from 40 Hz to 16 kHz

# Least-squares fit on the 40 train clips (experiments/methods/stereo_fit.py), clipped at 0, columns sum to 1.
GAINS = {  # per band: (centre, not-centred mid, side) gain
    "vocals": [
        (0.012, 0.009, 0.588),
        (0.000, 0.006, 0.006),
        (0.000, 0.010, 0.004),
        (0.000, 0.010, 0.001),
        (0.003, 0.012, 0.001),
        (0.016, 0.012, 0.006),
        (0.080, 0.023, 0.027),
        (0.353, 0.009, 0.112),
        (0.543, 0.029, 0.186),
        (0.667, 0.241, 0.369),
        (0.821, 0.205, 0.427),
        (0.771, 0.309, 0.485),
        (0.787, 0.518, 0.589),
        (0.858, 0.389, 0.732),
        (0.732, 0.273, 0.410),
        (0.537, 0.054, 0.173),
        (0.554, 0.096, 0.214),
        (0.624, 0.157, 0.288),
        (0.429, 0.036, 0.111),
        (0.519, 0.000, 0.082),
        (0.681, 0.085, 0.162),
        (0.674, 0.112, 0.305),
        (0.196, 0.097, 0.132),
        (0.173, 0.028, 0.072),
    ],
    "drums": [
        (0.599, 0.535, 0.337),
        (0.439, 0.983, 0.737),
        (0.380, 0.990, 0.667),
        (0.201, 0.990, 0.448),
        (0.162, 0.705, 0.404),
        (0.232, 0.347, 0.205),
        (0.239, 0.390, 0.458),
        (0.178, 0.189, 0.191),
        (0.109, 0.098, 0.107),
        (0.035, 0.101, 0.060),
        (0.015, 0.078, 0.051),
        (0.013, 0.055, 0.036),
        (0.012, 0.037, 0.033),
        (0.011, 0.040, 0.019),
        (0.018, 0.052, 0.043),
        (0.033, 0.057, 0.053),
        (0.038, 0.144, 0.124),
        (0.065, 0.205, 0.192),
        (0.171, 0.456, 0.464),
        (0.339, 0.806, 0.801),
        (0.268, 0.837, 0.780),
        (0.296, 0.805, 0.633),
        (0.768, 0.830, 0.815),
        (0.678, 0.925, 0.849),
    ],
    "other": [
        (0.388, 0.456, 0.075),
        (0.560, 0.011, 0.257),
        (0.620, 0.000, 0.330),
        (0.799, 0.000, 0.551),
        (0.836, 0.283, 0.594),
        (0.752, 0.642, 0.788),
        (0.681, 0.587, 0.514),
        (0.469, 0.802, 0.697),
        (0.348, 0.872, 0.707),
        (0.298, 0.658, 0.572),
        (0.164, 0.717, 0.523),
        (0.216, 0.635, 0.479),
        (0.202, 0.445, 0.379),
        (0.131, 0.571, 0.249),
        (0.250, 0.674, 0.548),
        (0.430, 0.889, 0.774),
        (0.409, 0.760, 0.662),
        (0.311, 0.638, 0.520),
        (0.400, 0.508, 0.425),
        (0.143, 0.194, 0.117),
        (0.051, 0.077, 0.059),
        (0.030, 0.083, 0.062),
        (0.036, 0.073, 0.054),
        (0.149, 0.047, 0.078),
    ],
}


def _stft(x, sr):
    _, _, Z = stft(x, fs=sr, window="hann", nperseg=N_FFT, noverlap=N_FFT - HOP, boundary="even", padded=True)
    return Z


def _istft(Z, sr, n):
    _, x = istft(Z, fs=sr, window="hann", nperseg=N_FFT, noverlap=N_FFT - HOP, boundary=True)
    x = x[..., :n]
    if x.shape[-1] < n:
        x = np.pad(x, [(0, 0)] * (x.ndim - 1) + [(0, n - x.shape[-1])])
    return x


def band_index(sr):
    f = np.fft.rfftfreq(N_FFT, 1 / sr)
    edges = np.concatenate([[0], np.geomspace(40, 16000, N_BANDS - 1)])
    return np.clip(np.searchsorted(edges, f, side="right") - 1, 0, N_BANDS - 1)


def components(mix, sr):
    """Split the stereo STFT into centre / not-centred mid / side, each as a (2, F, T) complex array."""
    L, R = _stft(mix[0], sr), _stft(mix[1], sr)
    M, S = (L + R) / 2, (L - R) / 2
    pm = uniform_filter(np.abs(M) ** 2, SMOOTH)
    ps = uniform_filter(np.abs(S) ** 2, SMOOTH)
    c = np.clip((pm - ps) / (pm + 1e-12), 0, 1) ** POWER
    Mc = c * M
    Mw = M - Mc
    return [np.stack([Mc, Mc]), np.stack([Mw, Mw]), np.stack([S, -S])]


def separate(mix, sr):
    n = mix.shape[1]
    Z = components(mix, sr)
    b = band_index(sr)
    out = {}
    for stem, table in GAINS.items():
        g = np.asarray(table)[b]                                   # (F, 3)
        Y = sum(g[:, p][None, :, None] * Z[p] for p in range(len(Z)))
        out[stem] = _istft(Y, sr, n)
    return out
