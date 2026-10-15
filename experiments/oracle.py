"""Ceiling for frequency-masking methods: the ideal ratio mask, computed from the true stems.

Every signal-processing method we try works by deciding, for each time-frequency cell of the
spectrogram, how much belongs to each stem. This "cheats" by reading the answer, so it shows the
best any such mask could do. Writes results/oracle_irm_<split>.json.
"""
import argparse
import json

import librosa
import numpy as np

from common import RESULTS, STEMS, load_track, sdr, tracks

N_FFT, HOP = 4096, 1024


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="test", choices=["train", "test"])
    args = parser.parse_args()

    rows = []
    for track_dir in tracks(args.split):
        mix, refs = load_track(track_dir)
        X = librosa.stft(mix, n_fft=N_FFT, hop_length=HOP)
        power = {s: np.abs(librosa.stft(refs[s], n_fft=N_FFT, hop_length=HOP)) ** 2 for s in STEMS}
        total = sum(power.values()) + 1e-10
        row = {"track": track_dir.name}
        for s in STEMS:
            est = librosa.istft(X * power[s] / total, hop_length=HOP, length=mix.shape[1])
            row[s] = sdr(refs[s], est)
        rows.append(row)
    summary = {s: float(np.nanmedian([r[s] for r in rows])) for s in STEMS}
    summary["mean"] = float(np.mean([summary[s] for s in STEMS]))
    print("oracle_irm:", "  ".join(f"{k} {v:6.2f}" for k, v in summary.items()))
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"oracle_irm_{args.split}.json").write_text(
        json.dumps({"method": "oracle_irm", "split": args.split, "median_sdr": summary, "per_track": rows}, indent=1))


if __name__ == "__main__":
    main()
