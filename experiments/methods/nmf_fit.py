"""Learn the per-stem NMF templates for nmf.py from the TRAIN ground-truth stems (never the test split).

    .venv/bin/python experiments/methods/nmf_fit.py      # writes experiments/methods/nmf_stats.npz (~15 min)

For each of 3 dictionary sets (different random seeds and remixes):
  1. features (exactly as nmf.py computes them) of every train mixture and of its stems, the stems split
     into views with their MIXTURE's HPSS masks; plus 80 synthetic songs, each stem from a random train song,
     sped up/down by up to 2 semitones and scaled by up to +-6 dB, summed into a new mixture;
  2. 64 templates per stem by KL-NMF (200 multiplicative updates) on that stem's features;
  3. 10 rounds of discriminative refinement: activations of all 192 templates are fitted to the MIXTURE
     features (as at separation time), then each stem's templates are updated (3 KL steps) so that its part
     W_s H_s reproduces the stem's true features.
"""
import os

os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "3")
os.environ.setdefault("OMP_NUM_THREADS", "3")

import importlib.util  # noqa: E402
import sys  # noqa: E402
from fractions import Fraction  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
from scipy.signal import resample_poly  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from common import STEMS, load, tracks  # noqa: E402

_spec = importlib.util.spec_from_file_location("nmf_method", HERE / "nmf.py")
nmf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nmf)

FINAL = dict(K=(64, 64, 64), n_iter=200, disc_epochs=10, n_remix=80)
SEEDS = (0, 1, 2)


def _feats(mix, stems, p, mats, out):
    vm, masks = nmf.views_of(nmf.stft(mix, p), p)
    out["mixture"].append(nmf.features(vm, *mats, p).astype(np.float16))     # float16: half the memory
    for s in stems:
        v, _ = nmf.views_of(nmf.stft(stems[s], p), p, masks)
        out[s].append(nmf.features(v, *mats, p).astype(np.float16))


def _shift(y, semis):
    """Speed change by `semis` semitones (pitch and spectral envelope move together, like a tape)."""
    if semis == 0:
        return y
    fr = Fraction(2 ** (-semis / 12)).limit_denominator(200)
    return resample_poly(y, fr.numerator, fr.denominator, axis=1).astype(np.float32)


def stem_mags(track_dirs, p, sr=44100, n_remix=0, aug_seed=0, max_semis=2, gain_db=6.0):
    """Features of every train song and of n_remix synthetic remixes: {stem or "mixture": [(D, T), ...]}."""
    mats = nmf.matrices(sr, p)
    out = {s: [] for s in STEMS + ("mixture",)}
    data = []
    for d in track_dirs:
        stems = {s: load(d / f"{s}.wav") for s in STEMS}
        if n_remix:
            data.append(stems)
        _feats(load(d / "mixture.wav"), stems, p, mats, out)
    rng = np.random.default_rng(aug_seed)
    for _ in range(n_remix):
        st = {}
        for s in STEMS:
            y = data[rng.integers(len(data))][s]
            st[s] = _shift(y, int(rng.integers(-max_semis, max_semis + 1))) * 10 ** (rng.uniform(-1, 1) * gain_db / 20)
        n = min(y.shape[1] for y in st.values())
        st = {s: np.ascontiguousarray(y[:, :n]) for s, y in st.items()}
        _feats(sum(st.values()), st, p, mats, out)
    return out


def _f32(a):
    return a.astype(np.float32)


def learn_dicts(Vlist, K, n_iter=200, seed=0):
    """KL-NMF V ~ W H over a list of per-song feature matrices (one song at a time, to save memory).
    Silent frames are dropped. Returns W (D, K) with unit-sum columns."""
    E = nmf.EPS
    tot = [_f32(v).sum(0) for v in Vlist]
    thr = 1e-3 * max(t.max() for t in tot)
    keep = [t > thr for t in tot]
    rng = np.random.default_rng(seed)
    pool = [(i, j) for i, k in enumerate(keep) for j in np.nonzero(k)[0]]
    pick = [pool[q] for q in rng.choice(len(pool), K, replace=len(pool) < K)]
    W = np.stack([_f32(Vlist[i][:, j]) for i, j in pick], 1)          # init: random frames
    W += W.mean() * (1e-3 + 0.1 * rng.random(W.shape).astype(np.float32))
    W /= W.sum(0, keepdims=True)
    Hs = [np.full((K, int(k.sum())), t[k].mean() / K, np.float32) for t, k in zip(tot, keep)]
    for _ in range(n_iter):
        num = np.zeros_like(W)
        den = np.zeros(K, np.float32)
        ws = W.sum(0)[:, None]
        for v, k, H in zip(Vlist, keep, Hs):
            V = _f32(v[:, k])
            H *= (W.T @ (V / (W @ H + E))) / (ws + E)
            num += (V / (W @ H + E)) @ H.T
            den += H.sum(1)
        W *= num / (den[None, :] + E)
        sc = W.sum(0, keepdims=True) + E
        W /= sc
        for H in Hs:
            H *= sc.T
    return W


def fit(track_dirs, p=None, K=(64, 64, 64), n_iter=200, seed=0, aug=None, mags=None,
        disc_epochs=10, disc_h_iter=30, disc_w_iter=3):
    """One dictionary set {W_vocals, W_drums, W_other} from the given train songs."""
    p = {**nmf.P, **(p or {})}
    mags = mags if mags is not None else stem_mags(track_dirs, p, **(aug or {}))
    Ws = [learn_dicts(mags[s], k, n_iter=n_iter, seed=seed) for s, k in zip(STEMS, K)]
    E = nmf.EPS
    ks = np.cumsum([0] + list(K))
    Hs = [np.full((ks[-1], v.shape[1]), _f32(v).sum(0).mean() / ks[-1], np.float32) for v in mags["mixture"]]
    for ep in range(disc_epochs):
        W = np.concatenate(Ws, 1)
        for t, v in enumerate(mags["mixture"]):                         # activations from the MIXTURES
            Hs[t] = nmf.update_h(_f32(v), W, Hs[t], disc_h_iter if ep else 100)
        for _ in range(disc_w_iter):                                      # templates -> true stem features
            for i, s in enumerate(STEMS):
                num = np.zeros_like(Ws[i])
                den = np.zeros(Ws[i].shape[1], np.float32)
                for t, v in enumerate(mags[s]):
                    Hi = Hs[t][ks[i]:ks[i + 1]]
                    num += (_f32(v) / (Ws[i] @ Hi + E)) @ Hi.T
                    den += Hi.sum(1)
                Ws[i] = Ws[i] * num / (den[None, :] + E)
        for i in range(3):
            sc = Ws[i].sum(0, keepdims=True) + E
            Ws[i] = Ws[i] / sc
            for H in Hs:
                H[ks[i]:ks[i + 1]] *= sc.T
    return {f"W_{s}": w for s, w in zip(STEMS, Ws)}


def _member(seed):
    st = fit(tracks("train"), K=FINAL["K"], n_iter=FINAL["n_iter"], seed=seed, disc_epochs=FINAL["disc_epochs"],
             aug={"n_remix": FINAL["n_remix"], "aug_seed": seed})
    return {f"{k}_{seed}": v for k, v in st.items()}


def main():
    from multiprocessing import get_context
    with get_context("spawn").Pool(2) as pool:          # at most 2 worker processes
        parts = pool.map(_member, SEEDS)
    stats = {k: v for d in parts for k, v in d.items()}
    np.savez_compressed(HERE / "nmf_stats.npz", **stats)
    print("wrote", HERE / "nmf_stats.npz", {k: v.shape for k, v in stats.items()})


if __name__ == "__main__":
    main()
