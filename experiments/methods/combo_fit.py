"""Fit the fusion weights of combo.py on the TRAIN split (never the test split) -> combo_stats.npz.

    .venv/bin/python experiments/methods/combo_fit.py        # ~3 min, 2 worker processes, < 1.5 GB each

1. Every train clip is separated by the five experts of combo.py. The NMF expert uses OUT-OF-FOLD
   dictionaries (fit on the other half of the train songs, the nmf experimenter's 2-fold CV sets), so its
   masks on train are as good as on a song it has never seen; otherwise the fusion would over-trust it.
2. For 48 random frames per clip, store each expert's log ratio masks (features) and, from the true stems,
   what an ideal mask would need (|X|^2, Re(S X*), |S|^2), with per-clip weights 1 / (stem energy + 3% mix).
3. In each of 12 log-spaced bands, fit a softmax-linear model mask_s = softmax_s(A_s . features + c_s) by
   minimising the weighted spectrogram error sum |mask_s X - S_s|^2 (L-BFGS), with an L2 pull (lam) toward the
   plain geometric mean of the experts.
"""
import os

os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")

import importlib.util  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
from scipy.optimize import minimize  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
from common import STEMS, load_track, tracks  # noqa: E402

_spec = importlib.util.spec_from_file_location("combo_method", HERE / "combo.py")
combo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(combo)

METHODS = ["hpss", "repet", "pitch", "freq_bands", "nmf"]
NB, LAM, NF, MAXROWS, MAXITER = 12, 1e-2, 48, 200000, 200
FMIN, FMAX = 40.0, 16000.0
# out-of-fold NMF dictionary sets (seeds 0/1/2, 40 remixes) from the nmf experimenter's 2-fold CV cache:
# fold k was fit on the train songs with index % 2 != k, and is used for the songs with index % 2 == k.
NMF_CACHE = ROOT / "outputs/nmf/scratch/cache"
NMF_FOLD = {0: ["fdf0d6422d618806dda97271d977a758", "e821bc5128ae8607b2d92bd485bfd39a", "21d368636a7f20264d0924a217ce791b"],
            1: ["e33bd4c078e97cd9d1352b034b26372d", "8085c08df320b97c173b7347c0422a46", "f8461ad6877ab34b726d0eca752ef228"]}


def nmf_fold_stats(fold):
    """Out-of-fold dictionary sets; refit with nmf_fit.fit (slow) if the cache is gone."""
    files = [NMF_CACHE / f"{h}.npz" for h in NMF_FOLD[fold]]
    stats = {}
    if all(f.exists() for f in files):
        for e, f in enumerate(files):
            with np.load(f) as z:
                stats.update({f"{k}_{e}": z[k].astype(np.float32) for k in z.files})
        return stats
    spec = importlib.util.spec_from_file_location("nmf_fit", HERE / "nmf_fit.py")
    nmf_fit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nmf_fit)
    fit_set = [d for i, d in enumerate(tracks("train")) if i % 2 != fold]
    p = {"views": ["midP", "midH", "sideP", "sideH"], "ctx": [-4, -2, 2, 4], "voc_hp": 200}
    for e in range(3):
        st = nmf_fit.fit(fit_set, p, K=(64, 64, 64), seed=e, disc_epochs=10, aug={"n_remix": 40, "aug_seed": e})
        stats.update({f"{k}_{e}": v.astype(np.float32) for k, v in st.items()})
    return stats


def clip_rows(idx):
    """Features and targets for NF random frames of train clip idx: arrays (F, 2, NF, ...)."""
    d = tracks("train")[idx]
    mix, refs = load_track(d)
    experts = combo.run_experts(mix, 44100, METHODS, nmf_stats=nmf_fold_stats(idx % 2), threads=1)
    X = combo.fstft(mix)
    S = [combo.fstft(refs[s]) for s in STEMS]
    T = X.shape[-1]
    fr = np.sort(np.random.default_rng(idx).choice(T, NF, replace=False))
    feats = np.stack([lm[..., fr] for m in METHODS for lm in combo.log_ratio_masks(experts[m])], -1)
    EX = float(np.sum(np.abs(X) ** 2))
    w = np.array([1.0 / (float(np.sum(np.abs(s) ** 2)) + 0.03 * EX) for s in S]) * (T / NF)
    Xf = X[..., fr]
    P = np.abs(Xf) ** 2
    Q = np.stack([np.real(s[..., fr] * np.conj(Xf)) for s in S], -1)
    SS = np.stack([np.abs(s[..., fr]) ** 2 for s in S], -1)
    tr = lambda a, t=np.float32: np.ascontiguousarray(np.moveaxis(a, 1, 0)).astype(t)   # (F, 2, NF, ...)
    return idx, tr(feats, np.float16), tr(P), tr(Q), tr(SS), w     # features in float16 to save memory


def init_A(nm):
    A = np.zeros((3, 3 * nm))
    for j in range(nm):
        for s in range(3):
            A[s, 3 * j + s] = 1.0 / nm
    return A


def fit_band(Fb, P, Q, SS, Wt, A0, lam, maxiter=MAXITER):
    """Softmax-linear masks for one band: minimise sum w |M X - S|^2 / sum w |S|^2 + lam |A - A0|^2."""
    nf = Fb.shape[1]
    Fb = Fb.astype(np.float64)
    norm = float(np.sum(Wt * SS)) + 1e-30

    def f(x):
        A, c = x[:3 * nf].reshape(3, nf), x[3 * nf:]
        z = Fb @ A.T + c
        z -= z.max(1, keepdims=True)
        M = np.exp(z)
        M /= M.sum(1, keepdims=True)
        err = np.sum(Wt * (M * M * P[:, None] - 2 * M * Q + SS)) / norm
        g = 2 * Wt * (M * P[:, None] - Q) / norm
        dz = M * (g - np.sum(g * M, 1, keepdims=True))
        gA = dz.T @ Fb + 2 * lam * (A - A0)
        return err + lam * np.sum((A - A0) ** 2), np.concatenate([gA.ravel(), dz.sum(0)])

    r = minimize(f, np.concatenate([A0.ravel(), np.zeros(3)]), jac=True, method="L-BFGS-B",
                 options={"maxiter": maxiter})
    return r.x[:3 * nf].reshape(3, nf), r.x[3 * nf:], float(r.fun)


def main():
    from multiprocessing import get_context
    n = len(tracks("train"))
    with get_context("spawn").Pool(2) as pool:
        res = [list(r) for r in sorted(pool.map(clip_rows, range(n), chunksize=1), key=lambda r: r[0])]
    W = np.stack([r[5] for r in res])                              # (clips, 3)
    feats = np.stack([r[1] for r in res], 1)                       # (F, clips, 2, NF, nf)
    for r in res:
        r[1] = None
    P = np.stack([r[2] for r in res], 1)
    Q = np.stack([r[3] for r in res], 1)
    SS = np.stack([r[4] for r in res], 1)
    del res
    edges = np.concatenate([[0.0], np.geomspace(FMIN, FMAX, NB - 1)])
    bob = combo.band_of_bin(edges, 44100)
    nf = feats.shape[-1]
    A0 = init_A(len(METHODS))
    As, cs, log = [], [], []
    for b in range(NB):
        sl = bob == b
        Fe = feats[sl].reshape(-1, nf)
        Pb = P[sl].reshape(-1)
        Qb = Q[sl].reshape(-1, 3)
        Sb = SS[sl].reshape(-1, 3)
        Wb = np.broadcast_to(W[None, :, None, None, :], SS[sl].shape).reshape(-1, 3)
        if len(Pb) > MAXROWS:
            sel = np.sort(np.random.default_rng(b).choice(len(Pb), MAXROWS, replace=False))
            Fe, Pb, Qb, Sb, Wb = Fe[sel], Pb[sel], Qb[sel], Sb[sel], Wb[sel]
        A, c, fun = fit_band(Fe, Pb.astype(np.float64), Qb.astype(np.float64), Sb.astype(np.float64), Wb, A0, LAM)
        As.append(A)
        cs.append(c)
        log.append({"band": b, "lo_hz": float(edges[b]), "rows": int(len(Pb)), "loss": fun})
        print(log[-1], flush=True)
    np.savez(HERE / "combo_stats.npz", A=np.stack(As).astype(np.float32), c=np.stack(cs).astype(np.float32),
             methods=np.array(METHODS), edges=edges, lam=LAM)
    print("wrote", HERE / "combo_stats.npz")
    (ROOT / "outputs/combo/scratch").mkdir(parents=True, exist_ok=True)
    (ROOT / "outputs/combo/scratch/fit_log.json").write_text(json.dumps(log, indent=1))


if __name__ == "__main__":
    main()
