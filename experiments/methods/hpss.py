"""Two-stage harmonic/percussive separation by median filtering (FitzGerald 2010 / 2012).

Stage 1 (drums), STFT 4096/1024 on the mid (|L|+|R|) magnitude: a time-median (0.25 s) gives the
"sustained" picture H, a frequency-median (500 Hz) gives the "hit" picture P. Below ~150 Hz the
kernels change to 0.6 s / 40 Hz so the kick (a short low blob) is told apart from bass notes
(narrow horizontal lines). Soft masks (power 2) with margin 1.5; the drum test is 2x stricter in
300-4000 Hz, where strummed-guitar attacks and consonants imitate drums. Drums = P-mask plus the
undecided residual above 5 kHz (cymbal noise). Stage 2 (vocals), STFT 16384/4096 on what is left:
at this fine frequency resolution steady instruments are long horizontal lines (2 s time-median)
while the wobbling, gliding voice behaves as "percussive" (frequency-median 25 Hz below 2 kHz,
60 Hz above). Vocals = that percussive part, high-passed at 200 Hz; other = mix - drums - vocals.
"""
import numpy as np
import librosa
from scipy.ndimage import median_filter

EPS = 1e-10

P = dict(
    # stage 1: drums vs everything sustained
    n1=4096, hop1=1024, h1_sec=0.25, p1_hz=500.0, margin1=1.5,
    lo_cut=150.0, lo_h_sec=0.6, lo_p_hz=40.0,          # kick-vs-bass kernels below lo_cut
    mid_pmargin=2.0, mid_lo=300.0, mid_hi=4000.0,       # stricter drum test in the mid band
    resid_cut=5000.0,                                   # undecided noise above this -> drums
    # stage 2: fluctuating voice vs steady instruments
    n2=16384, hop2=4096, h2_sec=2.0, p2_hz=60.0,
    lo2_cut=2000.0, lo2_p_hz=25.0,                      # narrower frequency kernel below lo2_cut
    voc_lo=200.0,                                       # voice high-pass (below -> other)
    power=2.0,
)


def _odd(x):
    return max(1, int(round(x)) | 1)


def _median_pair(mag, kh, kp, lo=None):
    """Horizontal (time) and vertical (frequency) median filters of mag (F, T).

    lo = (cut_bin, kh_lo, kp_lo): different kernels below cut_bin, crossfaded over ~1 octave."""
    H = median_filter(mag, size=(1, kh), mode="reflect")
    V = median_filter(mag, size=(kp, 1), mode="reflect")
    if lo is not None:
        cut, kh_lo, kp_lo = lo
        top = min(mag.shape[0], int(cut * 1.5) + kp_lo)
        Hl = median_filter(mag[:top], size=(1, kh_lo), mode="reflect")
        Vl = median_filter(mag[:top], size=(kp_lo, 1), mode="reflect")
        w = np.clip((cut * 1.4 - np.arange(top, dtype=float)[:, None]) / (cut * 0.7), 0.0, 1.0)
        H[:top] = w * Hl + (1 - w) * H[:top]
        V[:top] = w * Vl + (1 - w) * V[:top]
    return H, V


def _kernels(sr, n_fft, hop, h_sec, p_hz, lo):
    kh, kp = _odd(h_sec * sr / hop), _odd(p_hz * n_fft / sr)
    if lo is not None:  # (cut_hz, h_sec, p_hz) -> (bins, frames, bins)
        lo = (max(1, int(round(lo[0] * n_fft / sr))), _odd(lo[1] * sr / hop), _odd(lo[2] * n_fft / sr))
    return kh, kp, lo


def _highpass(freqs, cut):
    return 1.0 / (1.0 + (cut / np.maximum(freqs, 1.0)) ** 4)


def separate(mix, sr):
    p, pw = P, P["power"]
    n = mix.shape[1]
    mix = mix.astype(np.float64)

    # ---- stage 1: drums ----
    X1 = librosa.stft(mix, n_fft=p["n1"], hop_length=p["hop1"])
    kh, kp, lo = _kernels(sr, p["n1"], p["hop1"], p["h1_sec"], p["p1_hz"],
                          (p["lo_cut"], p["lo_h_sec"], p["lo_p_hz"]))
    H, V = _median_pair(np.abs(X1).sum(0), kh, kp, lo)
    f1 = librosa.fft_frequencies(sr=sr, n_fft=p["n1"])[:, None]
    lf = np.log2(np.maximum(f1, 1.0))
    bump = np.clip(1.0 - np.maximum(np.log2(p["mid_lo"]) - lf, lf - np.log2(p["mid_hi"])) / 0.5, 0.0, 1.0)
    m_h = p["margin1"]
    m_p = p["margin1"] * (1.0 + (p["mid_pmargin"] - 1.0) * bump)
    mh = H ** pw / (H ** pw + m_h ** pw * V ** pw + EPS)
    mp = V ** pw / (V ** pw + m_p ** pw * H ** pw + EPS)
    md = mp + (1.0 - mh - mp) * _highpass(f1, p["resid_cut"])
    drums = librosa.istft(X1 * md[None], hop_length=p["hop1"], length=n)
    rest = mix - drums

    # ---- stage 2: vocals ----
    X2 = librosa.stft(rest, n_fft=p["n2"], hop_length=p["hop2"])
    kh, kp, lo = _kernels(sr, p["n2"], p["hop2"], p["h2_sec"], p["p2_hz"],
                          (p["lo2_cut"], p["h2_sec"], p["lo2_p_hz"]))
    H, V = _median_pair(np.abs(X2).sum(0), kh, kp, lo)
    mv = V ** pw / (H ** pw + V ** pw + EPS)
    mv = mv * _highpass(librosa.fft_frequencies(sr=sr, n_fft=p["n2"])[:, None], p["voc_lo"])
    vocals = librosa.istft(X2 * mv[None], hop_length=p["hop2"], length=n)

    other = mix - drums - vocals
    return {"vocals": vocals.astype(np.float32), "drums": drums.astype(np.float32),
            "other": other.astype(np.float32)}
