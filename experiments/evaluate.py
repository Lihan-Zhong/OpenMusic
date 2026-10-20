"""Score a separation method on the MUSDB 7-second clips, and optionally render it on the song excerpts.

A method is experiments/methods/<name>.py exposing

    separate(mix, sr) -> {"vocals": ..., "drums": ..., "other": ...}

where mix and every returned stem are float arrays shaped (2, samples). The method only sees the mixture.

    python experiments/evaluate.py hpss                       # tune on the 40 train clips
    python experiments/evaluate.py hpss --split test          # final score on the 50 test clips
    python experiments/evaluate.py hpss --split test --song   # also write lossless FLAC stems for data/song_excerpts/*.wav
"""
import argparse
import importlib.util
import json
import time

import numpy as np

from common import EXCERPTS, LOSSLESS_EXT, OUTPUTS, RESULTS, ROOT, SR, STEMS, load, load_track, save, sdr, tracks


def load_method(name):
    path = ROOT / "experiments" / "methods" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"method_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.separate


def run(separate, mix):
    est = separate(mix.copy(), SR)
    for s in STEMS:
        if s not in est:
            raise ValueError(f"method returned no '{s}' stem")
        if np.shape(est[s]) != mix.shape:
            raise ValueError(f"'{s}' has shape {np.shape(est[s])}, expected {mix.shape}")
        if not np.all(np.isfinite(est[s])):
            raise ValueError(f"'{s}' contains NaN/inf")
    return est


def score(separate, split, limit=None):
    rows = []
    start = time.perf_counter()
    for track_dir in tracks(split)[:limit]:
        mix, refs = load_track(track_dir)
        est = run(separate, mix)
        rows.append({"track": track_dir.name, **{s: sdr(refs[s], est[s]) for s in STEMS}})
    seconds = time.perf_counter() - start
    summary = {s: float(np.nanmedian([r[s] for r in rows])) for s in STEMS}
    summary["mean"] = float(np.mean([summary[s] for s in STEMS]))
    return summary, rows, seconds


def render_songs(name, separate):
    for path in sorted(EXCERPTS.glob("*.wav")):
        est = run(separate, load(path))
        for s in STEMS:
            save(OUTPUTS / name / path.stem / f"{s}{LOSSLESS_EXT}", est[s])
        print(f"wrote {OUTPUTS / name / path.stem}/")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("method")
    parser.add_argument("--split", default="train", choices=["train", "test"])
    parser.add_argument("--limit", type=int, help="only the first N clips (quick checks)")
    parser.add_argument("--song", action="store_true", help="also render the song excerpts")
    args = parser.parse_args()

    separate = load_method(args.method)
    summary, rows, seconds = score(separate, args.split, args.limit)
    print(f"{args.method} on {args.split} ({len(rows)} clips, {seconds:.0f} s): median SDR dB  "
          + "  ".join(f"{k} {v:6.2f}" for k, v in summary.items()))

    if args.limit is None:
        RESULTS.mkdir(exist_ok=True)
        out = RESULTS / f"{args.method}_{args.split}.json"
        out.write_text(json.dumps({"method": args.method, "split": args.split, "seconds": seconds,
                                   "median_sdr": summary, "per_track": rows}, indent=1))
    if args.song:
        render_songs(args.method, separate)


if __name__ == "__main__":
    main()
