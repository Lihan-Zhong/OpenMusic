"""Add bass ground truth next to the 3-stem files: data/gt/<split>/<track>/{bass,other4}.wav.

other4 = the MUSDB "other" stem without bass (guitar, keys, synths...). MUSDB has no separate guitar
stem, so guitar can only be judged by ear. Uses the same gain as prepare_data.py so the files line up.
"""
import numpy as np

from common import DATA, GT, save
from prepare_data import STREAMS, TRAIN_CLIPS, decode


def main():
    for split, limit in (("test", None), ("train", TRAIN_CLIPS)):
        files = sorted((DATA / "musdb7" / split).glob("*.stem.mp4"))[:limit]
        for f in files:
            raw = {k: decode(f, i) for k, i in STREAMS.items()}
            n = min(x.shape[1] for x in raw.values())
            raw = {k: v[:, :n] for k, v in raw.items()}
            three = [raw["vocals"], raw["drums"], raw["bass"] + raw["other"]]
            peak = max(np.abs(x).max() for x in [sum(three), *three])
            gain = min(1.0, 0.99 / peak) if peak > 0 else 1.0
            out = GT / split / f.name.removesuffix(".stem.mp4")
            save(out / "bass.wav", raw["bass"] * gain)
            save(out / "other4.wav", raw["other"] * gain)
        print(f"{split}: {len(files)} clips")


if __name__ == "__main__":
    main()
