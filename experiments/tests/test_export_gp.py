"""Check experiments/export_gp.py on a synthetic song whose notes are known, and check exported songs' files.

    python experiments/tests/test_export_gp.py
    python experiments/tests/test_export_gp.py --songs outputs/mp3   # also check every <song>.gp5 there

The synthetic song (outputs/_export_gp_test/<song>/, git-ignored, so the .gp5 can be opened by hand as well)
is an analysis.json at a steady 120 BPM with a 2-beat pickup, 4/4 bars and one 3/4 bar, chords and a key, plus
notes.json files with timing jitter (+-20 ms): guitar open chords (E, Am, C, G), an F barre chord, an 8th-note
run high on the neck, 16ths, an 8th-note triplet, a power chord, and ghost notes (soft, an overtone, one out of
range); a bass line; a vocal melody; an arpeggio on "other"; a few notes on "piano" (too few: skipped); and a
drums.mid. The .gp5 is read back with PyGuitarPro and every note must be on its beat with its pitch, the ghosts
gone, the chord shapes the ones a guitarist would use, no fretted stretch wider than the track allows, and the
triplet written as one; its title must be plain ASCII (pinyin). The .gp is read back with alphaTab (same
title in Chinese, tracks, bars, time signatures, tempo and notes as the .gp5) and its score.gpif XML with
Python's own parser (title, bars, tracks, chord names without diagrams, tempo changes as steps). A few small
cases test the rhythm grid, the note values, the clean-up and the ASCII titles on their own.
Exits with status 1 on any failure.

--songs checks every <song>/transcription/<song>.gp5 and .gp under a stems folder: they read back, have the bars
and time signatures of the song's tempo map (so bar N matches the MIDI and the player), full bars, notes on
distinct strings within the track's frets and hand span, a drum track exactly when drums.mid exists, the same
notes in both files, and the song's real title in the .gp.
"""
import argparse
import json
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import guitarpro as gp
import mido
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import export_gp  # noqa: E402
import gp7  # noqa: E402
from common import OUTPUTS  # noqa: E402
from transcribe import read_analysis  # noqa: E402

TEST_ROOT = OUTPUTS / "_export_gp_test"
SONG = "Test Band - 测试歌"
ENCODING = export_gp.GP5_ENCODING
RNG = np.random.default_rng(0)
JITTER_S = 0.02
TICKS = export_gp.TICKS
FAILURES = []


def expect(ok, message):
    if not ok:
        FAILURES.append(message)
        print("  FAIL", message)
    return ok


# ---------- the synthetic song ----------

PERIOD = 0.5                                   # 120 BPM: MIDI beat b is at 0.5 * b seconds
BAR_STARTS = [2, 6, 10, 14, 18, 21, 25, 29, 33, 37, 41, 45, 49, 53, 57, 61, 65, 69, 73, 77]   # 18-21 is 3/4
N_BEATS = 80
CHORDS = [(2, 3, "E"), (3, 4, "Am"), (4, 5, "C"), (5, 6, "G"), (6, 10, "N"), (10, 12, "F"), (12, 13, "N"),
          (13, 14, "A5"), (14, 80, "N")]
EXPECTED_CHORDS = {2: "E", 3: "Am", 4: "C", 5: "G", 6: "N.C.", 10: "F", 13: "A5", 14: "N.C."}
SHAPES = {   # beat: pitches, frets from string 6 (low E) to string 1 (-1 = not played)
    2: ((40, 47, 52, 56, 59, 64), (0, 2, 2, 1, 0, 0)),      # E
    3: ((45, 52, 57, 60, 64), (-1, 0, 2, 2, 1, 0)),         # Am
    4: ((48, 52, 55, 60, 64), (-1, 3, 2, 0, 1, 0)),         # C
    5: ((43, 47, 50, 55, 59, 67), (3, 2, 0, 0, 0, 3)),      # G
    10: ((41, 48, 53, 57, 60, 65), (1, 3, 3, 2, 1, 1)),     # F barre, a half note
}
HIGH_RUN = [69, 71, 72, 74, 76, 74, 72, 71]                 # 8ths on beats 6-9: no open strings fit


def analysis():
    beats = [PERIOD * b for b in range(1, N_BEATS)]
    return {"version": 2, "song": SONG, "duration_s": PERIOD * N_BEATS,
            "tempo": {"bpm": 120.0, "beats": beats, "downbeats": [PERIOD * b for b in BAR_STARTS],
                      "beats_per_bar": 4},
            "key": {"tonic": "A", "mode": "minor", "label_en": "A minor", "label_zh": "A 小调"},
            "chords": {"segments": [{"start": PERIOD * a, "end": PERIOD * b, "label": label}
                                    for a, b, label in CHORDS]}}


def note(beat, length, pitch, velocity=95):
    """A note at a MIDI beat, as transcribe.py writes it: seconds with a little timing jitter."""
    a = PERIOD * beat + RNG.uniform(-JITTER_S, JITTER_S)
    b = PERIOD * (beat + length) + RNG.uniform(-JITTER_S, JITTER_S)
    return [round(a, 3), round(b, 3), pitch, int(velocity + RNG.integers(-5, 6))]


def song_parts():
    """{stem: (notes, {(tick, pitch) expected in the file}, ghosts [(tick, pitch)])} and the drum hits."""
    guitar, want, ghosts = [], set(), set()
    for beat, (pitches, _) in SHAPES.items():
        length = 2 if beat == 10 else 1
        guitar += [note(beat, length, p) for p in pitches]
        want |= {(beat * TICKS, p) for p in pitches}
    for i, p in enumerate(HIGH_RUN):
        guitar.append(note(6 + i / 2, 0.5, p))
        want.add((round((6 + i / 2) * TICKS), p))
    for i, p in enumerate((64, 65, 67, 69)):                 # 16ths on beat 14
        guitar.append(note(14 + i / 4, 0.25, p))
        want.add((round((14 + i / 4) * TICKS), p))
    for i, p in enumerate((71, 72, 74)):                     # an 8th-note triplet on beat 15
        guitar.append(note(15 + i / 3, 1 / 3, p))
        want.add((15 * TICKS + i * TICKS // 3, p))
    for p in (45, 52, 57):                                   # A5 power chord on beat 13
        guitar.append(note(13, 1, p))
        want.add((13 * TICKS, p))
    guitar.append(note(12.5, 0.5, 64))                       # a syncopated 8th
    want.add((round(12.5 * TICKS), 64))
    for beat, pitch, velocity in ((2, 76, 50), (13, 69, 40), (7.75, 80, 25), (16, 30, 95)):
        guitar.append(note(beat, 0.5, pitch, velocity))      # overtone ghosts, a soft note, out of range
        ghosts.add((round(beat * TICKS), pitch))

    bass, bass_want = [], set()
    line = [28, 33, 38, 43, 40, 45, 47, 45, 43, 42, 40, 35]
    for i, p in enumerate(line):
        bass.append(note(2 + i, 1, p))
        bass_want.add(((2 + i) * TICKS, p))
    for i, p in enumerate((40, 40, 47, 40, 52, 40, 47, 45)):  # 8ths
        bass.append(note(21 + i / 2, 0.5, p))
        bass_want.add((round((21 + i / 2) * TICKS), p))

    vocals, vocal_want = [], set()
    for beat, length, p in ((25, 1, 69), (26, 0.5, 72), (26.5, 0.5, 71), (27, 1.5, 69), (29, 1, 67), (30, 2, 64)):
        vocals.append(note(beat, length * 0.8, p))           # sung notes end early: legato closes the gaps
        vocal_want.add((round(beat * TICKS), p))

    other, other_want = [], set()
    for i in range(80):
        beat, p = 33 + i / 2, (57, 60, 64, 69)[i % 4]
        other.append(note(beat, 0.5, p))
        other_want.add((round(beat * TICKS), p))
    piano = [note(41 + i, 1, 60 + i) for i in range(10)]

    drums, drum_want = [], set()
    for bar in BAR_STARTS[1:6]:
        n = 3 if bar == 18 else 4
        for k in range(2 * n):
            drums.append((bar + k / 2, 42))
        drums += [(bar, 36), (bar + 1, 38), (bar + 2, 36)] + ([(bar + 3, 38)] if n == 4 else [])
    drum_want = {(round(b * TICKS), key) for b, key in drums}
    return ({"guitar": (guitar, want, ghosts), "bass": (bass, bass_want, set()),
             "vocals": (vocals, vocal_want, set()), "other": (other, other_want, set()),
             "piano": (piano, set(), set())}, drums, drum_want)


def write_drums(path, hits):
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    events = sorted([(round(b * 480), 1, key) for b, key in hits] + [(round(b * 480) + 60, 0, key) for b, key in hits])
    track.append(mido.MetaMessage("set_tempo", tempo=500000))
    now = 0
    for tick, on, key in events:
        track.append(mido.Message("note_on", note=key, velocity=100 if on else 0, channel=9, time=tick - now))
        now = tick
    mid.tracks.append(track)
    mid.save(str(path))


def write_song():
    song_dir = TEST_ROOT / SONG
    shutil.rmtree(song_dir, ignore_errors=True)
    tdir = song_dir / "transcription"
    tdir.mkdir(parents=True)
    (song_dir / "analysis.json").write_text(json.dumps(analysis(), ensure_ascii=False), encoding="utf-8")
    parts, drums, drum_want = song_parts()
    for stem, (notes, _, _) in parts.items():
        (tdir / f"{stem}.notes.json").write_text(json.dumps({"version": 1, "stem": stem, "notes": notes,
                                                              "method": "synthetic"}), encoding="utf-8")
    write_drums(tdir / "drums.mid", drums)
    return song_dir, parts, drum_want


# ---------- reading a .gp5 back ----------

def track_beats(track):
    """[(tick from the song start, gp.Beat)] for voice 1 of every bar."""
    out = []
    for measure in track.measures:
        tick = measure.start - TICKS
        for beat in measure.voices[0].beats:
            out.append((tick, beat))
            tick += beat.duration.time
    return out


def struck(track):
    """{(tick, pitch)} of the notes struck (not tied) in a track; drums give their GM key."""
    out = set()
    for tick, beat in track_beats(track):
        for n in beat.notes:
            if n.type == gp.NoteType.normal:
                out.add((tick, n.value if track.isPercussionTrack else n.realValue))
    return out


def fingering_problems(track, frets, span):
    """Beats whose notes share a string, use a fret out of range, or stretch wider than span frets."""
    problems = []
    for tick, beat in track_beats(track):
        notes = [n for n in beat.notes if n.type == gp.NoteType.normal]
        strings = [n.string for n in notes]
        fretted = [n.value for n in notes if n.value > 0]
        if len(set(strings)) < len(strings):
            problems.append(f"beat {tick / TICKS:g}: two notes on one string")
        if any(not 0 <= n.value <= frets for n in notes):
            problems.append(f"beat {tick / TICKS:g}: fret out of range")
        if span is not None and fretted and max(fretted) - min(fretted) > span:
            problems.append(f"beat {tick / TICKS:g}: stretch of {max(fretted) - min(fretted)} frets")
    return problems


def frets_at(track, tick):
    """Frets from string 6 to string 1 (-1 = not played) of the struck notes at a tick."""
    frets = [-1] * len(track.strings)
    for t, beat in track_beats(track):
        if t == tick:
            for n in beat.notes:
                frets[len(track.strings) - n.string] = n.value
    return tuple(frets)


# ---------- the checks ----------

def check_units():
    print("rhythm grid, note values, clean-up:")
    grid = export_gp.beat_grids(np.array([0, 0.25, 0.5, 0.75, 1, 1 + 1 / 3, 1 + 2 / 3, 2.38, 3.02, 3.36, 3.64]), 4)
    expect(list(grid[:4]) == [4, 3, 4, 3], f"grid of 16ths / triplet / one late 8th / jittery triplet: {grid[:4]}")
    for a, b, values in ((0, 4 * TICKS, [(1, False)]), (0, 720, [(8, True)]), (240, 1440, [(8, True), (8, False)]),
                         (480, 2400, [(4, True), (8, False)]), (0, 3 * TICKS, [(2, True)])):
        got = [(d.value, d.isDotted) for _, d in export_gp.straight_pieces(a, b, 0)]
        expect(got == values, f"straight {a}-{b}: {got}, expected {values}")
    got = [(d.value, d.tuplet.enters) for _, d in export_gp.triplet_pieces(TICKS + 320, 2 * TICKS)]
    expect(got == [(4, 3)], f"triplet quarter in the 2nd and 3rd third of a beat: {got}")
    got = [(d.value, d.tuplet.enters) for _, d in export_gp.triplet_pieces(0, 320)]
    expect(got == [(8, 3)], f"one triplet 8th: {got}")

    notes = [(0.0, 1.0, 45, 90), (0.0, 1.0, 57, 50), (0.0, 1.0, 64, 85), (1.0, 2.0, 50, 30)]
    kept, quiet, overtones = export_gp.drop_ghosts(notes, polyphonic=True)
    expect((quiet, overtones) == (1, 1) and [n[2] for n in kept] == [45, 64],
           f"ghosts: kept {[n[2] for n in kept]}, {quiet} soft, {overtones} overtone")
    for text, ascii in (("想想XiangXiang", "XiangXiang"), ("第二张专辑的intro", "Di Er Zhang Zhuan Ji De intro"),
                        ("中南海", "Zhong Nan Hai"), ("Björk – Jóga", "Bjork - Joga"),
                        ("测试歌（现场版）", "Ce Shi Ge (Xian Chang Ban)")):
        expect(export_gp.ascii_text(text) == ascii, f"ASCII title of {text}: {export_gp.ascii_text(text)!r}")
    bass = [(i, i + 0.5, 26 if i % 3 == 0 else 33, 90) for i in range(30)]
    strings, drop_d = export_gp.tuning(bass, export_gp.TRACKS["bass"])
    expect(drop_d and strings == (43, 38, 33, 26), f"bass with low Ds: tuning {strings}")
    strings, drop_d = export_gp.tuning(bass[1:3], export_gp.TRACKS["bass"])
    expect(not drop_d, "two bass notes are no reason for drop D")


def check_song():
    print(f"synthetic song: {TEST_ROOT / SONG}")
    song_dir, parts, drum_want = write_song()
    expect(export_gp.export_song(song_dir, force=True), "export_song wrote nothing")
    path = song_dir / "transcription" / f"{SONG}.gp5"
    song = gp.parse(str(path), encoding=ENCODING)

    expect((song.title, song.artist) == ("Ce Shi Ge", "Test Band"), f"title / artist: {song.title!r} / {song.artist!r}")
    key = song.measureHeaders[0].keySignature   # the song-level key of a GP5 file has no major / minor
    expect(key == gp.KeySignature.AMinor and song.key.value[0] == 0, f"key {key}")
    expect("pickup" in song.instructions, f"instructions should mention the pickup: {song.instructions!r}")
    expect(song.tempo == 120, f"tempo {song.tempo}")
    signatures = [h.timeSignature.numerator for h in song.measureHeaders]
    bars = [2] + [b - a for a, b in zip(BAR_STARTS, BAR_STARTS[1:])] + [4]
    expect(signatures == bars, f"bars {signatures}, expected {bars}")
    names = [t.name for t in song.tracks]
    expect(names == ["Guitar", "Bass", "Vocals (melody)", "Other", "Drums"], f"tracks {names} (piano: too few notes)")
    tracks = {t.name: t for t in song.tracks}

    for stem, name in (("guitar", "Guitar"), ("bass", "Bass"), ("vocals", "Vocals (melody)"), ("other", "Other")):
        _, want, ghosts = parts[stem]
        got = struck(tracks[name])
        expect(got == want, f"{stem}: missing {sorted(want - got)[:6]}, extra {sorted(got - want)[:6]}")
        expect(not got & ghosts, f"{stem}: ghost notes kept: {sorted(got & ghosts)}")
    got = struck(tracks["Drums"])
    expect(got == drum_want, f"drums: missing {sorted(drum_want - got)[:6]}, extra {sorted(got - drum_want)[:6]}")
    expect(tracks["Drums"].isPercussionTrack and tracks["Drums"].channel.channel == 9, "drums on MIDI channel 10")
    expect(not any(n.type == gp.NoteType.tie for _, b in track_beats(tracks["Drums"]) for n in b.notes),
           "drum hits should never be tied")

    for name, frets, span in (("Guitar", 22, 4), ("Bass", 20, 3)):
        problems = fingering_problems(tracks[name], frets, span)
        expect(not problems, f"{name}: {problems[:5]}")
    expect([s.value for s in tracks["Guitar"].strings] == [64, 59, 55, 50, 45, 40], "guitar in standard tuning")
    expect([s.value for s in tracks["Bass"].strings] == [43, 38, 33, 28], "bass in standard tuning (4 strings)")
    expect(tracks["Guitar"].settings.tablature and not tracks["Vocals (melody)"].settings.tablature,
           "tab for guitar, notation only for vocals")
    for beat, (_, shape) in SHAPES.items():
        got = frets_at(tracks["Guitar"], beat * TICKS)
        expect(got == shape, f"chord shape at beat {beat}: {got}, expected {shape}")
    run = [n.value for t, b in track_beats(tracks["Guitar"]) if 6 * TICKS <= t < 10 * TICKS for n in b.notes
           if n.type == gp.NoteType.normal]
    expect(max(run) - min(run) <= 5 and min(run) > 0, f"8th-note run should stay in one position: frets {run}")

    triplet = [b.duration for t, b in track_beats(tracks["Guitar"]) if 15 * TICKS <= t < 16 * TICKS]
    expect(len(triplet) == 3 and all(d.value == 8 and d.tuplet == gp.Tuplet(3, 2) for d in triplet),
           f"beat 15 should be three triplet 8ths: {[(d.value, d.tuplet.enters) for d in triplet]}")
    sixteenths = [b.duration.value for t, b in track_beats(tracks["Guitar"]) if 14 * TICKS <= t < 15 * TICKS]
    expect(sixteenths == [16] * 4, f"beat 14 should be four 16ths: {sixteenths}")
    vocal_rests = [b for t, b in track_beats(tracks["Vocals (melody)"])
                   if 25 * TICKS <= t < 31 * TICKS and b.status == gp.BeatStatus.rest]
    expect(not vocal_rests, f"the legato vocal phrase should have no rests: {len(vocal_rests)}")

    chords = {t // TICKS: b.effect.chord.name for t, b in track_beats(tracks["Guitar"]) if b.effect.chord}
    expect(chords == EXPECTED_CHORDS, f"chord symbols {chords}, expected {EXPECTED_CHORDS}")
    expect(not any(b.effect.chord for _, b in track_beats(tracks["Drums"])), "no chord symbols on drums")
    check_gp_file(path.with_suffix(".gp"), song, ("测试歌", "Test Band"))
    return path


def check_gp_file(path, song5, title_artist):
    """The .gp next to a .gp5: alphaTab reads the real title and artist and the same bars, tempo, tracks and
    notes; its score.gpif, parsed here independently, has the title, the bars, chord names without diagrams
    and tempo changes as steps."""
    name = path.parent.parent.name
    if not expect(path.is_file(), f"{name}: no .gp"):
        return
    got = gp7.summary(path.read_bytes())
    expect((got["title"], got["artist"]) == title_artist, f"{name}: .gp title / artist {got['title']!r} / {got['artist']!r}")
    expect(got["signatures"] == [h.timeSignature.numerator for h in song5.measureHeaders], f"{name}: .gp bars")
    expect(got["tempo"] == song5.tempo, f"{name}: .gp tempo {got['tempo']}, .gp5 {song5.tempo}")
    want = [(t.name, sum(n.type == gp.NoteType.normal for _, b in track_beats(t) for n in b.notes),
             t.isPercussionTrack, t.settings.tablature) for t in song5.tracks]
    have = [(t["name"], t["notes"], t["percussion"], t["tab"]) for t in got["tracks"]]
    expect(have == want, f"{name}: .gp tracks (name, notes, drums, tab) {have}, .gp5 {want}")
    for t5, t in zip(song5.tracks, got["tracks"]):
        if t5.isPercussionTrack:
            keys = {}
            for _, b in track_beats(t5):
                for n in b.notes:
                    if n.type == gp.NoteType.normal:
                        keys[str(n.value)] = keys.get(str(n.value), 0) + 1
            expect(t["kit"] == keys, f"{name}: .gp drum pieces {t['kit']}, .gp5 {keys}")

    with zipfile.ZipFile(path) as z:
        expect(z.read("VERSION") == b"7.0", f"{name}: .gp VERSION {z.read('VERSION')!r}")
        root = ET.fromstring(z.read("Content/score.gpif"))
    expect(root.findtext("Score/Title") == title_artist[0], f"{name}: gpif title {root.findtext('Score/Title')!r}")
    expect(len(root.findall("MasterBars/MasterBar")) == len(song5.measureHeaders), f"{name}: gpif bars")
    expect([t.findtext("Name") for t in root.findall("Tracks/Track")] == [t.name for t in song5.tracks],
           f"{name}: gpif tracks")
    shown = {e.get("value") for e in root.iter("Property") if e.get("name") == "ShowDiagram"}
    expect(shown <= {"false"}, f"{name}: chord diagrams shown: {shown}")
    tempos = [a for a in root.findall("MasterTrack/Automations/Automation") if a.findtext("Type") == "Tempo"]
    expect(all(a.findtext("Linear") == "false" for a in tempos) and [a.findtext("Bar") for a in tempos].count("0") == 1,
           f"{name}: tempo changes should be steps, bar 1 marked once")


def check_songs(stems_root):
    """Every exported .gp5 and .gp under stems_root against its song's tempo map; prints what is in each."""
    paths = sorted(p for p in Path(stems_root).glob("*/transcription/*.gp5") if p.stem == p.parent.parent.name)
    expect(paths, f"no exported .gp5 under {stems_root}")
    for path in paths:
        song_dir = path.parent.parent
        song = gp.parse(str(path), encoding=ENCODING)
        expect(song.title.isascii() and song.artist.isascii(), f"{song_dir.name}: .gp5 text should be ASCII")
        check_gp_file(path.with_suffix(".gp"), song, export_gp.split_name(song_dir.name))
        _, _, bars = export_gp.song_bars(read_analysis(song_dir), export_gp.read_notes(song_dir))
        got = [h.timeSignature.numerator for h in song.measureHeaders]
        expect(got == [n for _, n in bars], f"{song_dir.name}: bars differ from the tempo map")
        has_drums = any(t.isPercussionTrack for t in song.tracks)
        expect(has_drums == (song_dir / "transcription" / "drums.mid").is_file(), f"{song_dir.name}: drum track")
        n_chords = 0
        print(f"{song_dir.name}: {len(song.measureHeaders)} bars, {song.tempo} BPM, "
              f"key {song.measureHeaders[0].keySignature.name}, "
              f".gp5 title {song.title!r} by {song.artist!r}")
        for track in song.tracks:
            spec = export_gp.TRACKS.get(track.name.split(" ")[0].lower(), {})
            for measure in track.measures:
                if sum(b.duration.time for b in measure.voices[0].beats) != measure.length:
                    expect(False, f"{song_dir.name} / {track.name}: bar {measure.number} is not full")
            if not track.isPercussionTrack:
                problems = fingering_problems(track, spec.get("frets", 24), spec.get("span"))
                expect(not problems, f"{song_dir.name} / {track.name}: {problems[:5]}")
            beats = track_beats(track)
            notes = sum(n.type == gp.NoteType.normal for _, b in beats for n in b.notes)
            n_chords = max(n_chords, sum(b.effect.chord is not None for _, b in beats))
            tuplets = sum(b.duration.tuplet != gp.Tuplet() for _, b in beats)
            tuning = " ".join(str(s) for s in track.strings) if not track.isPercussionTrack else "drums"
            print(f"  {track.name}: {notes} notes, {tuplets} triplet notes/rests, tuning {tuning}, "
                  f"{'tab + notation' if track.settings.tablature else 'notation'}")
        print(f"  {n_chords} chord symbols")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--songs", type=Path, help="also check the exported .gp5 files under this stems folder")
    args = parser.parse_args()
    check_units()
    check_song()
    if args.songs:
        check_songs(args.songs)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
