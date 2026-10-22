"""Check experiments/export_gp.py on a synthetic song whose notes are known, and check exported songs' files.

    python experiments/tests/test_export_gp.py
    python experiments/tests/test_export_gp.py --songs outputs/mp3   # also check every <song>.gp there

The synthetic song (outputs/_export_gp_test/<song>/, git-ignored, so the .gp can be opened by hand as well)
is an analysis.json at a steady 120 BPM with a 2-beat pickup, 4/4 bars and one 3/4 bar, chords and a key, plus
notes.json files with timing jitter (+-20 ms): guitar open chords (E, Am, C, G), an F barre chord, an 8th-note
run high on the neck, 16ths, an 8th-note triplet, a power chord, and ghost notes (soft, an overtone, one out of
range); a bass line; a vocal melody; an arpeggio on "other"; a few notes on "piano" (too few: skipped); and a
drums.mid; and an "original" recording (a click per beat in songs/ of the test folder) and five short stems, each
a tone of its own (vocals 440 Hz, guitar 660, other 990, bass 110, drums 1760). The .gp is read back with alphaTab
(gp7.dump) and must have the Chinese title, every note on its beat with its pitch, the ghosts gone, the chord
shapes a guitarist would use, no fretted stretch wider than the track allows, the triplet written as one, the
drums on their kit pieces and never tied, the chord names on their beats, only the guitar muted, and as its
audio track the default mix (vocals + guitar + other, so their tones and not bass or drums, scaled to about
-1 dBFS rather than clipping) with a sync point at the start of every bar. Its score.gpif is also parsed with
Python's own XML parser (title, bars, tracks, chord names without diagrams, tempo changes as steps, the audio
track, sync points in samples, muted tracks). The other audio choices are tried too: --stems with a subset (a
lone stem is not boosted), with a stem the song lacks, and with only missing stems (no audio track); --audio
original (the recording byte for byte, or all stems mixed without it) and none. The command line is run as the
workbench runs it: --out writes the file elsewhere and leaves the song folder exactly as it was, and errors give
a non-zero exit code and one line on stderr. A few small cases test the rhythm grid, the note values and the
clean-up on their own. Exits with status 1 on any failure.

--songs checks every <song>/transcription/<song>.gp under a stems folder: it reads back with the song's real
title, has the bars and time signatures of the song's tempo map (so bar N matches the MIDI and the player), bars
that add up, notes on distinct strings within the track's frets and hand span, a drum track exactly when
drums.mid exists, only the guitar muted, the default audio track (vocals + guitar + other mixed into an MP3)
with a sync point on every bar start, and no .gp5 left next to it.
"""
import argparse
import io
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import mido
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import export_gp  # noqa: E402
import gp7  # noqa: E402
from common import OUTPUTS  # noqa: E402
from transcribe import read_analysis  # noqa: E402

TEST_ROOT = OUTPUTS / "_export_gp_test"
STEM_TONES = {"vocals": 440, "guitar": 660, "other": 990, "bass": 110, "drums": 1760}   # Hz, one per stem
STEM_LEVEL = 0.45                  # each stem's peak: vocals + guitar + other would sum to 1.35
SONG = "Test Band - 测试歌"
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
    # snare double strokes a 32nd apart, a little early and late (Crayon Case - Surabaya): on 16ths one would
    # merge into one hit, the other put its second hit on the next beat; on 32nds both stay where they were
    for first, second in ((13.72, 13.85), (9.73, 9.88)):
        drums += [(first, 38), (second, 38)]
        drum_want |= {(round(round(first * 8) / 8 * TICKS), 38), (round(round(second * 8) / 8 * TICKS), 38)}
    # in an otherwise empty bar: a kick that would last into a tie, a double stroke a little ahead (both hits
    # round to x.75), a kick struck with its 2nd stroke, and a crash a little before the next bar
    hits = [(25.0, 36), (25.25, 36), (25.70, 38), (25.80, 38), (25.80, 36), (26.91, 49)]
    hits += [(26.25, 38), (26.375, 38)]                     # puts beat 26 on 32nds: the crash still goes to 27
    drums += hits
    drum_want |= {(round(b * TICKS), key) for b, key in [(25.0, 36), (25.25, 36), (25.75, 38), (25.875, 38),
                                                         (25.875, 36), (27.0, 49), (26.25, 38), (26.375, 38)]}
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
    # the "original" recording: a click on every beat, so its sync points have an audible reference
    sr, n = 8000, int(PERIOD * N_BEATS * 8000)
    click = np.zeros(n, np.float32)
    for b in range(N_BEATS):
        i = int(b * PERIOD * sr)
        click[i:i + 80] = 0.5 * np.sin(2 * np.pi * 1000 * np.arange(len(click[i:i + 80])) / sr)
    songs = TEST_ROOT / "songs"
    songs.mkdir(parents=True, exist_ok=True)
    sf.write(str(songs / f"{SONG}.wav"), click, sr, subtype="PCM_16")
    for stem, hz in STEM_TONES.items():   # 2 s stems, one tone each, loud enough that three of them would clip
        sf.write(str(song_dir / f"{stem}.wav"), STEM_LEVEL * np.sin(2 * np.pi * hz * np.arange(22050 * 2) / 22050),
                 22050, subtype="PCM_16")
    return song_dir, parts, drum_want


def tones(mp3):
    """{stem: True if its tone is in the decoded audio}, and the audio's peak."""
    audio, sr = sf.read(io.BytesIO(mp3), always_2d=True)
    spectrum = np.abs(np.fft.rfft(audio.mean(axis=1)))
    freqs = np.fft.rfftfreq(len(audio), 1 / sr)
    level = {stem: spectrum[np.abs(freqs - hz) < 5].max() for stem, hz in STEM_TONES.items()}
    return {stem: v > 0.1 * max(level.values()) for stem, v in level.items()}, float(np.abs(audio).max())


def embedded(path):
    """(name of the audio file, its bytes, OriginalFilePath) in a .gp, or (None, b"", None)."""
    with zipfile.ZipFile(path) as z:
        name = gp7.audio_entry(z)
        gpif = z.read("Content/score.gpif").decode()
        original = ET.fromstring(gpif).findtext("Assets/Asset/OriginalFilePath")
        return name, z.read(name) if name else b"", original


# ---------- reading a .gp back ----------

def struck(track):
    """{(tick, pitch)} of the notes struck (not tied) in a track of gp7.dump; drums give their GM key."""
    return {(b["tick"], n["pitch"]) for b in track["beats"] for n in b["notes"] if not n["tie"]}


def fingering_problems(track, frets, span):
    """Beats whose notes share a string, use a fret out of range, or stretch wider than span frets."""
    problems = []
    for beat in track["beats"]:
        notes = [n for n in beat["notes"] if not n["tie"]]
        strings = [n["string"] for n in notes]
        fretted = [n["fret"] for n in notes if n["fret"] > 0]
        where = f"beat {beat['tick'] / TICKS:g}"
        if len(set(strings)) < len(strings):
            problems.append(f"{where}: two notes on one string")
        if any(not 0 <= n["fret"] <= frets for n in notes):
            problems.append(f"{where}: fret out of range")
        if span is not None and fretted and max(fretted) - min(fretted) > span:
            problems.append(f"{where}: stretch of {max(fretted) - min(fretted)} frets")
    return problems


def unfilled_bars(song, track):
    """Bars (1-based) whose beats do not add up to the bar's length."""
    filled = {}
    for beat in track["beats"]:
        filled[beat["bar"]] = filled.get(beat["bar"], 0) + beat["ticks"]
    return [i + 1 for i, bar in enumerate(song["bars"]) if filled.get(i) != bar["ticks"]]


def frets_at(track, tick):
    """Frets from the lowest string to the highest (-1 = not played) of the notes struck at a tick."""
    frets = [-1] * len(track["tuning"])
    for beat in track["beats"]:
        if beat["tick"] == tick:
            for n in beat["notes"]:
                frets[n["string"] - 1] = n["fret"]
    return tuple(frets)


def beats_in(track, a, b):
    """The beats of a track starting in beats a..b (MIDI beats)."""
    return [beat for beat in track["beats"] if a * TICKS <= beat["tick"] < b * TICKS]


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
    snare = lambda *xs: export_gp.beat_grids(np.array(xs), 3, drums=[38] * len(xs))[:3].tolist()   # noqa: E731
    for xs, want, what in (((0.72, 0.85), [8, 4, 4], "a 32nd double stroke"),
                           ((1.73, 1.88), [4, 8, 4], "one whose 2nd hit is nearer the next beat"),
                           ((0.3083, 0.3979, 0.4854), [8, 4, 4], "a ruff 0.09 beat apart (草东没有派对 - 大风吹)"),
                           ((0.72, 0.75), [4, 4, 4], "a flam"), ((0.5, 0.75), [4, 4, 4], "two 16ths")):
        expect(snare(*xs) == want, f"drum grid for {what} {xs}: {snare(*xs)}, expected {want}")
    got = export_gp.beat_grids(np.array([0.72, 0.85]), 3, drums=[38, 36])[:3].tolist()
    expect(got == [4, 4, 4], f"drum grid for two drums a 32nd apart (they may share a 16th): {got}")
    for hits, want, what in (
            ([(0.70, 38), (0.80, 38)], [720, 840], "a double stroke both of whose hits round to x.75"),
            ([(0.25, 38), (0.375, 38), (0.91, 49)], [240, 360, 960], "a crash played a little before the next beat"),
            ([(0.72, 38), (0.86, 38), (0.86, 36)], [720, 840, 840], "a kick struck with the 2nd stroke"),
            ([(0.3083, 38), (0.3979, 38), (0.4854, 38)], [240, 360, 480], "a ruff"),
            # found in review: a stroke moved off the grid of a triplet beat hung triplet_pieces; strokes pushed one
            # after another dragged the hits on the next beat a 32nd late
            ([(0.95, 38), (1.02, 38), (1.333, 42), (1.667, 42)], [840, 960, 1280, 1600], "a drag into a triplet beat"),
            ([(0.70, 38), (0.78, 38), (0.86, 38), (0.94, 38), (1.0, 36), (1.0, 49)], [600, 720, 840, 960, 960, 960],
             "a roll into a kick and crash on the beat"),
            ([(0.82, 38), (0.91, 38), (1.0, 38), (1.0, 49)], [720, 840, 960, 960], "a ruff ending on the beat"),
            ([(0.338, 38), (0.428, 38), (0.515, 38), (0.5, 42)], [240, 360, 480, 480], "a ruff ending on the &")):
        on, keys = np.array([h[0] for h in hits]), [h[1] for h in hits]
        grid = export_gp.beat_grids(on, 3, drums=keys)
        got = export_gp.drum_ticks(on, keys, grid).tolist()
        expect(got == want and all(export_gp.on_grid(t, grid) for t in got),
               f"drum ticks for {what}: {got}, expected {want}")
    for seconds, want in (([1.0, 1.05], [(0, 1)]), ([1.0, 1.04], [])):   # 0.1 beat; 40 ms is a flam or one hit twice
        got = export_gp.drum_pairs([0.7, 0.8], [38, 38], seconds)
        expect(got == want, f"double strokes {seconds[1] - seconds[0]:.2f} s apart: {got}, expected {want}")
    try:
        export_gp.triplet_pieces(TICKS, TICKS + 120)
        expect(False, "triplet_pieces should refuse ticks off the triplet grid, not loop")
    except ValueError:
        pass
    for a, b, values in ((0, TICKS, [(4, False)]), (0, 720, [(8, True)]), (720, 840, [(32, False)]),
                         (240, TICKS, [(16, False), (8, False)]), (600, TICKS, [(32, False), (16, False)]),
                         (TICKS + 720, 2 * TICKS, [(16, False)])):
        got = [(d.value, d.isDotted) for _, d in export_gp.fine_pieces(a, b)]
        expect(got == values, f"32nds {a}-{b}: {got}, expected {values}")

    notes = [(0.0, 1.0, 45, 90), (0.0, 1.0, 57, 50), (0.0, 1.0, 64, 85), (1.0, 2.0, 50, 30)]
    kept, quiet, overtones = export_gp.drop_ghosts(notes, polyphonic=True)
    expect((quiet, overtones) == (1, 1) and [n[2] for n in kept] == [45, 64],
           f"ghosts: kept {[n[2] for n in kept]}, {quiet} soft, {overtones} overtone")
    bass = [(i, i + 0.5, 26 if i % 3 == 0 else 33, 90) for i in range(30)]
    strings, drop_d = export_gp.tuning(bass, export_gp.TRACKS["bass"])
    expect(drop_d and strings == (43, 38, 33, 26), f"bass with low Ds: tuning {strings}")
    strings, drop_d = export_gp.tuning(bass[1:3], export_gp.TRACKS["bass"])
    expect(not drop_d, "two bass notes are no reason for drop D")

    # a first beat a few ms in: the squeezed lead-in beat is left out of the score (草东没有派对 - 大风吹)
    def bars_for(first):
        beats = [first + 0.5 * k for k in range(40)]
        return export_gp.song_bars({"duration_s": 20.0, "tempo": {"bpm": 120.0, "beats": beats,
                                                                  "downbeats": beats[::4], "beats_per_bar": 4}}, {})
    knots, _, bars, skip = bars_for(0.042)
    expect(skip == 0.042 and bars[0] == (0, 4) and knots[0] == (0.042, 0.0)
           and abs(float(export_gp.beats_to_seconds(4, knots)) - 2.042) < 1e-9,
           f"squeezed lead-in: skip {skip}, bars {bars[:2]}, knots {knots[:2]}")
    knots, _, bars, skip = bars_for(0.4)                    # a real pickup beat (0.4 of 0.5 s) stays
    expect(skip == 0.0 and bars[0] == (0, 1) and bars[1] == (1, 4), f"real pickup: skip {skip}, bars {bars[:2]}")


def check_song():
    print(f"synthetic song: {TEST_ROOT / SONG}")
    song_dir, parts, drum_want = write_song()
    export_gp.SONGS = TEST_ROOT / "songs"
    expect(export_gp.export_song(song_dir, force=True), "export_song wrote nothing")
    tdir = song_dir / "transcription"
    expect(not list(tdir.glob("*.gp5")), "no .gp5 should be written")
    path = tdir / f"{SONG}.gp"
    song = gp7.dump(path.read_bytes())

    expect((song["title"], song["artist"]) == ("测试歌", "Test Band"), f"title / artist: {song['title']!r} / {song['artist']!r}")
    expect((song["bars"][0]["key"], song["bars"][0]["minor"]) == (0, True), f"key {song['bars'][0]}, expected A minor")
    expect("pickup" in song["instructions"], f"instructions should mention the pickup: {song['instructions']!r}")
    expect(song["tempo"] == 120, f"tempo {song['tempo']}")
    signatures = [b["numerator"] for b in song["bars"]]
    bars = [2] + [b - a for a, b in zip(BAR_STARTS, BAR_STARTS[1:])] + [4]
    expect(signatures == bars, f"bars {signatures}, expected {bars}")
    names = [t["name"] for t in song["tracks"]]
    expect(names == ["Guitar", "Bass", "Vocals (melody)", "Other", "Drums"], f"tracks {names} (piano: too few notes)")
    tracks = {t["name"]: t for t in song["tracks"]}
    for name, track in tracks.items():
        expect(not unfilled_bars(song, track), f"{name}: bars {unfilled_bars(song, track)[:5]} do not add up")

    for stem, name in (("guitar", "Guitar"), ("bass", "Bass"), ("vocals", "Vocals (melody)"), ("other", "Other")):
        _, want, ghosts = parts[stem]
        got = struck(tracks[name])
        expect(got == want, f"{stem}: missing {sorted(want - got)[:6]}, extra {sorted(got - want)[:6]}")
        expect(not got & ghosts, f"{stem}: ghost notes kept: {sorted(got & ghosts)}")
    drums = tracks["Drums"]
    got = struck(drums)
    expect(got == drum_want, f"drums: missing {sorted(drum_want - got)[:6]}, extra {sorted(got - drum_want)[:6]}")
    expect(drums["percussion"] and drums["channel"] == 9, "drums on MIDI channel 10")
    expect(not any(n["tie"] for b in drums["beats"] for n in b["notes"]), "drum hits should never be tied")

    for name, frets, span in (("Guitar", 22, 4), ("Bass", 20, 3)):
        problems = fingering_problems(tracks[name], frets, span)
        expect(not problems, f"{name}: {problems[:5]}")
    expect(tracks["Guitar"]["tuning"] == [64, 59, 55, 50, 45, 40], "guitar in standard tuning")
    expect(tracks["Bass"]["tuning"] == [43, 38, 33, 28], "bass in standard tuning (4 strings)")
    expect(tracks["Guitar"]["tab"] and tracks["Bass"]["tab"] and not tracks["Vocals (melody)"]["tab"],
           "tab for guitar and bass, notation only for vocals")
    for beat, (_, shape) in SHAPES.items():
        got = frets_at(tracks["Guitar"], beat * TICKS)
        expect(got == shape, f"chord shape at beat {beat}: {got}, expected {shape}")
    run = [n["fret"] for b in beats_in(tracks["Guitar"], 6, 10) for n in b["notes"] if not n["tie"]]
    expect(max(run) - min(run) <= 5 and min(run) > 0, f"8th-note run should stay in one position: frets {run}")

    triplet = beats_in(tracks["Guitar"], 15, 16)
    expect(len(triplet) == 3 and all(b["value"] == 8 and b["tuplet"] == [3, 2] for b in triplet),
           f"beat 15 should be three triplet 8ths: {[(b['value'], b['tuplet']) for b in triplet]}")
    sixteenths = [b["value"] for b in beats_in(tracks["Guitar"], 14, 15)]
    expect(sixteenths == [16] * 4, f"beat 14 should be four 16ths: {sixteenths}")
    vocal_rests = [b for b in beats_in(tracks["Vocals (melody)"], 25, 31) if b["rest"]]
    expect(not vocal_rests, f"the legato vocal phrase should have no rests: {len(vocal_rests)}")

    chords = {b["tick"] // TICKS: b["chord"] for b in tracks["Guitar"]["beats"] if b["chord"]}
    expect(chords == EXPECTED_CHORDS, f"chord symbols {chords}, expected {EXPECTED_CHORDS}")
    expect(not any(b["chord"] for b in drums["beats"]), "no chord symbols on drums")
    check_gpif(path, song)

    # the audio track: by default vocals + guitar + other mixed into an MP3, no louder than about -1 dBFS (their
    # sum would peak at 1.35), with a sync point at each bar start (0.5 s per beat)
    name, mix, original = embedded(path)
    heard, peak = tones(mix)
    expect(name and name.endswith(".mp3") and original == f"{SONG} (vocals+guitar+other).mp3",
           f"the default audio track should be the vocals+guitar+other mix: {name}, {original}")
    expect(heard == {"vocals": True, "guitar": True, "other": True, "bass": False, "drums": False},
           f"the default mix should hold vocals, guitar and other only: {heard}")
    expect(0.85 < peak < 0.95, f"the mix should be scaled to about -1 dBFS (0.89), not clip: peak {peak:.3f}")
    starts = [0] + BAR_STARTS
    expect([(b, round(ms, 3)) for b, _, _, ms in song["sync"]] == [(i, 500.0 * s) for i, s in enumerate(starts)],
           f"sync points {song['sync'][:4]}..., expected one per bar start")
    expect([t["name"] for t in song["tracks"] if t["mute"]] == ["Guitar"], "only the guitar should be muted")
    expect(not any(t["solo"] for t in song["tracks"]), "no track should be soloed")
    check_audio_modes(song_dir, path)
    return path


def check_audio_modes(song_dir, path):
    """--stems subsets (one stem alone is not boosted; a stem the song lacks is skipped, only missing ones mean no
    audio track); --audio original (the recording as it is, or all stems mixed without one); none; and the
    command line as the workbench uses it (--out, one-line errors)."""
    export_gp.export_song(song_dir, force=True, audio_stems=("bass",))
    name, mix, original = embedded(path)
    heard, peak = tones(mix)
    expect(original == f"{SONG} (bass).mp3" and heard == {**dict.fromkeys(STEM_TONES, False), "bass": True},
           f"--stems bass: the bass stem only: {original}, {heard}")
    expect(abs(peak - STEM_LEVEL) < 0.05, f"--stems bass: one stem at {STEM_LEVEL} should not be boosted: peak {peak:.3f}")
    export_gp.export_song(song_dir, force=True, audio_stems=("vocals", "piano"))
    name, mix, original = embedded(path)
    heard, _ = tones(mix)
    expect(original == f"{SONG} (vocals).mp3" and heard == {**dict.fromkeys(STEM_TONES, False), "vocals": True},
           f"--stems vocals,piano with no piano stem: vocals only: {original}, {heard}")
    export_gp.export_song(song_dir, force=True, audio_stems=("piano",))
    song = gp7.dump(path.read_bytes())
    expect(not song["backing_track"] and not song["sync"], "--stems piano with no piano stem: no audio track")

    export_gp.export_song(song_dir, force=True, audio_mode="original")
    name, audio, original = embedded(path)
    expect(name and name.endswith(".wav") and audio == (TEST_ROOT / "songs" / f"{SONG}.wav").read_bytes(),
           f"--audio original: the recording, byte for byte, with its extension: {name}")
    export_gp.SONGS = TEST_ROOT / "no songs here"
    export_gp.export_song(song_dir, force=True, audio_mode="original")
    name, mix, original = embedded(path)
    heard, peak = tones(mix)
    expect(name.endswith(".mp3") and all(heard.values()) and peak < 0.95,
           f"--audio original without a recording: all stems mixed: {original}, {heard}, peak {peak:.3f}")
    export_gp.SONGS = TEST_ROOT / "songs"
    export_gp.export_song(song_dir, force=True, audio_mode="none")
    song = gp7.dump(path.read_bytes())
    expect(not song["backing_track"] and not song["sync"] and song["audio_bytes"] == 0, "--audio none: no audio track")
    with zipfile.ZipFile(path) as z:
        expect(not any(n.startswith("Content/Assets") for n in z.namelist()) and "meta.json" not in z.namelist(),
               f"--audio none: no audio file and no meta.json: {z.namelist()}")
    export_gp.export_song(song_dir, force=True)   # leave the default file for opening by hand
    check_command_line(song_dir)


def check_command_line(song_dir):
    """export_gp.py as the workbench runs it: --out writes the .gp there (even with an up-to-date .gp in the song
    folder) and leaves the song folder exactly as it was, a stale .gp5 included; errors exit non-zero with one
    line on stderr."""
    stale = song_dir / "transcription" / f"{SONG}.gp5"
    stale.write_bytes(b"left by an older version")
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in song_dir.rglob("*")}
    script = Path(export_gp.__file__)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "download.gp"
        run = subprocess.run([sys.executable, str(script), str(TEST_ROOT), "--song", SONG, "--out", str(out)],
                             capture_output=True, text=True)
        expect(run.returncode == 0 and out.is_file(), f"--out: exit {run.returncode}, {run.stderr.strip()[-300:]}")
        if out.is_file():
            song = gp7.dump(out.read_bytes())
            _, mix, original = embedded(out)
            expect(song["title"] == "测试歌" and original == f"{SONG} (vocals+guitar+other).mp3" and tones(mix)[0]["vocals"],
                   f"--out: the default .gp, with the default mix: {song['title']}, {original}")
        after = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in song_dir.rglob("*")}
        expect(after == before, f"--out must not touch the song folder: {set(after.items()) ^ set(before.items())}")
        for args, why in ((["--stems", "vocals,bogus"], "an unknown stem"), (["--out", str(out)], "--out without --song"),
                          (["--song", "no such song", "--out", str(out)], "a song that is not there")):
            run = subprocess.run([sys.executable, str(script), str(TEST_ROOT), *args], capture_output=True, text=True)
            expect(run.returncode != 0 and len(run.stderr.strip().splitlines()) == 1,
                   f"{why}: exit {run.returncode}, stderr {run.stderr!r}")
    stale.unlink(missing_ok=True)


def check_gpif(path, song, skip=0.0):
    """The .gp read with Python's own zip and XML parsers: version, title, bars and tracks as alphaTab read
    them, chord names without diagrams, tempo changes as steps with bar 1 marked once, the layout fields as
    Guitar Pro 8 writes them."""
    name = path.parent.parent.name
    with zipfile.ZipFile(path) as z:
        expect(z.read("VERSION") == b"7.0", f"{name}: .gp VERSION {z.read('VERSION')!r}")
        gpif = z.read("Content/score.gpif").decode("utf-8")
        root = ET.fromstring(gpif)
        entries, audio = z.namelist(), gp7.audio_entry(z)
        meta = json.loads(z.read("meta.json")) if "meta.json" in entries else {}
    expect(root.findtext("Score/Title") == song["title"], f"{name}: gpif title {root.findtext('Score/Title')!r}")
    expect(len(root.findall("MasterBars/MasterBar")) == len(song["bars"]), f"{name}: gpif bars")
    expect([t.findtext("Name") for t in root.findall("Tracks/Track")] == [t["name"] for t in song["tracks"]],
           f"{name}: gpif tracks")
    shown = {e.get("value") for e in root.iter("Property") if e.get("name") == "ShowDiagram"}
    expect(shown <= {"false"}, f"{name}: chord diagrams shown: {shown}")
    tempos = [a for a in root.findall("MasterTrack/Automations/Automation") if a.findtext("Type") == "Tempo"]
    expect(all(a.findtext("Linear") == "false" for a in tempos) and [a.findtext("Bar") for a in tempos].count("0") == 1,
           f"{name}: tempo changes should be steps, bar 1 marked once")
    # in CDATA (alphaTab), Guitar Pro 8 saved a nonsense bars-per-line number that then hung it (gp7.gp8_layout)
    layout = re.findall(r"<(?:ScoreSystems\w*Layout|MultiVoice)>[^<]*(?:<!\[CDATA\[)?", gpif)
    expect(layout == ["<ScoreSystemsDefaultLayout>3", "<ScoreSystemsLayout>", "<MultiVoice>1"],
           f"{name}: gpif layout fields {layout}")
    states = {t.findtext("Name"): t.findtext("PlaybackState") for t in root.findall("Tracks/Track")}
    expect({n for n, state in states.items() if state == "Mute"} == {t["name"] for t in song["tracks"] if t["mute"]},
           f"{name}: gpif playback states {states}")
    if song["backing_track"]:
        # laid out as in a .gp Guitar Pro 8 saved itself (alphaTab's test file guitarpro8/canon-audio-track.gp)
        backing = root.find("BackingTrack")
        expect(backing is not None and backing.findtext("Enabled") == "true" and backing.findtext("Source") == "Local"
               and backing.findtext("FramePadding") == str(-round(skip * export_gp.FRAME_RATE)), f"{name}: gpif audio track")
        order = ["IconId", "Color", "Name", "ShortName", "PlaybackState", "ChannelStrip", "Enabled", "Source", "AssetId",
                 "YouTubeVideoUrl", "Filter", "FramesPerPixel", "FramePadding", "Semitones", "Cents"]
        expect([c.tag for c in backing] == order and [c.tag for c in backing.find("ChannelStrip")] == ["Parameters"],
               f"{name}: audio track elements {[c.tag for c in backing]}")
        asset = root.find("Assets/Asset")
        expect(asset is not None and asset.get("id") == backing.findtext("AssetId")
               and asset.findtext("EmbeddedFilePath") == audio and audio.startswith(gp7.ASSETS)
               and Path(audio).stem == asset.findtext("OriginalFileSha1")
               and Path(audio).suffix == Path(asset.findtext("OriginalFilePath")).suffix.lower() != "",
               f"{name}: gpif asset {ET.tostring(asset, encoding='unicode')[:200] if asset is not None else None}")
        expect([c.tag for c in root][-1] == "Assets" and root.find("AudioTracks") is None,
               f"{name}: Assets should come last and there should be no AudioTracks")
        expect(gp7.ASSETS in entries and meta.get("hasAudio") is True, f"{name}: Content/Assets/ entry and meta.json")
        frames = [(int(a.findtext("Value/BarIndex")), int(a.findtext("Value/FrameOffset")))
                  for a in root.findall("MasterTrack/Automations/Automation") if a.findtext("Type") == "SyncPoint"]
        want = [(bar, round((ms / 1000 - skip) * 44100)) for bar, _, _, ms in song["sync"]]   # counted after the padding
        expect(len(frames) == len(want) and all(a == b and abs(x - y) <= 1 for (a, x), (b, y) in zip(frames, want)),
               f"{name}: gpif sync points (samples at 44.1 kHz) {frames[:3]}, expected {want[:3]}")


def check_songs(stems_root):
    """Every exported .gp under stems_root against its song's tempo map; prints what is in each."""
    paths = sorted(p for p in Path(stems_root).glob("*/transcription/*.gp") if p.stem == p.parent.parent.name)
    expect(paths, f"no exported .gp under {stems_root}")
    for path in paths:
        song_dir = path.parent.parent
        song = gp7.dump(path.read_bytes())
        expect((song["title"], song["artist"]) == export_gp.split_name(song_dir.name),
               f"{song_dir.name}: title / artist {song['title']!r} / {song['artist']!r}")
        expect(not path.with_suffix(".gp5").exists(), f"{song_dir.name}: a .gp5 is left over")
        knots, _, bars, skip = export_gp.song_bars(read_analysis(song_dir), export_gp.read_notes(song_dir))
        check_gpif(path, song, skip)
        expect([b["numerator"] for b in song["bars"]] == [n for _, n in bars], f"{song_dir.name}: bars differ from the tempo map")
        has_drums = any(t["percussion"] for t in song["tracks"])
        expect(has_drums == (song_dir / "transcription" / "drums.mid").is_file(), f"{song_dir.name}: drum track")
        guitar = ["Guitar"] if any(t["name"] == "Guitar" for t in song["tracks"]) else []   # a song may have no guitar notes
        expect([t["name"] for t in song["tracks"] if t["mute"]] == guitar, f"{song_dir.name}: only the guitar muted")
        starts = [1000 * float(export_gp.beats_to_seconds(b, knots)) for b, _ in bars]   # read back: in the audio file
        expect(song["backing_track"] and len(song["sync"]) == len(bars)
               and all(abs(ms - s) < 0.5 for (_, _, _, ms), s in zip(song["sync"], starts)),
               f"{song_dir.name}: audio track with a sync point at every bar start")
        name, _, original = embedded(path)
        expect(name.endswith(".mp3") and original == f"{song_dir.name} (vocals+guitar+other).mp3",
               f"{song_dir.name}: the default audio track should be the vocals+guitar+other mix: {original}")
        print(f"{song_dir.name}: {len(song['bars'])} bars, {song['tempo']} BPM, title {song['title']!r} by "
              f"{song['artist']!r}, {sum(bool(t['tempos']) for t in song['bars'])} tempo marks; audio track "
              f"{song['audio_bytes'] / 1e6:.1f} MB ({original}), {len(song['sync'])} sync points; "
              f".gp {path.stat().st_size / 1e6:.1f} MB")
        n_chords = 0
        for track in song["tracks"]:
            spec = export_gp.TRACKS.get(track["name"].split(" ")[0].lower(), {})
            expect(not unfilled_bars(song, track), f"{song_dir.name} / {track['name']}: bars do not add up")
            if not track["percussion"]:
                problems = fingering_problems(track, spec.get("frets", 24), spec.get("span"))
                expect(not problems, f"{song_dir.name} / {track['name']}: {problems[:5]}")
            notes = sum(not n["tie"] for b in track["beats"] for n in b["notes"])
            n_chords = max(n_chords, sum(b["chord"] is not None for b in track["beats"]))
            tuplets = sum(b["tuplet"][0] > 1 for b in track["beats"])
            print(f"  {track['name']}: {notes} notes, {tuplets} triplet notes/rests, tuning "
                  f"{'drums' if track['percussion'] else track['tuning']}, "
                  f"{'tab + notation' if track['tab'] else 'notation'}{', muted' if track['mute'] else ''}")
        print(f"  {n_chords} chord symbols")


def check_drum_fuzz(cases=25):
    """Random drum parts (8ths or triplet hi-hats, rolls, ruffs and flams of one drum, some across beats) through
    build_song, Guitar Pro and back: no hang, bars that add up, no tied drum note, every hit where drum_ticks put
    it on its beat's grid, and no two strokes of a run on one tick unless they were too fast for 32nds."""
    print("random drum parts:")
    rng = np.random.default_rng(1)
    period, n_beats = 0.5, 16
    beats = [period * b for b in range(n_beats + 1)]
    analysis = {"duration_s": period * n_beats,
                "tempo": {"bpm": 120.0, "beats": beats, "downbeats": beats[::4], "beats_per_bar": 4}}

    def too_slow(*_):
        raise TimeoutError("the export took over 20 s")
    old = signal.signal(signal.SIGALRM, too_slow)
    try:
        for case in range(cases):
            hits = []
            for b in range(n_beats):
                for x in ((0, 1 / 3, 2 / 3) if rng.random() < 0.2 else (0, 0.5)):
                    hits.append((b + x + rng.normal(0, 0.015), 42))
                hits.append((b + rng.normal(0, 0.015), 36 if b % 2 == 0 else 38))
            for _ in range(int(rng.integers(1, 5))):
                start, gap, n = rng.uniform(0, n_beats - 2), rng.uniform(0.02, 0.2), int(rng.integers(2, 9))
                key = int(rng.choice([38, 50, 47, 43, 36]))
                hits += [(start + i * gap + rng.normal(0, 0.01), key) for i in range(n)]
            hits = sorted((max(b, 0.0), key) for b, key in hits if b < n_beats - 0.25)
            notes = [(period * b, period * b, key, 100) for b, key in hits]
            stems = {"drums": (notes, "random")}
            signal.alarm(20)
            try:
                song, report = export_gp.build_song(Path("Fuzz - 鼓"), analysis, stems)
                gp5 = io.BytesIO()
                export_gp.gp.write(song, gp5, version=(5, 1, 0), encoding=export_gp.GP5_TEXT)
                dumped = gp7.dump(gp7.to_gp(gp5.getvalue()))
            except Exception as e:
                expect(False, f"random drums {case}: {type(e).__name__}: {e}")
                continue
            finally:
                signal.alarm(0)
            knots, _, bars, _ = export_gp.song_bars(analysis, stems)
            total = (bars[-1][0] + bars[-1][1]) * TICKS
            times, keys = [n[0] for n in notes], [n[2] for n in notes]
            on = export_gp.seconds_to_beats(times, knots)
            grid = export_gp.beat_grids(on, int(total // TICKS), drums=keys, seconds=times)
            ticks = export_gp.drum_ticks(on, keys, grid, times).tolist()
            drums = dumped["tracks"][0]
            want = {(t, key) for t, key in zip(ticks, keys) if t < total}
            expect(not export_gp.check(dumped, report), f"random drums {case}: {export_gp.check(dumped, report)}")
            expect(struck(drums) == want and all(export_gp.on_grid(t, grid) for t in ticks),
                   f"random drums {case}: missing {sorted(want - struck(drums))[:4]}, extra {sorted(struck(drums) - want)[:4]}")
            expect(not any(n["tie"] for b in drums["beats"] for n in b["notes"]), f"random drums {case}: a tied drum note")
            for run in export_gp.stroke_runs(export_gp.drum_pairs(on, keys, times)):
                shared = len(run) - len({ticks[i] for i in run})
                room = len({t for i in run for t in range(ticks[run[0]], ticks[run[-1]] + 1, 120)})
                expect(shared == 0 or room < len(run), f"random drums {case}: run {[round(on[i], 3) for i in run]} "
                       f"shares {shared} tick(s): {[ticks[i] for i in run]}")
    finally:
        signal.signal(signal.SIGALRM, old)
    print(f"  {cases} parts")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--songs", type=Path, help="also check the exported .gp files under this stems folder")
    args = parser.parse_args()
    check_units()
    check_drum_fuzz()
    songs = export_gp.SONGS
    try:
        check_song()
    finally:
        export_gp.SONGS = songs   # the real songs/ again, for --songs
    if args.songs:
        check_songs(args.songs)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
