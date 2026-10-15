"""Combined classical pipeline: five signal-processing "experts" + a learned per-band mask fusion.

Final algorithm:
 1. Run five classical separators on the mixture (code copied from the family files, two at a time in
    threads): hpss (two-stage median-filter HPSS), repet (repetition model), pitch (melody-guided
    source-filter NMF), freq_bands (fixed per-frequency Wiener EQ), nmf (fixed-dictionary KL-NMF).
 2. STFT (4096 / hop 1024) of each expert's three stems -> per-expert ratio masks R_ms = |S_ms| / sum_s |S_ms|.
 3. Fusion = per-band log-linear (softmax) "product of experts": in each of 24 log-spaced bands,
    logit_s = sum_{m,s'} A[band, s, m, s'] * log(R_ms' + 1e-3) + c[band, s]; mask_s = softmax_s(logit).
    A and c (24 x 3 x 16 numbers) are fitted on the 40 TRAIN clips by combo_fit.py, minimising each clip's
    normalised spectrogram error with an L2 pull toward the plain geometric mean (lam 1e-2); the NMF expert's
    train inputs come from out-of-fold dictionaries, so the fusion trusts it as much as on unseen songs.
 4. Masks (per channel, summing to 1) are applied to the complex mixture STFT -> stems add up to the mix.
    Songs longer than 30 s are processed in 20 s pieces with 2 s crossfades (keeps memory < ~1.4 GB).
Train 2-fold CV (fusion fitted on 20 songs, scored on the other 20; median SDR mean): best single family
hpss 4.07, plain geometric mean of 4 experts 4.31, learned fusion of all 6 families 4.46-4.57, this
(5 experts, 12 bands, lam 1e-2) 4.68. Stereo expert, smoothed/stereo/onset features, a multichannel Wiener
post-filter, mask exponents and a low-band kick NMFD expert gave no gain. Scratch: outputs/combo/scratch/.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import librosa
import scipy.ndimage as ndi
from scipy.ndimage import median_filter

HERE = Path(__file__).resolve().parent
STEMS = ("vocals", "drums", "other")
EPS = 1e-10
FUSE_N, FUSE_HOP = 4096, 1024
FLOOR = 1e-3


# ===================================================================================== expert 1: hpss
_HP = dict(
    n1=4096, hop1=1024, h1_sec=0.25, p1_hz=500.0, margin1=1.5,
    lo_cut=150.0, lo_h_sec=0.6, lo_p_hz=40.0,
    mid_pmargin=2.0, mid_lo=300.0, mid_hi=4000.0,
    resid_cut=5000.0,
    n2=16384, hop2=4096, h2_sec=2.0, p2_hz=60.0,
    lo2_cut=2000.0, lo2_p_hz=25.0,
    voc_lo=200.0,
    power=2.0,
)


def _hp_odd(x):
    return max(1, int(round(x)) | 1)


def _hp_median_pair(mag, kh, kp, lo=None):
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


def _hp_kernels(sr, n_fft, hop, h_sec, p_hz, lo):
    kh, kp = _hp_odd(h_sec * sr / hop), _hp_odd(p_hz * n_fft / sr)
    if lo is not None:
        lo = (max(1, int(round(lo[0] * n_fft / sr))), _hp_odd(lo[1] * sr / hop), _hp_odd(lo[2] * n_fft / sr))
    return kh, kp, lo


def _hp_highpass(freqs, cut):
    return 1.0 / (1.0 + (cut / np.maximum(freqs, 1.0)) ** 4)


def expert_hpss(mix, sr):
    p, pw = _HP, _HP["power"]
    n = mix.shape[1]
    mix = mix.astype(np.float64)
    X1 = librosa.stft(mix, n_fft=p["n1"], hop_length=p["hop1"])
    kh, kp, lo = _hp_kernels(sr, p["n1"], p["hop1"], p["h1_sec"], p["p1_hz"],
                             (p["lo_cut"], p["lo_h_sec"], p["lo_p_hz"]))
    H, V = _hp_median_pair(np.abs(X1).sum(0), kh, kp, lo)
    f1 = librosa.fft_frequencies(sr=sr, n_fft=p["n1"])[:, None]
    lf = np.log2(np.maximum(f1, 1.0))
    bump = np.clip(1.0 - np.maximum(np.log2(p["mid_lo"]) - lf, lf - np.log2(p["mid_hi"])) / 0.5, 0.0, 1.0)
    m_h = p["margin1"]
    m_p = p["margin1"] * (1.0 + (p["mid_pmargin"] - 1.0) * bump)
    mh = H ** pw / (H ** pw + m_h ** pw * V ** pw + EPS)
    mp = V ** pw / (V ** pw + m_p ** pw * H ** pw + EPS)
    md = mp + (1.0 - mh - mp) * _hp_highpass(f1, p["resid_cut"])
    drums = librosa.istft(X1 * md[None], hop_length=p["hop1"], length=n)
    del X1, H, V, mh, mp, md
    rest = mix - drums
    X2 = librosa.stft(rest, n_fft=p["n2"], hop_length=p["hop2"])
    kh, kp, lo = _hp_kernels(sr, p["n2"], p["hop2"], p["h2_sec"], p["p2_hz"],
                             (p["lo2_cut"], p["h2_sec"], p["lo2_p_hz"]))
    H, V = _hp_median_pair(np.abs(X2).sum(0), kh, kp, lo)
    mv = V ** pw / (H ** pw + V ** pw + EPS)
    mv = mv * _hp_highpass(librosa.fft_frequencies(sr=sr, n_fft=p["n2"])[:, None], p["voc_lo"])
    vocals = librosa.istft(X2 * mv[None], hop_length=p["hop2"], length=n)
    other = mix - drums - vocals
    return {"vocals": vocals.astype(np.float32), "drums": drums.astype(np.float32),
            "other": other.astype(np.float32)}


# ==================================================================================== expert 2: repet
_RP = dict(n_fft=8192, hop=2048, guide_fmax=500.0, sim_k=10, sim_width_s=0.25, period_range_s=(0.5, 2.5),
           n_rep=3, vocal_hp_hz=200.0, w=(0.7, 1.0, 1.0), hpss_time=15, hpss_freq=61)


def _rp_knn_median(V, feat, k, width, chunk=256):
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


def _rp_beat_spectrum(power):
    T = power.shape[1]
    spec = np.fft.rfft(power, n=2 * T, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, :T] / np.arange(T, 0, -1)[None, :]
    b = ac.mean(0)
    return b / (b[0] + EPS)


def _rp_period(b, pmin, pmax):
    pmax = min(pmax, len(b) - 2)
    if pmax <= pmin:
        return max(1, min(pmin, len(b) - 1))
    seg = b[pmin:pmax + 1]
    peaks = [i for i in range(1, len(seg) - 1) if seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]]
    return pmin + (max(peaks, key=lambda j: seg[j]) if peaks else int(np.argmax(seg)))


def _rp_local(V, p, n_rep, fchunk=512):
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


def expert_repet(mix, sr):
    p = _RP
    length = mix.shape[1]
    X = np.stack([librosa.stft(c, n_fft=p["n_fft"], hop_length=p["hop"]) for c in mix.astype(np.float32)])
    V = np.abs(X)
    frame_rate = sr / p["hop"]
    bin_hz = sr / p["n_fft"]
    guide = V.mean(0)[: int(p["guide_fmax"] / bin_hz) + 1]
    R_sim = _rp_knn_median(V, guide, p["sim_k"], max(1, int(round(p["sim_width_s"] * frame_rate))))
    per = _rp_period(_rp_beat_spectrum(guide ** 2), max(1, int(p["period_range_s"][0] * frame_rate)),
                     int(p["period_range_s"][1] * frame_rate))
    R_rep = _rp_local(V, max(1, per), p["n_rep"])
    R = np.minimum(0.5 * (R_sim + R_rep), V)
    del R_sim, R_rep
    E_v = V - R
    E_v[:, : int(p["vocal_hp_hz"] / bin_hz) + 1, :] = 0
    Rm = R.mean(0, keepdims=True)
    harm = ndi.median_filter(Rm, size=(1, 1, p["hpss_time"]), mode="reflect")
    perc = ndi.median_filter(Rm, size=(1, p["hpss_freq"], 1), mode="reflect")
    share = perc ** 2 / (harm ** 2 + perc ** 2 + EPS)
    E_d, E_o = R * share, R * (1 - share)
    A = (p["w"][0] * E_v, p["w"][1] * E_d, p["w"][2] * E_o)
    total = A[0] + A[1] + A[2] + EPS
    return {name: np.stack([librosa.istft(c, hop_length=p["hop"], length=length) for c in X * (a / total)]
                           ).astype(np.float32)
            for name, a in zip(STEMS, A)}


# ==================================================================================== expert 3: pitch
_PT = dict(
    n_fft=4096, hop=1024,
    fmin=80.0, fmax=1000.0, sal_step=20.0, sal_harm=10, sal_alpha=0.8, sal_tol=50.0, sal_peak_db=40.0,
    sal_fmax_spec=5000.0, sal_jump_cents=100.0, sal_sharp=10.0, fl_time=41, refine_cents=30, refine_harm=12,
    vthresh=0.3,
    nm_band=3, nm_step=20.0, nm_harm=40, nm_slope=1.0, nm_uv_frames=3, nm_uv_init=0.1,
    nm_nfilt=30, nm_rank=15, nm_iter=40,
    hpss_kernel=(31, 31), hpss_power=2.0,
)


def _pt_logfreq(m, sr, p):
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


def _pt_refine_f0(mag, f0, sr, p):
    n_bins, T = mag.shape
    m = mag ** 0.5
    ks = np.arange(-p["refine_cents"], p["refine_cents"] + 1, 2)
    hs = np.arange(1, p["refine_harm"] + 1)
    w = p["sal_alpha"] ** (hs - 1)
    b = f0[None, None, :] * 2 ** (ks[:, None, None] / 1200) * hs[None, :, None] * p["n_fft"] / sr
    valid = b < n_bins - 2
    b = np.minimum(b, n_bins - 2)
    i0 = np.floor(b).astype(int)
    fr = b - i0
    tt = np.broadcast_to(np.arange(T)[None, None, :], b.shape)
    val = m[i0, tt] * (1 - fr) + m[i0 + 1, tt] * fr
    score = np.sum(val * valid * w[None, :, None], axis=1)
    return f0 * 2 ** (ks[score.argmax(0)] / 1200)


def _pt_track_melody(mag, sr, p):
    n_bins, T = mag.shape
    _, Pl = librosa.decompose.hpss(mag, kernel_size=(p["fl_time"], 1), power=2.0, mask=True)
    inp = mag * Pl
    fb = np.arange(n_bins) * sr / p["n_fft"]
    m = inp * librosa.db_to_amplitude(librosa.A_weighting(np.maximum(fb, 1)))[:, None]
    pk = (m > np.roll(m, 1, 0)) & (m >= np.roll(m, -1, 0))
    pk &= m > m.max(0, keepdims=True) * 10 ** (-p["sal_peak_db"] / 20)
    L = _pt_logfreq(m * pk, sr, p)
    tol = int(round(p["sal_tol"] / p["sal_step"]))
    if tol > 0:
        L = ndi.maximum_filter1d(L, 2 * tol + 1, axis=0)
    n_c = int(np.floor(1200 * np.log2(p["fmax"] / p["fmin"]) / p["sal_step"])) + 1
    cands = p["fmin"] * 2 ** (np.arange(n_c) * p["sal_step"] / 1200)
    S = np.zeros((n_c, T))
    for h in range(1, p["sal_harm"] + 1):
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
    f0 = _pt_refine_f0(inp, cands[path], sr, p)
    return f0, strength / (np.percentile(strength, 90) + 1e-12)


def _pt_hann_lobe(d):
    d = np.asarray(d, np.float64)
    out = np.where(np.abs(d) < 1e-6, 1.0, np.sinc(d) / np.where(np.abs(1 - d ** 2) < 1e-6, 1e-6, 1 - d ** 2))
    out = np.where(np.abs(np.abs(d) - 1) < 1e-6, 0.5, out)
    return np.abs(out) * (np.abs(d) < 3)


def _pt_voice_mask(V, f0, voiced, sr, p):
    rng = np.random.default_rng(0)
    F, T = V.shape
    eps = 1e-9
    V = (V + eps).astype(np.float32)
    offs = np.arange(-p["nm_band"], p["nm_band"] + 1)
    K = len(offs)
    G = np.zeros((F, K + 1, T), np.float32)
    vt = np.nonzero(voiced)[0]
    if len(vt):
        hs = np.arange(1, p["nm_harm"] + 1)
        fk = f0[vt][:, None] * 2 ** (offs * p["nm_step"] / 1200)[None, :]
        hb = fk[:, :, None] * hs[None, None, :] * p["n_fft"] / sr
        amp = np.broadcast_to((hs ** -p["nm_slope"])[None, None, :], hb.shape)
        idx = np.floor(hb).astype(int)[..., None] + np.arange(-2, 4)
        val = amp[..., None] * _pt_hann_lobe(idx - hb[..., None])
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


def expert_pitch(mix, sr):
    p = _PT
    X = librosa.stft(mix, n_fft=p["n_fft"], hop_length=p["hop"])
    mag = np.abs(X).mean(0)
    _, Pm = librosa.decompose.hpss(mag, kernel_size=p["hpss_kernel"], power=p["hpss_power"], mask=True)
    f0, conf = _pt_track_melody(mag, sr, p)
    voiced = conf >= p["vthresh"]
    mV = _pt_voice_mask(mag, f0, voiced, sr, p)
    mD = np.minimum(Pm * (1 - mV), 1 - mV)
    mO = 1 - mV - mD
    return {name: librosa.istft(X * m[None], hop_length=p["hop"], length=mix.shape[1])
            for name, m in (("vocals", mV), ("drums", mD), ("other", mO))}


# =============================================================================== expert 4: freq_bands
_FB = {}


def expert_freq_bands(mix, sr):
    if sr not in _FB:
        z = np.load(HERE / "freq_bands_stats.npz")
        mask, n_fft = z["mask"].astype(np.float32), int(z["n_fft"])
        if sr != 44100:
            f_fit = librosa.fft_frequencies(sr=44100, n_fft=n_fft)
            f_new = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
            mask = np.stack([np.interp(f_new, f_fit, m) for m in mask]).astype(np.float32)
        _FB[sr] = (mask, n_fft)
    mask, n_fft = _FB[sr]
    hop = n_fft // 4
    n = mix.shape[1]
    X = librosa.stft(np.asarray(mix, np.float32), n_fft=n_fft, hop_length=hop)
    return {s: librosa.istft(X * mask[i][None, :, None], hop_length=hop, length=n).astype(np.float32)
            for i, s in enumerate(STEMS)}


# ====================================================================================== expert 5: nmf
_NM = dict(
    n_fft=4096, hop=1024,
    views=("midP", "midH", "sideP", "sideH"),
    f_lin=1500.0, bpo=36,
    hp_time=11, hp_freq=47,
    ctx=(-4, -2, 2, 4), ctx_bpo=6, ctx_flin=200.0,
    n_iter=100,
    voc_hp=200.0,
)
_NM_STATS = {}
_NM_EPS = 1e-9


def _nm_load_stats():
    if not _NM_STATS:
        with np.load(HERE / "nmf_stats.npz") as z:
            _NM_STATS.update({k: z[k].astype(np.float32) for k in z.files})
    return _NM_STATS


def _nm_update_h(V, W, H, n_iter):
    Wt = np.ascontiguousarray(W.T)
    wsum = W.sum(0)[:, None] + _NM_EPS
    for _ in range(n_iter):
        H *= (Wt @ (V / (W @ H + _NM_EPS))) / wsum
    return H


def _nm_band_matrix(sr, n_fft, f_lin, bpo):
    F = n_fft // 2 + 1
    f = np.arange(F) * sr / n_fft
    band = np.arange(F)
    hi = f >= f_lin
    band[hi] = int(np.ceil(f_lin * n_fft / sr)) + np.floor(bpo * np.log2(f[hi] / f_lin)).astype(int)
    _, band = np.unique(band, return_inverse=True)
    C = np.zeros((band.max() + 1, F), np.float32)
    C[band, np.arange(F)] = 1.0
    return C


def _nm_hpss_masks(X, p):
    out = {}
    for b, Y in (("mid", 0.5 * (X[0] + X[1])), ("side", 0.5 * (X[0] - X[1]))):
        mag = np.abs(Y)
        h = median_filter(mag, size=(1, p["hp_time"]), mode="reflect") ** 2
        q = median_filter(mag, size=(p["hp_freq"], 1), mode="reflect") ** 2
        out[b] = (q / (q + h + _NM_EPS)).astype(np.float32)
    return out


def _nm_views(X, p):
    masks = _nm_hpss_masks(X, p)
    base = {"mid": 0.5 * (X[0] + X[1]), "side": 0.5 * (X[0] - X[1])}
    out = []
    for v in p["views"]:
        b, k = v[:-1], v[-1]
        out.append(base[b] * (masks[b] if k == "P" else 1.0 - masks[b]))
    return out


def _nm_features(views, C, Cc, p):
    mags = [np.abs(y) for y in views]
    out = [C @ m for m in mags]
    coarse = [Cc @ m for m in mags]
    T = mags[0].shape[1]
    for o in p["ctx"]:
        idx = np.clip(np.arange(T) + o, 0, T - 1)
        out += [c[:, idx] for c in coarse]
    return np.concatenate(out, 0).astype(np.float32)


def _nm_stem_models(V, Ws, p):
    ks = np.cumsum([0] + [w.shape[1] for w in Ws])
    W = np.concatenate(Ws, 1)
    H = np.full((W.shape[1], V.shape[1]), V.sum(0).mean() / W.shape[1] + _NM_EPS, np.float32)
    H = _nm_update_h(V, W, H, p["n_iter"])
    return [W[:, ks[i]:ks[i + 1]] @ H[ks[i]:ks[i + 1]] for i in range(3)]


def _nm_dictionary_sets(st):
    n = sum(1 for k in st if k.startswith("W_vocals_"))
    if not n:
        return [[st[f"W_{s}"] for s in STEMS]]
    return [[st[f"W_{s}_{e}"] for s in STEMS] for e in range(n)]


def expert_nmf(mix, sr, stats=None):
    """stats: dictionary sets to use (default nmf_stats.npz; combo_fit.py passes out-of-fold sets)."""
    p = _NM
    sets = _nm_dictionary_sets(stats if stats is not None else _nm_load_stats())
    X = librosa.stft(mix.astype(np.float32), n_fft=p["n_fft"], hop_length=p["hop"])
    C = _nm_band_matrix(sr, p["n_fft"], p["f_lin"], p["bpo"])
    Cc = _nm_band_matrix(sr, p["n_fft"], p["ctx_flin"], p["ctx_bpo"])
    views = _nm_views(X, p)
    V = _nm_features(views, C, Cc, p)
    models = [0.0, 0.0, 0.0]
    for Ws in sets:
        models = [a + b / len(sets) for a, b in zip(models, _nm_stem_models(V, Ws, p))]
    tot = sum(models) + _NM_EPS
    Fc = C.shape[0]
    f = np.arange(C.shape[1]) * sr / p["n_fft"]
    hp = (1.0 / (1.0 + (p["voc_hp"] / np.maximum(f, 1.0)) ** 4))[:, None]
    Ys = [np.zeros_like(X) for _ in STEMS]
    for j, (vname, y) in enumerate(zip(p["views"], views)):
        rows = slice(j * Fc, (j + 1) * Fc)
        mk = [C.T @ (m[rows] / tot[rows]) for m in models]
        mk[2] = mk[2] + mk[0] * (1.0 - hp)
        mk[0] = mk[0] * hp
        for Y, m in zip(Ys, mk):
            Y += np.stack([y * m, y * m]) if vname.startswith("mid") else np.stack([y * m, -y * m])
    return {s: librosa.istft(Y, hop_length=p["hop"], length=mix.shape[1]).astype(np.float32)
            for s, Y in zip(STEMS, Ys)}


# ========================================================================================= fusion
EXPERTS = {"hpss": expert_hpss, "repet": expert_repet, "pitch": expert_pitch,
           "freq_bands": expert_freq_bands, "nmf": expert_nmf}
_FUSE = {}


def load_fusion():
    if not _FUSE:
        with np.load(HERE / "combo_stats.npz") as z:
            _FUSE.update(A=z["A"].astype(np.float32), c=z["c"].astype(np.float32),
                         methods=[str(m) for m in z["methods"]], edges=z["edges"].astype(np.float64))
    return _FUSE


def run_experts(mix, sr, methods, nmf_stats=None, threads=2):
    def one(m):
        if m == "nmf":
            return m, expert_nmf(mix, sr, nmf_stats)
        return m, EXPERTS[m](mix, sr)
    if threads > 1:
        with ThreadPoolExecutor(threads) as ex:
            return dict(ex.map(one, methods))
    return dict(one(m) for m in methods)


def fstft(x):
    return librosa.stft(np.asarray(x, np.float32), n_fft=FUSE_N, hop_length=FUSE_HOP)


def log_ratio_masks(est):
    """Per-expert log ratio masks, each (2, F, T) float32, in stem order."""
    E = [np.abs(fstft(est[s])) for s in STEMS]
    t = E[0] + E[1] + E[2] + 1e-9
    return [np.log(e / t + FLOOR).astype(np.float32) for e in E]


def band_of_bin(edges, sr):
    f = np.arange(FUSE_N // 2 + 1) * sr / FUSE_N
    return np.clip(np.searchsorted(edges, f, side="right") - 1, 0, len(edges) - 1)


def fuse(X, experts, fz, sr):
    """Softmax of per-band linear combinations of the experts' log ratio masks -> 3 masks (2, F, T)."""
    bob = band_of_bin(fz["edges"], sr)
    A = fz["A"][bob]                                      # (F, 3, nf)
    c = fz["c"][bob]                                      # (F, 3)
    z = np.zeros((3,) + X.shape, np.float32)
    k = 0
    for m in fz["methods"]:
        for lm in log_ratio_masks(experts[m]):
            for s in range(3):
                z[s] += A[:, s, k][None, :, None] * lm
            k += 1
    z += c.T[:, None, :, None]
    z -= z.max(0, keepdims=True)
    np.exp(z, out=z)
    z /= z.sum(0, keepdims=True)
    return z


CHUNK_S, OVERLAP_S = 20.0, 2.0     # long songs: 20 s pieces, 2 s linear crossfades (bounds memory)


def separate_piece(mix, sr, threads=2):
    fz = load_fusion()
    n = mix.shape[1]
    experts = run_experts(mix, sr, fz["methods"], threads=threads)
    X = fstft(mix)
    M = fuse(X, experts, fz, sr)
    del experts
    return {s: librosa.istft(X * M[i], hop_length=FUSE_HOP, length=n).astype(np.float32)
            for i, s in enumerate(STEMS)}


def separate(mix, sr):
    mix = np.asarray(mix, np.float32)
    n = mix.shape[1]
    L, O = int(CHUNK_S * sr), int(OVERLAP_S * sr)
    if n <= 1.5 * L:
        return separate_piece(mix, sr)
    out = {s: np.zeros_like(mix) for s in STEMS}
    starts = list(range(0, n - O, L - O))
    for i, s0 in enumerate(starts):
        e0 = min(s0 + L, n)
        w = np.ones(e0 - s0, np.float32)
        if i > 0:
            w[:O] = np.linspace(0, 1, O, endpoint=False, dtype=np.float32) + 0.5 / O
        if i < len(starts) - 1:
            w[-O:] = 1.0 - (np.linspace(0, 1, O, endpoint=False, dtype=np.float32) + 0.5 / O)
        est = separate_piece(mix[:, s0:e0], sr)
        for s in STEMS:
            out[s][:, s0:e0] += w * est[s]
    return out
