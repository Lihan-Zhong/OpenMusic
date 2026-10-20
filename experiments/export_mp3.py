"""Convert finished stems (WAV or FLAC) to MP3 for practice, keeping the folder layout.

    python experiments/export_mp3.py <source folder> <destination folder>
    python experiments/export_mp3.py outputs/_share/roformer_sw_full_songs outputs/mp3

<source>/<song>/<stem>.flac becomes <destination>/<song>/<stem>.mp3 (LAME VBR, about V2); the folder
and file names say which song and stem it is. Keep the lossless originals for comparing methods; the MP3s are for listening.
"""
import argparse
from pathlib import Path

from common import PRODUCT_EXT, load, read_stems_info, save, write_stems_info

SOURCE_EXT = {".wav", ".flac"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()

    files = sorted(p for p in args.source.rglob("*") if p.suffix.lower() in SOURCE_EXT)
    if not files:
        raise SystemExit(f"no .wav or .flac files under {args.source}")
    before = after = 0
    for src in files:
        dst = (args.destination / src.relative_to(args.source)).with_suffix(PRODUCT_EXT)
        save(dst, load(src))
        before += src.stat().st_size
        after += dst.stat().st_size
        print(f"{dst}  {dst.stat().st_size / 1e6:.1f} MB")
    for folder in sorted({src.parent for src in files}):
        info = read_stems_info(folder)
        if info:  # keep the record of which model made these stems
            info = {k: v for k, v in info.items() if k != "version"}
            info.update(format=PRODUCT_EXT.lstrip("."), converted_from=folder.as_posix())
            write_stems_info(args.destination / folder.relative_to(args.source), **info)
    print(f"{len(files)} files: {before / 1e6:.0f} MB -> {after / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
