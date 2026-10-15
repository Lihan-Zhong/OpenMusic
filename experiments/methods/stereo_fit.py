"""Fit the stereo method's per-band gains on the 40 train clips; prints the GAINS table embedded in stereo.py.

Run from the project root: python experiments/methods/stereo_fit.py
"""
import sys; sys.path.insert(0, "experiments")
import importlib.util
import numpy as np
from common import tracks, load_track, STEMS
spec = importlib.util.spec_from_file_location("sm", "experiments/methods/stereo.py"); sm = importlib.util.module_from_spec(spec); spec.loader.exec_module(sm)
nb = sm.N_BANDS
b = sm.band_index(44100)
G = None
for td in tracks("train"):
    mix, refs = load_track(td)
    Z = sm.components(mix, 44100)          # list of (2, F, T) complex
    S = {s: np.stack([sm._stft(refs[s][0], 44100), sm._stft(refs[s][1], 44100)]) for s in STEMS}
    P = len(Z)
    if G is None:
        G = np.zeros((nb, P, P)); B = {s: np.zeros((nb, P)) for s in STEMS}
    for p in range(P):
        for q in range(P):
            G[:, p, q] += np.bincount(b, np.real(Z[p] * np.conj(Z[q])).sum(axis=(0, 2)), nb)
        for s in STEMS:
            B[s][:, p] += np.bincount(b, np.real(S[s] * np.conj(Z[p])).sum(axis=(0, 2)), nb)
reg = 1e-6 * np.trace(G, axis1=1, axis2=2)[:, None, None] * np.eye(P)
W = {s: np.linalg.solve(G + reg, B[s][..., None])[..., 0] for s in STEMS}
# project each (band, component) column onto the simplex: no negative gains, the three stems sum to 1
A = np.clip(np.stack([W[s] for s in STEMS]), 0, None); A /= A.sum(0, keepdims=True)
W = {s: A[i] for i, s in enumerate(STEMS)}
print("GAINS = {  # per band: (centre, not-centred mid, side) gain")
for s in STEMS:
    print(f'    "{s}": [')
    for k in range(nb):
        print("        (" + ", ".join(f"{v:.3f}" for v in W[s][k]) + "),")
    print("    ],")
print("}")
print("# check: gains sum per band/component", np.round(sum(W[s] for s in STEMS), 3).min(), np.round(sum(W[s] for s in STEMS), 3).max(), file=sys.stderr)
