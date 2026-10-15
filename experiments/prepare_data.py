"""Decode the MUSDB18 7-second sample (data/musdb7/*/*.stem.mp4) into 3-stem ground truth.

Writes data/gt/<split>/<track>/{mixture,vocals,drums,other}.wav, where other = bass + other and
mixture is the exact sum of the three stems. All 50 test clips are kept; the first 40 train clips
are kept for tuning.
"""
import av
import numpy as np

from common import DATA, GT, SR, save

STREAMS = {"drums": 1, "bass": 2, "other": 3, "vocals": 4}  # MUSDB stem order; stream 0 is the AAC mix
TRAIN_CLIPS = 40


def decode(path, index):
    with av.open(str(path)) as container:
        stream = container.streams.audio[index]
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
        chunks = []
        for frame in container.decode(stream):
            chunks += [f.to_ndarray() for f in resampler.resample(frame)]
        chunks += [f.to_ndarray() for f in resampler.resample(None)]
    return np.concatenate(chunks, axis=1)


def main():
    for split, limit in (("test", None), ("train", TRAIN_CLIPS)):
        files = sorted((DATA / "musdb7" / split).glob("*.stem.mp4"))[:limit]
        for f in files:
            raw = {k: decode(f, i) for k, i in STREAMS.items()}
            n = min(x.shape[1] for x in raw.values())
            stems = {
                "vocals": raw["vocals"][:, :n],
                "drums": raw["drums"][:, :n],
                "other": raw["bass"][:, :n] + raw["other"][:, :n],
            }
            mix = sum(stems.values())
            # Scale mix and stems together so nothing clips in 16-bit; SDR is unaffected.
            peak = max(np.abs(x).max() for x in [mix, *stems.values()])
            gain = min(1.0, 0.99 / peak) if peak > 0 else 1.0
            out = GT / split / f.name.removesuffix(".stem.mp4")
            save(out / "mixture.wav", mix * gain)
            for name, y in stems.items():
                save(out / f"{name}.wav", y * gain)
        print(f"{split}: {len(files)} clips")


if __name__ == "__main__":
    main()
