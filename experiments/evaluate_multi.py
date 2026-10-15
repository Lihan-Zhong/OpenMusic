"""Score multi-stem separators (with bass, guitar, piano...) and render them on the songs.

A method is experiments/methods_multi/<name>.py exposing separate(mix, sr) -> dict of stems, each
shaped like mix (2, samples). Any of: vocals, drums, bass, guitar, piano, other.

Scoring uses MUSDB's 4 stems: vocals, drums, bass and other4. MUSDB has no guitar or piano stem,
so guitar + piano + other are summed into other4 for scoring. Guitar is judged by ear only.

    python experiments/evaluate_multi.py demucs6 --split test --song     # 50 test clips + 60 s excerpts
    python experiments/evaluate_multi.py demucs6 --full                  # also split every full song in songs/
"""
import argparse
import importlib.util
import json
import time

import numpy as np

from common import EXCERPTS, OUTPUTS, RESULTS, ROOT, SR, load, save, sdr, tracks

SCORED = ("vocals", "drums", "bass", "other4")
AUDIO = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"}


def load_method(name):
    path = ROOT / "experiments" / "methods_multi" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"multi_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.separate


def run(separate, mix):
    est = separate(mix.copy(), SR)
    for k, v in est.items():
        if np.shape(v) != mix.shape or not np.all(np.isfinite(v)):
            raise ValueError(f"bad stem '{k}': shape {np.shape(v)}")
    return est


def to_scored(est):
    rest = [v for k, v in est.items() if k not in ("vocals", "drums", "bass")]
    return {"vocals": est["vocals"], "drums": est["drums"], "bass": est["bass"], "other4": sum(rest)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("method")
    parser.add_argument("--split", default="test", choices=["train", "test"])
    parser.add_argument("--song", action="store_true", help="render the 60 s song excerpts")
    parser.add_argument("--full", action="store_true", help="render every full song in songs/")
    args = parser.parse_args()
    separate = load_method(args.method)

    rows, start = [], time.perf_counter()
    for track_dir in tracks(args.split):
        mix = load(track_dir / "mixture.wav")
        est = to_scored(run(separate, mix))
        rows.append({"track": track_dir.name,
                     **{s: sdr(load(track_dir / f"{s}.wav"), est[s]) for s in SCORED}})
    seconds = time.perf_counter() - start
    summary = {s: float(np.nanmedian([r[s] for r in rows])) for s in SCORED}
    summary["mean"] = float(np.mean([summary[s] for s in SCORED]))
    print(f"{args.method} on {args.split} ({len(rows)} clips, {seconds:.0f} s): median SDR dB  "
          + "  ".join(f"{k} {v:6.2f}" for k, v in summary.items()))
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"multi_{args.method}_{args.split}.json").write_text(json.dumps(
        {"method": args.method, "split": args.split, "seconds": seconds, "median_sdr": summary,
         "per_track": rows}, indent=1))

    if args.song:
        for path in sorted(EXCERPTS.glob("*.wav")):
            for k, v in run(separate, load(path)).items():
                save(OUTPUTS / args.method / path.stem / f"{k}.wav", v)
            print(f"wrote {OUTPUTS / args.method / path.stem}/")
    if args.full:
        for path in sorted(p for p in (ROOT / "songs").iterdir() if p.suffix.lower() in AUDIO):
            mix = load(path)
            t0 = time.perf_counter()
            est = run(separate, mix)
            took = time.perf_counter() - t0
            for k, v in est.items():
                save(OUTPUTS / f"{args.method}_full" / path.stem / f"{k}.wav", v)
            print(f"{path.name}: {mix.shape[1] / SR:.0f} s song split in {took:.1f} s")


if __name__ == "__main__":
    main()
