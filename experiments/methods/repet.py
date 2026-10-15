"""Repetition-based separation: REPET-SIM + local REPET for vocals, median-filter HPSS of the repeating part for drums.

Final algorithm (tuned on the 40 train clips only):
  1. STFT per channel, n_fft 8192 / hop 2048 (186 ms window: long windows suit both steps below).
  2. "Guide" = mono magnitude restricted to 0-500 Hz (bass + kick: the rhythm section defines what repeats, and
     the voice has little energy there, so the voice does not make frames look alike).
  3. Repeating model R = mean of two estimates, then R = min(R, |X|):
       a. REPET-SIM: per frame, median of the mixture magnitude over the 10 most similar frames (cosine on the
          guide) that are at least 0.25 s away;
       b. local REPET: repetition period from the beat spectrum (time-autocorrelation of the guide's power,
          strongest peak between 0.5 and 2.5 s); median over the frames +-1..3 periods away.
  4. Vocals = non-repeating part |X| - R, with everything below 200 Hz removed, weighted 0.7.
     Drums / other = percussive / harmonic share of R, from median filters across frequency (61 bins, ~330 Hz)
     and across time (15 frames, ~0.7 s) on mono R.
  5. Linear soft masks (each stem's estimate / sum of estimates) applied to the complex STFT, then iSTFT.
"""
import numpy as np
import scipy.ndimage as ndi
import librosa

EPS = 1e-10

N_FFT, HOP = 8192, 2048
GUIDE_FMAX = 500.0      # Hz, band used to decide which frames repeat
SIM_K = 10              # neighbours per frame (REPET-SIM)
SIM_WIDTH_S = 0.25      # ignore neighbours closer than this in time
PERIOD_RANGE_S = (0.5, 2.5)
N_REP = 3               # local REPET: use +-1..N_REP periods
VOCAL_HP_HZ = 200.0     # vocals forced to zero below this
W_VOCALS, W_DRUMS, W_OTHER = 0.7, 1.0, 1.0
HPSS_TIME, HPSS_FREQ = 15, 61   # median-filter lengths (frames, bins)


def _stft(mix):
    return np.stack([librosa.stft(c, n_fft=N_FFT, hop_length=HOP) for c in mix])


def _istft(X, length):
    return np.stack([librosa.istft(c, hop_length=HOP, length=length) for c in X])


def _knn_median(V, feat, k, width, chunk=256):
    """REPET-SIM: per frame, median of V over the k frames most similar to it (cosine), >= width frames away.
    Chunked over frames so memory stays bounded on long excerpts (no full T x T x F array)."""
    T = feat.shape[1]
    f = feat / (np.linalg.norm(feat, axis=0, keepdims=True) + EPS)
    k = int(max(1, min(k, T - 2 * width - 1))) if T > 2 * width + 1 else 1
    out = np.empty_like(V)
    cols = np.arange(T)
    for s in range(0, T, chunk):
        e = min(T, s + chunk)
        sim = f[:, s:e].T @ f
        sim[np.abs(np.arange(s, e)[:, None] - cols[None, :]) < width] = -np.inf
        nbr = np.argpartition(-sim, k - 1, axis=1)[:, :k]
        for c in range(V.shape[0]):
            out[c, :, s:e] = np.median(V[c][:, nbr], axis=2)
    return out


def _beat_spectrum(power):
    """Rafii & Pardo beat spectrum: mean over frequency of each row's (unbiased) time-autocorrelation."""
    T = power.shape[1]
    spec = np.fft.rfft(power, n=2 * T, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, :T] / np.arange(T, 0, -1)[None, :]
    b = ac.mean(0)
    return b / (b[0] + EPS)


def _period(b, pmin, pmax):
    pmax = min(pmax, len(b) - 2)
    if pmax <= pmin:
        return max(1, min(pmin, len(b) - 1))
    seg = b[pmin:pmax + 1]
    peaks = [i for i in range(1, len(seg) - 1) if seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]]
    return pmin + (max(peaks, key=lambda j: seg[j]) if peaks else int(np.argmax(seg)))


def _repet_local(V, p, n_rep, fchunk=512):
    """Local REPET: median of the frames at t + j*p, j = -n_rep..n_rep (chunked over frequency for memory)."""
    C, F, T = V.shape
    out = np.empty_like(V)
    shifts = [j * p for j in range(-n_rep, n_rep + 1) if abs(j * p) < T]
    for c in range(C):
        for f0 in range(0, F, fchunk):
            blk = V[c, f0:f0 + fchunk]
            stack = np.full((len(shifts),) + blk.shape, np.nan, dtype=V.dtype)
            for i, d in enumerate(shifts):
                if d >= 0:
                    stack[i, :, :T - d] = blk[:, d:]
                else:
                    stack[i, :, -d:] = blk[:, :T + d]
            out[c, f0:f0 + fchunk] = np.nanmedian(stack, axis=0)
    return out


def separate(mix, sr):
    length = mix.shape[1]
    X = _stft(mix.astype(np.float32))
    V = np.abs(X)
    frame_rate = sr / HOP
    bin_hz = sr / N_FFT

    guide = V.mean(0)[: int(GUIDE_FMAX / bin_hz) + 1]

    # repeating background: nearest-neighbour median + period-based median
    R_sim = _knn_median(V, guide, SIM_K, max(1, int(round(SIM_WIDTH_S * frame_rate))))
    p = _period(_beat_spectrum(guide ** 2), max(1, int(PERIOD_RANGE_S[0] * frame_rate)),
                int(PERIOD_RANGE_S[1] * frame_rate))
    R_rep = _repet_local(V, max(1, p), N_REP)
    R = np.minimum(0.5 * (R_sim + R_rep), V)
    del R_sim, R_rep

    # vocals = what does not repeat
    E_v = V - R
    E_v[:, : int(VOCAL_HP_HZ / bin_hz) + 1, :] = 0

    # drums vs other inside the repeating part: median-filter HPSS on mono R
    Rm = R.mean(0, keepdims=True)
    harm = ndi.median_filter(Rm, size=(1, 1, HPSS_TIME), mode="reflect")
    perc = ndi.median_filter(Rm, size=(1, HPSS_FREQ, 1), mode="reflect")
    share = perc ** 2 / (harm ** 2 + perc ** 2 + EPS)
    E_d, E_o = R * share, R * (1 - share)

    A = (W_VOCALS * E_v, W_DRUMS * E_d, W_OTHER * E_o)
    total = A[0] + A[1] + A[2] + EPS
    return {name: _istft(X * (a / total), length).astype(np.float32)
            for name, a in zip(("vocals", "drums", "other"), A)}
