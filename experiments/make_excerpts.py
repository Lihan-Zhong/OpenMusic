"""Run Demucs on each full song in songs/, save its stems, and cut a 60 s listening excerpt.

The excerpt is the 60 s window with the most vocal energy among seconds where drums are playing,
so every method gets judged on a stretch that has vocals, drums and band together.
Writes outputs/demucs_ai_full/<song>/<stem>.mp3 (.flac with --lossless) and
data/song_excerpts/<song>.wav (+ excerpts.json).
"""
import argparse
import importlib.util
import json
import time

import numpy as np

from common import EXCERPTS, LOSSLESS_EXT, OUTPUTS, PRODUCT_EXT, ROOT, SR, STEMS, load, save

WINDOW_S = 60
AUDIO = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"}


def per_second_energy(y):
    n = y.shape[1] // SR
    return (y[:, : n * SR].reshape(2, n, SR) ** 2).mean(axis=(0, 2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lossless", action="store_true", help="write full-song stems as FLAC instead of MP3")
    ext = LOSSLESS_EXT if parser.parse_args().lossless else PRODUCT_EXT

    spec = importlib.util.spec_from_file_location("demucs_ai", ROOT / "experiments/methods/demucs_ai.py")
    demucs_ai = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demucs_ai)

    info = {}
    for path in sorted(p for p in (ROOT / "songs").iterdir() if p.suffix.lower() in AUDIO):
        mix = load(path)
        start = time.perf_counter()
        est = demucs_ai.separate(mix, SR)
        took = time.perf_counter() - start
        for s in STEMS:
            save(OUTPUTS / "demucs_ai_full" / path.stem / f"{s}{ext}", est[s])

        vocals, drums = per_second_energy(est["vocals"]), per_second_energy(est["drums"])
        playing = drums > 0.1 * np.median(drums[drums > 0]) if np.any(drums > 0) else np.ones_like(drums, bool)
        weight = vocals * playing
        n = len(weight)
        if n <= WINDOW_S:
            begin = 0
        else:
            sums = np.convolve(weight, np.ones(WINDOW_S), mode="valid")
            begin = int(np.argmax(sums))
        clip = mix[:, begin * SR: (begin + WINDOW_S) * SR]
        save(EXCERPTS / f"{path.stem}.wav", clip)
        info[path.stem] = {"source": path.name, "start_s": begin, "length_s": clip.shape[1] / SR,
                           "duration_s": mix.shape[1] / SR, "demucs_full_song_seconds": round(took, 1)}
        print(f"{path.name}: {mix.shape[1] / SR:.0f} s song, Demucs took {took:.1f} s, excerpt starts at {begin} s")
    (EXCERPTS / "excerpts.json").write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
