"""Re-score methods on the test split from scratch and compare with the stored results/<method>_test.json.

Nothing is written; this only checks that the recorded numbers reproduce.
    python experiments/verify_scores.py stereo hpss combo
"""
import json
import sys

from common import RESULTS
from evaluate import load_method, score


def main():
    ok = True
    for name in sys.argv[1:]:
        stored = json.loads((RESULTS / f"{name}_test.json").read_text())["median_sdr"]
        fresh, rows, seconds = score(load_method(name), "test")
        diff = max(abs(fresh[k] - stored[k]) for k in stored)
        ok &= diff < 0.01
        print(f"{name:11s} stored mean {stored['mean']:6.2f}  re-run mean {fresh['mean']:6.2f}  "
              f"max diff {diff:.4f}  ({len(rows)} clips, {seconds:.0f} s)  {'OK' if diff < 0.01 else 'MISMATCH'}", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
