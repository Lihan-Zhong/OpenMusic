"""Pitch / melody-guided separation: follow the sung melody, then pull out the voice around that pitch.

Final algorithm (pure signal processing, no training data):
 1. STFT 4096 / hop 1024 of both channels; mag = mean of the two channel magnitudes.
 2. Melody tracker (Melodia-style, for polyphonic audio): emphasise the "wobbly" part of the spectrum
    (mag x percussive-mask of a 41-frame-long horizontal median filter, so steady instrument notes are
    damped and vibrato/slides survive), A-weight, keep spectral peaks, resample to a 20-cent log grid,
    harmonic sum of 10 harmonics (weight 0.8^(h-1)) for f0 candidates 80-1000 Hz, Viterbi path, then a
    +-30-cent fine-tune per frame. A frame counts as sung when its salience >= 0.3 x the clip's 90th pct.
 3. Voice model fitted to the mixture (source-filter NMF after Durrieu et al. 2010, KL divergence,
    40 multiplicative updates): voice = smooth spectral envelope (30 mel-spaced bumps) x harmonic comb
    (40 harmonics, 1/h roll-off, Hann-lobe shaped, pitch free to move +-60 cents around the track) plus a
    flat noise source near sung frames (consonants, breaths); accompaniment = free NMF with 15 parts.
    vocals mask = voice / (voice + accompaniment).
 4. Drums = median-filter HPSS (31x31, power 2) percussive mask x (1 - vocals mask); other = the rest.
"""
import numpy as np
import librosa
import scipy.ndimage as ndi

P = dict(
    n_fft=4096, hop=1024,
    # melody tracker
    fmin=80.0, fmax=1000.0, sal_step=20.0, sal_harm=10, sal_alpha=0.8, sal_tol=50.0, sal_peak_db=40.0,
    sal_fmax_spec=5000.0, sal_jump_cents=100.0, sal_sharp=10.0, fl_time=41, refine_cents=30, refine_harm=12,
    vthresh=0.3,
    # source-filter voice model + accompaniment NMF
    nm_band=3, nm_step=20.0, nm_harm=40, nm_slope=1.0, nm_uv_frames=3, nm_uv_init=0.1,
    nm_nfilt=30, nm_rank=15, nm_iter=40,
    # drums
    hpss_kernel=(31, 31), hpss_power=2.0,
)


# ----------------------------------------------------------------------------------------------- tracker
def _logfreq(m, sr, p):
    """Resample a linear-frequency spectrogram onto a log grid (sal_step cents) by max-pooling."""
    n_bins = m.shape[0]
    n_log = int(np.floor(1200 * np.log2(p["sal_fmax_spec"] / p["fmin"]) / p["sal_step"])) + 1
    edges = p["fmin"] * 2 ** ((np.arange(n_log + 1) - 0.5) * p["sal_step"] / 1200) * p["n_fft"] / sr
    centers = p["fmin"] * 2 ** (np.arange(n_log) * p["sal_step"] / 1200) * p["n_fft"] / sr
    lo = np.ceil(edges[:-1]).astype(int)
    hi = np.floor(edges[1:]).astype(int)
    i0 = np.floor(centers).astype(int)
    fr = (centers - i0)[:, None]
    out = m[i0] * (1 - fr) + m[np.minimum(i0 + 1, n_bins - 1)] * fr
    for k in np.nonzero(hi >= lo)[0]:
        out[k] = np.maximum(out[k], m[lo[k]:hi[k] + 1].max(0))
    return out


def _refine_f0(mag, f0, sr, p):
    """Fine-tune each frame's f0 (+-refine_cents, 2-cent steps) so the harmonics land on the actual peaks."""
    n_bins, T = mag.shape
    m = mag ** 0.5
    ks = np.arange(-p["refine_cents"], p["refine_cents"] + 1, 2)
    hs = np.arange(1, p["refine_harm"] + 1)
    w = p["sal_alpha"] ** (hs - 1)
    b = f0[None, None, :] * 2 ** (ks[:, None, None] / 1200) * hs[None, :, None] * p["n_fft"] / sr  # (K, H, T)
    valid = b < n_bins - 2
    b = np.minimum(b, n_bins - 2)
    i0 = np.floor(b).astype(int)
    fr = b - i0
    tt = np.broadcast_to(np.arange(T)[None, None, :], b.shape)
    val = m[i0, tt] * (1 - fr) + m[i0 + 1, tt] * fr
    score = np.sum(val * valid * w[None, :, None], axis=1)
    return f0 * 2 ** (ks[score.argmax(0)] / 1200)


def track_melody(mag, sr, p):
    """Returns (f0 per frame in Hz, relative confidence per frame)."""
    n_bins, T = mag.shape
    # steady notes form long horizontal lines; a voice wobbles, so keep what a long time-median can't explain
    _, Pl = librosa.decompose.hpss(mag, kernel_size=(p["fl_time"], 1), power=2.0, mask=True)
    inp = mag * Pl
    fb = np.arange(n_bins) * sr / p["n_fft"]
    m = inp * librosa.db_to_amplitude(librosa.A_weighting(np.maximum(fb, 1)))[:, None]
    pk = (m > np.roll(m, 1, 0)) & (m >= np.roll(m, -1, 0))
    pk &= m > m.max(0, keepdims=True) * 10 ** (-p["sal_peak_db"] / 20)
    L = _logfreq(m * pk, sr, p)
    tol = int(round(p["sal_tol"] / p["sal_step"]))
    if tol > 0:
        L = ndi.maximum_filter1d(L, 2 * tol + 1, axis=0)
    n_c = int(np.floor(1200 * np.log2(p["fmax"] / p["fmin"]) / p["sal_step"])) + 1
    cands = p["fmin"] * 2 ** (np.arange(n_c) * p["sal_step"] / 1200)
    S = np.zeros((n_c, T))
    for h in range(1, p["sal_harm"] + 1):           # harmonic sum: candidate f0 collects its h-th harmonic
        sh = int(round(1200 * np.log2(h) / p["sal_step"]))
        if sh >= L.shape[0]:
            break
        seg = L[sh:sh + n_c]
        S[:len(seg)] += p["sal_alpha"] ** (h - 1) * seg
    d = np.arange(n_c)[:, None] - np.arange(n_c)[None, :]
    trans = np.exp(-0.5 * (d * p["sal_step"] / p["sal_jump_cents"]) ** 2)
    trans /= trans.sum(1, keepdims=True)
    prob = (S / (S.max(0, keepdims=True) + 1e-12)) ** p["sal_sharp"]
    prob /= prob.sum(0, keepdims=True) + 1e-12
    path = librosa.sequence.viterbi(prob + 1e-12, trans)
    strength = S[path, np.arange(T)]
    f0 = _refine_f0(inp, cands[path], sr, p)
    return f0, strength / (np.percentile(strength, 90) + 1e-12)


# ------------------------------------------------------------------------------------------- voice model
def _hann_lobe(d):
    """Magnitude response of a Hann window in FFT-bin units (main lobe +-2 bins)."""
    d = np.asarray(d, np.float64)
    out = np.where(np.abs(d) < 1e-6, 1.0, np.sinc(d) / np.where(np.abs(1 - d ** 2) < 1e-6, 1e-6, 1 - d ** 2))
    out = np.where(np.abs(np.abs(d) - 1) < 1e-6, 0.5, out)
    return np.abs(out) * (np.abs(d) < 3)


def voice_mask(V, f0, voiced, sr, p):
    """Source-filter NMF guided by the melody (after Durrieu et al. 2010, simplified), KL divergence.
    voice = envelope(f,t) x [harmonic comb near the tracked pitch + noise source near sung frames]
    accompaniment = free NMF. Returns the voice's Wiener share of each time-frequency cell."""
    rng = np.random.default_rng(0)
    F, T = V.shape
    eps = 1e-9
    V = (V + eps).astype(np.float32)
    offs = np.arange(-p["nm_band"], p["nm_band"] + 1)
    K = len(offs)
    G = np.zeros((F, K + 1, T), np.float32)          # per-frame source dictionary: K pitches + 1 noise
    vt = np.nonzero(voiced)[0]
    if len(vt):
        hs = np.arange(1, p["nm_harm"] + 1)
        fk = f0[vt][:, None] * 2 ** (offs * p["nm_step"] / 1200)[None, :]
        hb = fk[:, :, None] * hs[None, None, :] * p["n_fft"] / sr                     # (Nv, K, H) bins
        amp = np.broadcast_to((hs ** -p["nm_slope"])[None, None, :], hb.shape)
        idx = np.floor(hb).astype(int)[..., None] + np.arange(-2, 4)
        val = amp[..., None] * _hann_lobe(idx - hb[..., None])
        ok = (idx >= 0) & (idx < F) & (hb[..., None] < F - 3)
        kk = np.broadcast_to(np.arange(K)[None, :, None, None], idx.shape)
        tt = np.broadcast_to(vt[:, None, None, None], idx.shape)
        np.add.at(G, (idx[ok], kk[ok], tt[ok]), val[ok].astype(np.float32))
    near = ndi.binary_dilation(voiced, iterations=p["nm_uv_frames"])
    G[:, K, near] = 1.0
    Hs = np.zeros((K + 1, T), np.float32)
    Hs[:K, vt] = 1.0
    Hs[K, near] = p["nm_uv_init"]
    mel = librosa.hz_to_mel(np.arange(F) * sr / p["n_fft"])
    cen = np.linspace(mel[0], mel[-1], p["nm_nfilt"])
    wid = (cen[1] - cen[0]) * 2
    dm = mel[:, None] - cen[None, :]
    Wg = (np.clip(np.cos(np.pi / 2 * dm / wid), 0, None) ** 2 * (np.abs(dm) < wid)).astype(np.float32) + 1e-4
    Hg = np.ones((p["nm_nfilt"], T), np.float32)
    Wm = rng.random((F, p["nm_rank"])).astype(np.float32) + 0.1
    Hm = rng.random((p["nm_rank"], T)).astype(np.float32) + 0.1

    def model():
        src = np.einsum("fkt,kt->ft", G, Hs)
        filt = Wg @ Hg
        return src, filt, src * filt, Wm @ Hm

    src, filt, Sv, Sa = model()
    scale = V.mean() / ((Sv + Sa).mean() + eps)
    Hm *= scale
    Hg *= scale
    ones = np.ones((F, T), np.float32)
    for _ in range(p["nm_iter"]):
        src, filt, Sv, Sa = model()
        Rn = V / (Sv + Sa + eps)
        Hs *= (np.einsum("fkt,ft->kt", G, filt * Rn) + eps) / (np.einsum("fkt,ft->kt", G, filt * ones) + eps)
        src = np.einsum("fkt,kt->ft", G, Hs)
        Rn = V / (src * filt + Sa + eps)
        Hg *= (Wg.T @ (src * Rn) + eps) / (Wg.T @ src + eps)
        Sv = src * (Wg @ Hg)
        Rn = V / (Sv + Sa + eps)
        Hm *= (Wm.T @ Rn + eps) / (Wm.sum(0)[:, None] + eps)
        Rn = V / (Sv + Wm @ Hm + eps)
        Wm *= (Rn @ Hm.T + eps) / (Hm.sum(1)[None, :] + eps)
    src, filt, Sv, Sa = model()
    return Sv / (Sv + Sa + eps)


# ---------------------------------------------------------------------------------------------- separate
def separate(mix, sr, p=None):
    p = {**P, **(p or {})}
    X = librosa.stft(mix, n_fft=p["n_fft"], hop_length=p["hop"])          # (2, F, T)
    mag = np.abs(X).mean(0)
    _, Pm = librosa.decompose.hpss(mag, kernel_size=p["hpss_kernel"], power=p["hpss_power"], mask=True)
    f0, conf = track_melody(mag, sr, p)
    voiced = conf >= p["vthresh"]
    mV = voice_mask(mag, f0, voiced, sr, p)
    mD = np.minimum(Pm * (1 - mV), 1 - mV)
    mO = 1 - mV - mD
    return {name: librosa.istft(X * m[None], hop_length=p["hop"], length=mix.shape[1])
            for name, m in (("vocals", mV), ("drums", mD), ("other", mO))}
