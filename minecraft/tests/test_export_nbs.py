"""Check minecraft/export_nbs.py (Guitar Pro / MIDI -> Open Note Block Studio .nbs) on files whose notes are known,
and the workbench's /api/nbs.

    .venv/bin/python minecraft/tests/test_export_nbs.py
    .venv/bin/python minecraft/tests/test_export_nbs.py --gp "songs/电动少女(Live).gp"     # also a real score
    .venv/bin/python minecraft/tests/test_export_nbs.py --songs outputs/roformer_sw_full   # and a whole library

The test files go to outputs/_export_nbs_test/ (git-ignored), so the .nbs files can be opened in Note Block Studio
by hand as well; each run deletes its old outputs first. Every .nbs is read back twice: with pynbs, and byte by
byte with this file's own reader written from the format page (noteblock.studio/nbs), which must consume the
whole file. Expected timbres are Note Block Studio 3.12's values written out here (NBS312), not read from
export_nbs.py's tables, so a changed map fails.

1. A Guitar Pro file built here (PyGuitarPro -> Guitar Pro 5 bytes -> experiments/gp7.to_gp, as export_gp.py
   does) with a Chinese title at 122 BPM: a bass whose bars 2-3 repeat, with notes below the Double Bass's range,
   a 16th run of two legato slides into a shift slide, and a tied note; a drum kit whose kick is in the second
   voice, with a crash, an open hi-hat, a ride, a cowbell, a triangle (Chime), and a snare double stroke a 32nd
   apart; and a muted guitar with a Chinese name, whose last bar has an E chord strummed down (Guitar Pro 5's 8th
   stroke) and a dead-note strum strummed up. By default only the bass and drums come out: every note on its
   tick (the repeat played twice, the target of each slide struck, also the legato one's, which alphaTab only
   bends to; the tie struck once, the 32nd on the next 16th), at 4 ticks per beat = 150 BPM, the bass on the
   Double Bass folded into its two octaves, each drum on its kit piece's instrument and key, all at velocity 100
   (--velocities keeps the score's), the sand (Snare Drum) layers after all others, ASCII names, version 5,
   10 ticks/s. The guitar alone (at 4 ticks per beat): the strum's six strings on one tick (alphaTab spreads them
   over a 16th, so the last three would round onto the next tick), the same chord as an arpeggio keeping that
   spread, and the dead strum as one side-stick click (Click 41) on its own layer; --list counts it as one dead
   hit. --list writes nothing (checked in a folder of its own and in minecraft/output_nbs/); --tracks and
   --instrument by number, name in any case and kind; --instrument errors for an unknown, a drum or an
   unconverted track; a number never matches a track's name.
2. Two MIDI files like OpenMusic's transcriptions, notes 15 ms off the grid: one at 150 BPM with a 2-beat pickup
   (the pickup is padded so that the first downbeat sits on Note Block Studio's bar line), one at 88 BPM whose
   first beat is squeezed into 46 ms (1304 BPM, as in 草东没有派对 - 大风吹; the song's tempo is still 88, so 8
   ticks per beat = 75 BPM). Unquantised notes keep 16ths; a drum note outside General MIDI's kit plays as a Click.
3. MIDI edge cases: a quantised first note exactly half a 6-tick grid step before a bar line (it once came out at
   tick -1, which corrupts the file); a time signature with numerator 0 (once an endless loop); program changes
   kept in a setup track; GB2312 track names (shown in Chinese, layers named by kind and number); a Shift-JIS name
   with the byte 0x85 (Latin-1 U+0085 once split the workbench's JSON line); Latin-1 and cp1252 names (Canção,
   Don't Stop: kept, not read as GB2312 or blanked); tracks named with digits; a type-0 file; a file over 8 MB of
   MIDI (refused); a first note 268 million beats in (once a tuple per bar up to it); --instrument by kind with
   one of two guitars converted, and all=bit; --ticks-per-beat 0 (an error, not "automatic").
4. Guitar Pro edge cases: a zip that unpacks to more than 64 MB (refused without unpacking), a .gpx declaring
   1 GB (refused), a zip without a score (one line of error, not alphaTab's minified source), a repeat count of
   100000 (played 8 times, quickly), an anacrusis bar (padded like a pickup), and a dead-slapped bass beat as the
   score's only dead thing (one side-stick click, not a chord of every open string; one dead hit in --list).
5. Grid choice on quantised notes: straight 8ths at 105 BPM take 6 ticks per beat (100 BPM), 16ths at 110 take 4
   (150 BPM) and at 105 take 8 (75 BPM, nearer than 150), a shuffle in triplets at 100 BPM takes 6; a transcribed
   first note a little before the first downbeat starts the song there, not a silent pickup bar earlier.
6. An OpenMusic song folder: without --out the .nbs goes to minecraft/output_nbs/<song>.nbs (the test removes it
   again, and leaves a file of that name it did not make alone) and the song folder does not change; with
   --tracks all the vocals land on the Piano (their range), not on their program's Flute.
7. The command line: errors (missing file, not a score, unknown track, bad instrument, bad grid) are a non-zero
   exit code and one line on stderr.
8. The workbench (experiments/app.py on a free port, with a temp folder of its own): /api/nbs lists the tracks
   (also of the Shift-JIS file), returns the same .nbs as the command line with a small X-NBS-Summary, refuses a
   request without the X-OpenMusic header or from another host, a file that is not a score, bad options, an
   unknown track and a declared size over 64 MB, and leaves nothing in its temp folder.
--gp and --songs convert real files (to a temp folder) and check that the file was written, that every note
block is in range on its own (tick, layer), and that the drum and bass parts are there. Exits with status 1 on any
failure.
"""
import argparse
import hashlib
import http.client
import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import mido

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "minecraft"))
sys.path.insert(0, str(ROOT / "experiments"))
import export_nbs  # noqa: E402
import gp7  # noqa: E402
import guitarpro as gp  # noqa: E402
import pynbs  # noqa: E402

TEST_ROOT = ROOT / "outputs" / "_export_nbs_test"
SCRIPT = ROOT / "minecraft" / "export_nbs.py"
FAILURES = []
# Note Block Studio 3.12's drum map (midi_instruments.gml at v3.12.0-rc.1) for the notes the tests play:
# General MIDI note -> (Note Block Studio instrument, key). 2 Bass Drum, 3 Snare Drum, 4 Click, 7 Bell, 8 Chime,
# 11 Cow Bell; notes outside the map play as a Click at 45. 37, the side stick, is how dead notes sound.
NBS312 = {36: (2, 41), 37: (4, 41), 38: (3, 48), 42: (4, 54), 44: (4, 56), 46: (3, 55), 49: (3, 53), 51: (3, 50),
          53: (7, 50), 56: (11, 39), 81: (8, 52)}
DEAD_CLICK = NBS312[37]
SNARE_DRUM, DOUBLE_BASS, PIANO, GUITAR, DIDGERIDOO = 3, 1, 0, 5, 12


def expect(ok, message):
    if not ok:
        FAILURES.append(message)
        print("  FAIL", message)
    return ok


# ---------- reading a .nbs without pynbs ----------

def read_nbs(path):
    """(header, notes, layers) of a version 4-6 .nbs, read from the format page; notes as (tick, layer,
    instrument, key, velocity, panning, pitch)."""
    data, pos = Path(path).read_bytes(), 0

    def take(fmt):
        nonlocal pos
        value = struct.unpack_from("<" + fmt, data, pos)[0]
        pos += struct.calcsize("<" + fmt)
        return value

    def text():
        nonlocal pos
        n = take("i")
        raw = data[pos:pos + n]
        pos += n
        return raw.decode("ascii")                 # export_nbs writes ASCII only

    head = {"zero": take("h"), "version": take("B"), "instruments": take("B"), "length": take("h"),
            "layers": take("h")}
    for key in ("name", "author", "original_author", "description"):
        head[key] = text()
    head["tempo"] = take("h") / 100
    take("B"), take("B")
    head["time_signature"] = take("B")
    for _ in range(5):
        take("i")
    head["origin"] = text()
    head["loop"], head["max_loops"], head["loop_start"] = take("B"), take("B"), take("h")
    notes, tick = [], -1
    while True:
        jump = take("h")
        if jump == 0:
            break
        tick += jump
        layer = -1
        while True:
            jump = take("h")
            if jump == 0:
                break
            layer += jump
            notes.append((tick, layer, take("B"), take("B"), take("B"), take("B") - 100, take("h")))
    layers = [(text(), take("B"), take("B"), take("B") - 100) for _ in range(head["layers"])]
    head["custom"] = take("B")
    head["left_over"] = len(data) - pos
    return head, notes, layers


def check_file(path, label, velocity=100):
    """Read a .nbs both ways and check what every file must be. Returns (header, notes, layers)."""
    try:
        head, notes, layers = read_nbs(path)
    except (struct.error, UnicodeDecodeError, OSError) as e:
        expect(False, f"{label}: cannot read {path.name}: {e}")
        return {"length": 0, "name": "", "original_author": "", "author": "", "origin": "", "time_signature": 0}, [], []
    expect(head["zero"] == 0 and head["version"] == 5 and head["instruments"] == 16,
           f"{label}: header {head['zero']}/{head['version']}/{head['instruments']}, want 0 / 5 / 16")
    expect(head["left_over"] == 0 and head["custom"] == 0, f"{label}: {head['left_over']} bytes left, "
                                                           f"{head['custom']} custom instruments")
    expect(head["tempo"] == 10.0, f"{label}: tempo {head['tempo']} ticks/s, want 10")
    expect(bool(notes) and notes[0][0] >= 0, f"{label}: no notes, or a note before tick 0")
    expect(all(33 <= n[3] <= 57 for n in notes), f"{label}: keys outside 33-57")
    expect(all(0 <= n[2] <= 15 for n in notes), f"{label}: instruments outside 0-15")
    expect(len({(n[0], n[1]) for n in notes}) == len(notes), f"{label}: two notes on one tick and layer")
    expect(bool(notes) and head["length"] == notes[-1][0], f"{label}: length {head['length']} is not the last tick")
    expect(head["layers"] == len(layers) and bool(notes) and max(n[1] for n in notes) < len(layers),
           f"{label}: layer count")
    expect(all(n[5] == 0 and n[6] == 0 for n in notes), f"{label}: panning/pitch not 0")
    if velocity is not None:
        expect(all(n[4] == velocity for n in notes), f"{label}: velocities other than {velocity}")
    f = pynbs.read(str(path))
    expect([(n.tick, n.layer, n.instrument, n.key, n.velocity) for n in f.notes] == [n[:5] for n in notes],
           f"{label}: pynbs reads other notes")
    return head, notes, layers


def run(*args, ok=True):
    done = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=120)
    if ok:
        expect(done.returncode == 0, f"export_nbs.py {' '.join(map(str, args))}: exit {done.returncode}: "
                                     f"{done.stderr.strip()[-300:]}")
    return done


def convert(src, out, *args, label=None):
    """Run the converter to `out` (deleted first, so an old file never passes) and read the result back."""
    out = Path(out)
    out.unlink(missing_ok=True)
    run(src, *args, "--out", out)
    if not expect(out.is_file(), f"{label or out.name}: no {out.name} written"):
        return {"length": 0, "time_signature": 0, "name": "", "original_author": "", "author": "", "origin": ""}, [], []
    velocity = None if "--velocities" in args else 100
    return check_file(out, label or out.name, velocity)


def bad(label, *args, says=None):
    done = run(*args, ok=False)
    lines = [line for line in done.stderr.splitlines() if line.strip()]
    expect(done.returncode != 0 and len(lines) == 1 and lines[0].startswith("export_nbs.py: ")
           and len(lines[0]) < 600 and (says is None or says in lines[0]),
           f"{label}: exit {done.returncode}, stderr {done.stderr.strip()[-300:]!r}")


def drum(key):
    return NBS312.get(key, (4, 45))


def bass_key(pitch):
    return export_nbs.fold(pitch - 30 + 33)


def guitar_key(pitch):
    return export_nbs.fold(pitch - 42 + 33)


def tick_of(beat, tpb, start=0.0):
    return int((beat - start) * tpb + 0.5)


def tree(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(folder).rglob("*")) if p.is_file()}


def fresh(name):
    folder = TEST_ROOT / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    return folder


# ---------- 1. a Guitar Pro file ----------

def gp_song(first_bar_beats=4, dead_slap=False, repeat_count=None):
    """(.gp bytes, {track: [(beat, MIDI pitch)]} as played: the repeat twice, each slide's target struck, the tie
    once; and "吉他 dead": [beat] of each dead strum). first_bar_beats < 4 makes bar 1 hold only that many quarter
    notes (for an anacrusis); dead_slap marks the bass's first beat and leaves the guitar without dead notes, so
    the slap is the score's only dead thing; repeat_count writes that count on the repeat (the expected notes still
    assume two plays)."""
    ticks = 960
    song = gp.Song(title="测试歌", artist="Test Band", tempo=122)
    song.measureHeaders, song.tracks = [], []
    for number in range(1, 5):
        song.addMeasureHeader(gp.MeasureHeader(number=number, start=ticks + (number - 1) * 4 * ticks,
                                               timeSignature=gp.TimeSignature(numerator=4, denominator=gp.Duration(4)),
                                               isRepeatOpen=number == 2, repeatClose=1 if number == 3 else -1))

    def track(number, name, strings, channel, program, drums=False, mute=False):
        t = gp.Track(song, number=number, name=name, fretCount=24, isPercussionTrack=drums, isMute=mute,
                     strings=[gp.GuitarString(i, v) for i, v in enumerate(strings, 1)],
                     channel=gp.MidiChannel(channel=channel, effectChannel=channel if drums else channel + 1,
                                            instrument=program))
        t.measures = [gp.Measure(t, h) for h in song.measureHeaders]
        song.tracks.append(t)
        return t

    def put(measure, voice, beats):
        """beats: [(note value, [(string, fret)], tied, *effects)]; an effect is a SlideType (out of each note),
        "dead" (dead notes) or a BeatStroke (a strum)."""
        v = measure.voices[voice]
        for value, notes, tied, *effects in beats:
            b = gp.Beat(v, duration=gp.Duration(value=value), status=gp.BeatStatus.normal if notes else gp.BeatStatus.rest)
            b.effect.stroke = next((e for e in effects if isinstance(e, gp.BeatStroke)), b.effect.stroke)
            for string, fret in notes:
                note = gp.Note(b, value=fret, string=string, velocity=gp.Velocities.default,
                               type=gp.NoteType.dead if "dead" in effects else
                               gp.NoteType.tie if tied else gp.NoteType.normal)
                note.effect.slides = [e for e in effects if isinstance(e, gp.SlideType)]
                b.notes.append(note)
            v.beats.append(b)

    bass = track(1, "Electric Bass", [43, 38, 33, 28], 0, 33)
    quarters = [(4, [(4, 0)], False), (4, [(4, 1)], False), (4, [(3, 0)], False), (4, [(2, 2)], False)]  # E1 F1 A1 E2
    put(bass.measures[0], 0, quarters[:first_bar_beats])
    for m in bass.measures[1:3]:
        put(m, 0, quarters)
    put(bass.measures[3], 0, [(16, [(4, 5)], False, gp.SlideType.legatoSlideTo),
                              (16, [(4, 7)], False, gp.SlideType.legatoSlideTo),
                              (16, [(4, 9)], False, gp.SlideType.shiftSlideTo),
                              (16, [(4, 10)], False),                         # A1 B1 C#2 D2, sliding up the E string
                              (4, [(2, 2)], False), (4, [(2, 2)], True), (4, [], False)])   # E2 tied on, rest
    drums = track(2, "Drumkit", [0] * 6, 9, 0, drums=True)
    groove = [(8, [(1, 42)], False), (8, [(1, 42)], False), (8, [(1, 42), (2, 38)], False), (8, [(1, 42)], False)] * 2
    kicks = [(4, [(1, 36)], False), (4, [], False), (4, [(1, 36)], False), (4, [], False)]
    put(drums.measures[0], 0, groove[:2 * first_bar_beats])
    put(drums.measures[0], 1, kicks[:first_bar_beats])
    for m in drums.measures[1:3]:
        put(m, 0, groove)
        put(m, 1, kicks)                                                     # the kick: in voice 2
    put(drums.measures[3], 0, [(8, [(1, 49), (2, 46)], False), (8, [(1, 51)], False), (8, [(1, 56)], False),
                               (8, [(1, 81)], False), (4, [], False), (32, [(2, 38)], False), (32, [(2, 38)], False),
                               (16, [], False), (8, [], False)])
    put(drums.measures[3], 1, [(1, [(1, 36)], False)])
    guitar = track(3, "吉他", [64, 59, 55, 50, 45, 40], 2, 27, mute=True)
    put(guitar.measures[0], 0, [(4, [(1, 0)], False)] * first_bar_beats)
    for m in guitar.measures[1:3]:
        put(m, 0, [(4, [(1, 0)], False)] * 4)
    put(guitar.measures[3], 0, [(4, [(6, 0), (5, 2), (4, 2), (3, 1), (2, 0), (1, 0)], False,   # E2 B2 E3 G#3 B3 E4
                                 gp.BeatStroke(gp.BeatStrokeDirection.down, 8)),            # strummed down
                                (4, [(4, 2), (3, 1), (2, 0), (1, 0)], False, "dead",        # a dead strum, up
                                 gp.BeatStroke(gp.BeatStrokeDirection.up, 8)),
                                (4, [(1, 0)], False), (4, [(1, 0)], False)])

    buf = io.BytesIO()
    gp.write(song, buf, version=(5, 1, 0), encoding="utf-8")
    data = gp7.to_gp(buf.getvalue())
    if first_bar_beats < 4 or dead_slap or repeat_count:
        def edit(gpif):
            if first_bar_beats < 4:
                gpif = gpif.replace("<MasterTrack>", "<MasterTrack><Anacrusis />", 1)
            if dead_slap:                                # the slap as the score's only dead thing
                gpif = gpif.replace("<Beat id=\"0\">", "<Beat id=\"0\"><DeadSlapped />", 1)
                gpif = gpif.replace('<Property name="Muted"><Enable/></Property>', "")
            if repeat_count:
                gpif = gpif.replace('end="true" count="2"', f'end="true" count="{repeat_count}"', 1)
            return gpif
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            data = gp7.rezip([(i, z.read(i)) for i in z.infolist()], edit)
    played = {"Electric Bass": [], "Drumkit": [], "吉他": [], "吉他 dead": []}   # the last: dead strums' beats
    start = 0
    for bar, at in enumerate([0, 1, 2, 1, 2, 3]):                    # bars as played: 1 2 3 2 3 4
        length = first_bar_beats if bar == 0 else 4
        if at < 3:
            played["Electric Bass"] += [(start + i, p) for i, p in enumerate((28, 29, 33, 40)) if i < length]
            played["Drumkit"] += [(start + i / 2, 42) for i in range(2 * length)]
            played["Drumkit"] += [(start + b, 38) for b in (1, 3) if b < length]
            played["Drumkit"] += [(start + b, 36) for b in (0, 2) if b < length]
        else:
            played["Electric Bass"] += [(start, 33), (start + 0.25, 35), (start + 0.5, 37), (start + 0.75, 38),
                                        (start + 1, 40)]
            played["Drumkit"] += [(start, 49), (start, 46), (start + 0.5, 51), (start + 1, 56), (start + 1.5, 81),
                                  (start + 3, 38), (start + 3.125, 38), (start, 36)]
        if at < 3:
            played["吉他"] += [(start + i, 64) for i in range(length)]
        else:
            played["吉他"] += [(start, p) for p in (40, 47, 52, 56, 59, 64)] + [(start + 2, 64), (start + 3, 64)]
            if dead_slap:                                # its dead strum is a plain strum then
                played["吉他"] += [(start + 1, p) for p in (52, 56, 59, 64)]
            else:
                played["吉他 dead"] += [start + 1]
        start += length
    return data, played


def check_gp():
    print("1. Guitar Pro file")
    folder = fresh("gp")
    data, played = gp_song()
    src = folder / "test song.gp"
    src.write_bytes(data)
    before = hashlib.sha256(data).hexdigest()
    out = folder / "test song.nbs"
    out.unlink(missing_ok=True)
    done = run(src, "--out", out)
    expect("150 BPM" in done.stdout and "4 ticks per beat" in done.stdout, f"grid not 4 ticks per beat:\n{done.stdout}")
    head, notes, layers = check_file(out, "gp")
    expect(head["name"] == "OpenMusic song" and head["original_author"] == "Test Band" and head["author"] == "OpenMusic",
           f"gp: names {head['name']!r} / {head['original_author']!r} / {head['author']!r}")
    expect(head["origin"] == "test song.gp" and head["time_signature"] == 4,
           f"gp: origin {head['origin']!r}, time signature {head['time_signature']}")
    names = [l[0] for l in layers]
    expect(names[0] == "Electric Bass" and all(n.startswith("Drumkit") for n in names[1:]),
           f"gp: layers {names} (the muted guitar must not be there)")
    got = {(n[0], n[2], n[3]) for n in notes}
    want = {(tick_of(b, 4), DOUBLE_BASS, bass_key(p)) for b, p in played["Electric Bass"]}
    want |= {(tick_of(b, 4), *drum(k)) for b, k in played["Drumkit"]}
    expect(got == want, f"gp: notes differ: missing {sorted(want - got)[:8]}, extra {sorted(got - want)[:8]}")
    expect(sum(n[2] == DOUBLE_BASS for n in notes) == len(played["Electric Bass"]),
           f"gp: bass notes, want {len(played['Electric Bass'])} (repeat twice, tie once)")
    sand = [n[1] for n in notes if n[2] == SNARE_DRUM]
    expect(sand and min(sand) > max(n[1] for n in notes if n[2] != SNARE_DRUM),
           "gp: Snare Drum (sand) layers are not after all other layers")
    expect(hashlib.sha256(src.read_bytes()).hexdigest() == before, "gp: the input file changed")

    _, nv, _ = convert(src, folder / "velocities.nbs", "--velocities", label="gp --velocities")
    expect({n[4] for n in nv} != {100} and all(1 <= n[4] <= 100 for n in nv), "gp --velocities: no score velocities")

    lone = fresh("gp-list")                                  # --list writes nothing: in a folder of its own
    shutil.copy(src, lone / src.name)
    default = export_nbs.OUTPUT_DIR / f"{src.stem}.nbs"     # nor in the default place
    default_before = default.stat().st_mtime_ns if default.exists() else None
    listed = run(lone / src.name, "--list")
    info = json.loads(run(lone / src.name, "--list", "--json").stdout)
    expect(tree(lone) == {src.name: before}, f"gp --list wrote {sorted(tree(lone))}")
    expect((default.stat().st_mtime_ns if default.exists() else None) == default_before,
           f"gp --list wrote minecraft/output_nbs/{default.name}")
    expect("muted" in listed.stdout and "Electric Bass" in listed.stdout, f"gp --list: {listed.stdout}")
    expect([t["kind"] for t in info["tracks"]] == ["bass", "drums", "guitar"]
           and [t["default"] for t in info["tracks"]] == [True, True, False]
           and [t["muted"] for t in info["tracks"]] == [False, False, True]
           and [t["notes"] for t in info["tracks"]] == [len(played[k]) for k in ("Electric Bass", "Drumkit", "吉他")]
           and [t["dead"] for t in info["tracks"]] == [0, 0, len(played["吉他 dead"])],   # hits, not strings
           f"gp --list --json: {info}")

    _, notes_all, layers_all = convert(src, folder / "all.nbs", "--tracks", "all", label="gp --tracks all")
    guitar_layer = next((i for i, l in enumerate(layers_all) if l[0] == "Guitar 3"), None)
    expect(guitar_layer is not None and {n[2] for n in notes_all if n[1] == guitar_layer} == {GUITAR},
           f"gp --tracks all: no Guitar 3 layer on the Guitar: {[l[0] for l in layers_all]}")
    # the guitar at 4 ticks per beat (alone, its quarters would take 5 = 120 BPM): the strum's six strings on one
    # tick (Guitar Pro 5's 8th stroke, which alphaTab spreads over a 16th: the last three strings would round onto
    # the next tick), the dead strum (strummed up) as one side-stick click on a layer of its own
    want_guitar = {(tick_of(b, 4), GUITAR, guitar_key(p)) for b, p in played["吉他"]}
    want_guitar |= {(tick_of(b, 4), *DEAD_CLICK) for b in played["吉他 dead"]}
    for spec in ("3", "guitar", "吉他"):
        _, n1, l1 = convert(src, folder / "one.nbs", "--tracks", spec, "--ticks-per-beat", "4",
                            label=f"gp --tracks {spec}")
        got1 = {(n[0], n[2], n[3]) for n in n1}
        clicks = sorted((n[0], n[2], n[3]) for n in n1 if l1[n[1]][0] == "Guitar 3: Dead notes")
        expect(got1 == want_guitar and len(n1) == len(want_guitar) and l1[:1] and l1[0][0] == "Guitar 3"
               and clicks == sorted(t for t in want_guitar if t[1:] == DEAD_CLICK),
               f"gp --tracks {spec}: missing {sorted(want_guitar - got1)[:8]}, "
               f"extra {sorted(got1 - want_guitar)[:8]}, clicks {clicks}, layers {[l[0] for l in l1]}")

    def arpeggio(gpif):                                    # the strummed chord as an arpeggio of the same length
        brush = '<Properties><Property name="Brush"><Direction>Down</Direction></Property></Properties>'
        expect(gpif.count(brush) == 1, "gp arpeggio: the strum is not in the score as the test expects")
        return gpif.replace(brush, "<Arpeggio>Down</Arpeggio>")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        (folder / "arpeggio.gp").write_bytes(gp7.rezip([(i, z.read(i)) for i in z.infolist()], arpeggio))
    _, na, _ = convert(folder / "arpeggio.gp", folder / "arpeggio.nbs", "--tracks", "3", "--ticks-per-beat", "4",
                       label="gp arpeggio")
    chord = tick_of(next(b for b, p in played["吉他"] if p == 40), 4)   # an arpeggio keeps its spread:
    late = {(chord, GUITAR, guitar_key(p)) for p in (56, 59, 64)}       # G#3 B3 E4 on the next tick
    want_arpeggio = (want_guitar - late) | {(tick + 1, sound, key) for tick, sound, key in late}
    got_arpeggio = {(n[0], n[2], n[3]) for n in na}
    expect(got_arpeggio == want_arpeggio, f"gp arpeggio: missing {sorted(want_arpeggio - got_arpeggio)}, "
                                          f"extra {sorted(got_arpeggio - want_arpeggio)}")
    for spec in ("Electric Bass=didgeridoo", "electric bass=didgeridoo", "1=didgeridoo", "bass=didgeridoo"):
        _, nd, _ = convert(src, folder / "didg.nbs", "--instrument", spec, label=f"gp --instrument {spec}")
        expect({n[2] for n in nd if n[1] == 0} == {DIDGERIDOO}, f"gp --instrument {spec}: the bass is not on the Didgeridoo")
    head6, n6, _ = convert(src, folder / "six.nbs", "--ticks-per-beat", "6", label="gp --ticks-per-beat 6")
    want6 = {(tick_of(b, 6), DOUBLE_BASS, bass_key(p)) for b, p in played["Electric Bass"]}
    expect({(n[0], n[2], n[3]) for n in n6 if n[2] == DOUBLE_BASS} == want6, "gp --ticks-per-beat 6: bass ticks")
    expect(head6["time_signature"] == 6, f"gp --ticks-per-beat 6: time signature {head6['time_signature']} (want 6)")
    bad("instrument for an unknown track", src, "--instrument", "nobody=guitar", says="no track nobody")
    bad("instrument for the drums", src, "--instrument", "Drumkit=guitar", says="drum kit")
    bad("instrument for an unconverted track", src, "--instrument", "3=flute", says="not converted")
    bad("bad instrument", src, "--instrument", "Electric Bass=banana")
    bad("drum as instrument", src, "--instrument", "Electric Bass=snare")
    bad("unknown track", src, "--tracks", "9", says="no track 9")
    bad("bad grid", src, "--ticks-per-beat", "13")
    bad("missing file", folder / "nothing.gp")
    (folder / "text.gp").write_text("not a score")
    bad("not a score", folder / "text.gp")
    return src, out


# ---------- 2-3. MIDI ----------

def write_midi(path, bpms, signatures, parts, jitter_ms=15.0, setup_programs=None, midi_type=1, ppq=480, exact=False):
    """bpms: [(beat, BPM)]; signatures: [(beat, num[, den])]; parts: {(name, channel, program): [(beat, pitch)]}
    (program None: no program change in that track). Notes go jitter_ms early and late in turn, as a
    transcription's do, unless exact. setup_programs: {channel: program} written in the first track instead."""
    events = [(round(b * ppq), mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm))) for b, bpm in bpms]
    for b, *sig in signatures:
        events.append((round(b * ppq), mido.MetaMessage("time_signature", numerator=sig[0],
                                                        denominator=sig[1] if len(sig) > 1 else 4)))
    events += [(0, mido.Message("program_change", program=p, channel=c)) for c, p in (setup_programs or {}).items()]
    tracks = [("Tempo map", events)]
    for (name, channel, program), notes in parts.items():
        evs = [(0, mido.Message("program_change", program=program, channel=channel))] if program is not None else []
        for i, (beat, pitch) in enumerate(notes):
            bpm = next(v for b, v in reversed(bpms) if b <= beat)
            shift = 0 if exact else (1 if i % 2 else -1) * jitter_ms / 1000 * bpm / 60
            on = max(0, round((beat + shift) * ppq))
            evs += [(on, mido.Message("note_on", note=pitch, velocity=90, channel=channel)),
                    (on + ppq // 8, mido.Message("note_on", note=pitch, velocity=0, channel=channel))]
        tracks.append((name, evs))
    mid = mido.MidiFile(type=midi_type, ticks_per_beat=ppq)
    if midi_type == 0:
        tracks = [("", [e for _, evs in tracks for e in evs])]
    for name, evs in tracks:
        track = mido.MidiTrack([mido.MetaMessage("track_name", name=name)] if name else [])
        now = 0
        for tick, msg in sorted(evs, key=lambda e: (e[0], e[1].type == "note_on" and e[1].velocity > 0)):
            track.append(msg.copy(time=tick - now))
            now = tick
        mid.tracks.append(track)
    mid.save(str(path))


def check_midi():
    print("2. MIDI like OpenMusic's")
    folder = fresh("midi")
    bass = [(b, 40 + int(b * 2) % 12) for b in [0.5, 1.0, 1.5] + [2 + i / 2 for i in range(16)]]
    drums = [(b, 36) for b in [0.0, 2.0, 4.0, 6.0, 8.0]] + [(2 + i / 4, 42) for i in range(32)] + [(5.0, 90)]
    src = folder / "pickup.mid"
    write_midi(src, [(0, 150)], [(0, 2), (2, 4)], {("Bass", 0, 33): bass, ("Drums", 9, 0): drums})
    out = folder / "pickup.nbs"
    done = run(src, "--out", out)
    expect("not quantised" in done.stdout and "4 ticks per beat" in done.stdout, f"pickup: {done.stdout}")
    head, notes, layers = check_file(out, "pickup")
    want = {(tick_of(b, 4, -2), DOUBLE_BASS, bass_key(p)) for b, p in bass}
    want |= {(tick_of(b, 4, -2), *drum(k)) for b, k in drums}
    got = {(n[0], n[2], n[3]) for n in notes}
    expect(got == want, f"pickup: missing {sorted(want - got)[:6]}, extra {sorted(got - want)[:6]}")
    expect(tick_of(2.0, 4, -2) == 16 and (16, *drum(36)) in got and head["time_signature"] == 4,
           "pickup: the first downbeat (beat 2) is not on Note Block Studio's second bar line (tick 16)")
    expect((tick_of(5.0, 4, -2), 4, 45) in got, "pickup: a drum note outside the kit is not a Click 45")
    expect(head["name"] == "pickup" and [l[0] for l in layers][:1] == ["Bass"], f"pickup: {head['name']}, {layers[:2]}")

    src = folder / "squeezed.mid"
    bass88 = [(1 + i / 2, 36 + i % 5) for i in range(24)]
    drums88 = [(1 + i, 36 if i % 2 == 0 else 38) for i in range(12)]
    write_midi(src, [(0, 1304.3), (1, 88)], [(0, 1), (1, 4)], {("Bass", 0, 33): bass88, ("Drums", 9, 0): drums88})
    out = folder / "squeezed.nbs"
    done = run(src, "--out", out)
    expect("88 BPM -> 8 ticks per beat = 75 BPM" in done.stdout, f"squeezed lead-in: {done.stdout}")
    _, notes, _ = check_file(out, "squeezed")
    want = {(tick_of(b, 8, 1), DOUBLE_BASS, bass_key(p)) for b, p in bass88}
    want |= {(tick_of(b, 8, 1), *drum(k)) for b, k in drums88}
    got = {(n[0], n[2], n[3]) for n in notes}
    expect(got == want, f"squeezed: missing {sorted(want - got)[:6]}, extra {sorted(got - want)[:6]}")


def check_midi_edges():
    print("3. MIDI edge cases")
    folder = fresh("midi-edges")
    # a quantised first note half a 6-tick grid step (1/12 beat) before bar 2: in floats its tick came out -1
    src = folder / "half step.mid"
    notes = [(23.5 / 6, 33)] + [(4 + i / 2, 33 + i % 5) for i in range(16)]
    write_midi(src, [(0, 100)], [(0, 4)], {("Bass", 0, 33): notes}, exact=True)
    head, got, _ = convert(src, folder / "half step.nbs", label="half step before a bar")
    expect(got and got[0][0] == 0, f"half step before a bar: first tick {got[0][0] if got else None}")

    src = folder / "numerator 0.mid"
    write_midi(src, [(0, 120)], [(0, 0)], {("Bass", 0, 33): [(i, 40) for i in range(8)]})
    started = time.perf_counter()
    convert(src, folder / "numerator 0.nbs", label="time signature 0/4")
    expect(time.perf_counter() - started < 20, "time signature 0/4: took too long")

    src = folder / "setup track.mid"
    write_midi(src, [(0, 120)], [(0, 4)], {("Bass", 0, None): [(i / 2, 33 + i % 7) for i in range(16)],
                                          ("Lead", 1, None): [(i, 72 + i % 5) for i in range(8)]},
               setup_programs={0: 33, 1: 73})
    info = json.loads(run(src, "--list", "--json").stdout)
    expect([(t["program"], t["kind"]) for t in info["tracks"]] == [(33, "bass"), (73, "melody")],
           f"setup track: {[(t['name'], t['program'], t['kind']) for t in info['tracks']]}")
    _, got, _ = convert(src, folder / "setup track.nbs", label="setup track")
    expect({n[2] for n in got} == {DOUBLE_BASS}, f"setup track: bass on {sorted({n[2] for n in got})}")

    src = folder / "gb2312.mid"
    gbk = lambda s: s.encode("gb2312").decode("latin-1")  # noqa: E731  (mido writes names as Latin-1)
    write_midi(src, [(0, 120)], [(0, 4)], {(gbk("贝斯"), 0, 33): [(i, 40) for i in range(8)],
                                          (gbk("鼓"), 9, 0): [(i, 36) for i in range(8)]})
    info = json.loads(run(src, "--list", "--json").stdout)
    expect([t["name"] for t in info["tracks"]] == ["贝斯", "鼓"], f"GB2312 names: {[t['name'] for t in info['tracks']]}")
    _, _, layers = convert(src, folder / "gb2312.nbs", label="GB2312 names")
    expect([l[0] for l in layers] == ["Bass 1", "Drums 2: Kick"], f"GB2312 names: layers {[l[0] for l in layers]}")
    _, got, _ = convert(src, folder / "gb2312-one.nbs", "--tracks", "贝斯", label="--tracks by a Chinese name")
    expect({n[2] for n in got} == {DOUBLE_BASS}, "--tracks 贝斯 did not pick the bass")

    sjis = folder / "shift-jis.mid"
    name = "ミュートベース".encode("shift_jis").decode("latin-1")
    write_midi(sjis, [(0, 120)], [(0, 4)], {(name, 0, 33): [(i, 40) for i in range(8)]})
    _, _, layers = convert(sjis, folder / "shift-jis.nbs", label="Shift-JIS name")
    expect([l[0] for l in layers] == ["Bass 1"], f"Shift-JIS name: layers {[l[0] for l in layers]} (want Bass 1)")

    src = folder / "digits.mid"
    write_midi(src, [(0, 120)], [(0, 4)], {("3", 0, 33): [(i, 40) for i in range(8)],
                                          ("1", 1, 73): [(i, 72) for i in range(8)],
                                          ("2", 9, 0): [(i, 36) for i in range(8)]})
    _, got, layers = convert(src, folder / "digits.nbs", "--tracks", "2", label="digit-named tracks")
    expect([l[0] for l in layers] == ["1"], f"--tracks 2 picked {[l[0] for l in layers]} (want the 2nd track, named 1)")

    src = folder / "type 0.mid"
    write_midi(src, [(0, 120)], [(0, 4)], {("Bass", 0, 33): [(i, 40) for i in range(8)],
                                          ("Drums", 9, 0): [(i, 38) for i in range(8)]}, midi_type=0)
    _, got, _ = convert(src, folder / "type 0.nbs", label="type-0 MIDI")
    expect({n[2] for n in got} == {DOUBLE_BASS, SNARE_DRUM}, f"type-0 MIDI: instruments {sorted({n[2] for n in got})}")

    big = folder / "huge.mid"
    big.write_bytes(b"MThd" + bytes(9 << 20))
    bad("over 8 MB of MIDI", big, says="8 MB")

    far = folder / "far first note.mid"                  # once: a tuple for every bar up to the first note
    mid = mido.MidiFile(type=0, ticks_per_beat=1)
    mid.tracks.append(mido.MidiTrack([mido.MetaMessage("time_signature", numerator=1, denominator=64),
                                      mido.Message("program_change", program=33, channel=0),
                                      mido.Message("note_on", note=40, velocity=90, time=0x0FFFFFFF),
                                      mido.Message("note_on", note=40, velocity=0, time=1)]))
    mid.save(str(far))
    started = time.perf_counter()
    convert(far, folder / "far first note.nbs", label="a first note 268 million beats in")
    expect(time.perf_counter() - started < 15, f"far first note: {time.perf_counter() - started:.1f} s")

    names = folder / "latin names.mid"
    cp1252 = lambda s: s.encode("cp1252").decode("latin-1")  # noqa: E731
    write_midi(names, [(0, 120)], [(0, 4)], {(cp1252("Canção"), 0, 33): [(i, 40) for i in range(8)],
                                            (cp1252("Don’t Stop"), 1, 27): [(i, 52) for i in range(8)]})
    info = json.loads(run(names, "--list", "--json").stdout)
    expect([t["name"] for t in info["tracks"]] == ["Canção", "Don’t Stop"],
           f"Latin-1 / cp1252 names: {[t['name'] for t in info['tracks']]}")
    _, _, layers = convert(names, folder / "latin names.nbs", "--tracks", "all", label="Latin-1 / cp1252 names")
    expect([l[0] for l in layers] == ["Cancao", "Don't Stop"], f"Latin-1 / cp1252 names: layers {[l[0] for l in layers]}")

    two = folder / "two guitars.mid"
    write_midi(two, [(0, 120)], [(0, 4)], {("Bass", 0, 33): [(i, 40) for i in range(8)],
                                          ("Gtr A", 1, 27): [(i, 52) for i in range(8)],
                                          ("Gtr B", 2, 27): [(i, 55) for i in range(8)]})
    _, got, layers = convert(two, folder / "two guitars.nbs", "--tracks", "bass,3", "--instrument", "guitar=harp",
                             label="--instrument by kind, one of two guitars converted")
    expect({n[2] for n in got if layers[n[1]][0] == "Gtr B"} == {PIANO}, "--instrument guitar=harp: Gtr B not on the Piano")
    _, got, _ = convert(two, folder / "two guitars all.nbs", "--tracks", "all", "--instrument", "all=bit",
                        label="--instrument all=bit")
    expect({n[2] for n in got} == {13}, f"--instrument all=bit: instruments {sorted({n[2] for n in got})}")
    bad("instrument for an unconverted number", two, "--tracks", "bass,3", "--instrument", "2=harp", says="not converted")
    bad("--ticks-per-beat 0", two, "--ticks-per-beat", "0", says="2..12")
    return sjis


def check_gp_edges():
    print("4. Guitar Pro edge cases")
    folder = fresh("gp-edges")
    bomb = folder / "bomb.gp"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Content/score.gpif", "<GPIF></GPIF>")
        z.writestr("Content/padding.bin", bytes(70 << 20))
    expect(bomb.stat().st_size < 1 << 20, "zip bomb: the test file is not small")
    bad("a .gp unpacking to over 64 MB", bomb, says="64 MB")
    empty = folder / "no score.gp"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("Content/other.txt", "nothing")
    bad("a zip without a score", empty, says="alphaTab")
    gpx = folder / "huge.gpx"
    gpx.write_bytes(b"BCFZ" + (1 << 30).to_bytes(4, "little") + bytes(64))
    bad("a .gpx declaring 1 GB", gpx, says="64 MB")

    data, _ = gp_song(repeat_count=100000)
    src = folder / "repeat 100000.gp"
    src.write_bytes(data)
    started = time.perf_counter()
    _, got, _ = convert(src, folder / "repeat 100000.nbs", label="repeat count 100000")
    expect(time.perf_counter() - started < 30 and sum(n[2] == DOUBLE_BASS for n in got) == 4 + 8 * 8 + 5,
           f"repeat count 100000: {sum(n[2] == DOUBLE_BASS for n in got)} bass notes (want 73: bars 2-3 played 8 "
           f"times), {time.perf_counter() - started:.1f} s")

    data, played = gp_song(first_bar_beats=1)
    src = folder / "anacrusis.gp"
    src.write_bytes(data)
    head, got, _ = convert(src, folder / "anacrusis.nbs", label="anacrusis")
    kick_ticks = sorted(n[0] for n in got if n[2] == 2)
    expect(kick_ticks[:2] == [12, 16] and head["time_signature"] == 4,
           f"anacrusis: kicks at {kick_ticks[:3]}, want the pickup's at 12 and the first downbeat's at 16")

    data, played = gp_song(dead_slap=True)
    src = folder / "dead slap.gp"
    src.write_bytes(data)
    _, got, layers = convert(src, folder / "dead slap.nbs", label="dead slap")
    at_zero = sorted(n[3] for n in got if n[2] == DOUBLE_BASS and n[0] == 0)
    expect(at_zero == [], f"dead slap: bass keys {at_zero} at tick 0 (want none: the slapped beat has no pitch)")
    clicks = [(n[0], n[2], n[3]) for n in got if layers[n[1]][0] == "Electric Bass: Dead notes"]
    expect(clicks == [(0, *DEAD_CLICK)], f"dead slap: {clicks} on the dead notes layer (want one side-stick click "
                                         "at tick 0, not one per open string)")
    hits = [t["dead"] for t in json.loads(run(src, "--list", "--json").stdout)["tracks"]]
    expect(hits == [1, 0, 0], f"dead slap: --list counts dead hits {hits} (want [1, 0, 0]: one, not one per string)")
    expect(sum(n[2] == DOUBLE_BASS for n in got) == len(played["Electric Bass"]) - 1,
           "dead slap: other bass notes missing or added")


# ---------- 5. grid choice ----------

def check_grids():
    print("5. Grid choice")
    eighths = [i / 2 for i in range(64)]
    sixteenths = [i / 4 for i in range(64)]
    shuffle = [i + f for i in range(32) for f in (0, 2 / 3)]
    for label, beats, bpm, want in (("8ths at 105", eighths, 105, 6), ("16ths at 110", sixteenths, 110, 4),
                                    ("16ths at 105", sixteenths, 105, 8),
                                    ("shuffle at 100", shuffle, 100, 6), ("16ths at 150", sixteenths, 150, 4),
                                    ("16ths at 80", sixteenths, 80, 8)):
        options, exact = export_nbs.grid_options(beats, bpm)
        expect(exact and options[0]["tpb"] == want, f"{label}: {options[0]['tpb']} ticks per beat, want {want}")
    jittered = [b + (0.03 if i % 2 else -0.03) for i, b in enumerate(eighths)]
    options, exact = export_nbs.grid_options(jittered, 105)
    expect(not exact and options[0]["tpb"] in (4, 8), f"jittered 8ths at 105: {options[0]['tpb']} (want 16ths)")
    expect(export_nbs.song_start([(0.0, 2, 4), (2.0, 4, 4)], 0.5) == (-2.0, 4.0), "pickup padding")
    expect(export_nbs.song_start([(0.0, 4, 4)], 5.0) == (4.0, 4.0), "start of the first note's bar")
    expect(export_nbs.fold(20) == 44 and export_nbs.fold(70) == 46 and export_nbs.fold(45) == 45, "octave folding")
    expect(export_nbs.ascii_text("电动少女（Live）") == "" and export_nbs.ascii_text("Sigur Rós") == "Sigur Ros",
           "ASCII names")


# ---------- 6. an OpenMusic song folder ----------

def check_song_folder():
    print("6. OpenMusic song folder")
    song = fresh("library") / "Test Band - 测试歌"
    (song / "transcription").mkdir(parents=True)
    write_midi(song / "transcription" / "bass.mid", [(0, 120)], [(0, 4)],
               {("Bass", 0, 33): [(i / 2, 33 + i % 7) for i in range(32)]})
    write_midi(song / "transcription" / "drums.mid", [(0, 120)], [(0, 4)],
               {("Drums", 9, 0): [(i / 2, 42) for i in range(32)]})
    write_midi(song / "transcription" / "vocals.mid", [(0, 120)], [(0, 4)],
               {("Vocals (melody)", 0, 53): [(i, 60 + i % 12) for i in range(16)]})
    (song / "bass.mp3").write_bytes(os.urandom(2048))
    (song / "stems.json").write_text('{"model": "test"}')
    before = tree(song)
    out = export_nbs.OUTPUT_DIR / f"{song.name}.nbs"            # the default place, without --out
    if out.exists():
        print(f"  (skipped the default place: {out.name} is already in {export_nbs.OUTPUT_DIR.name}/)")
    else:
        try:
            run(song)
            if expect(out.is_file(), f"song folder: no {out.name} in minecraft/output_nbs/"):
                head, notes, layers = check_file(out, "song folder")
                expect([l[0] for l in layers] == ["bass", "drums: Hi-hat"],
                       f"song folder: layers {[l[0] for l in layers]}")
                expect(head["original_author"] == "Test Band", f"song folder: author {head['original_author']!r}")
        finally:
            out.unlink(missing_ok=True)                          # the test's own file only
    expect(tree(song) == before, "song folder: its files changed")
    _, notes, layers = convert(song, TEST_ROOT / "library" / "all.nbs", "--tracks", "all", label="song folder --tracks all")
    vocals = next((i for i, l in enumerate(layers) if l[0] == "vocals"), None)
    expect(vocals is not None and {n[2] for n in notes if n[1] == vocals} == {PIANO},
           f"song folder --tracks all: the vocals (60-71) are not on the Piano: {[l[0] for l in layers]}")


# ---------- 8. the workbench ----------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port, path, body=b"", headers=None, method="POST"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), data


def check_workbench(gp_file, cli_nbs, sjis_mid):
    print("8. Workbench /api/nbs")
    port = free_port()
    tmp = fresh("workbench-tmp")
    env = dict(os.environ, TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp))   # its own temp folder
    server = subprocess.Popen([sys.executable, str(ROOT / "experiments" / "app.py"), "--port", str(port),
                               "--no-browser"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(150):
            try:
                if request(port, "/api/state", method="GET", headers={"Host": "localhost"})[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        data = gp_file.read_bytes()
        h = {"Host": "localhost", "X-OpenMusic": "1", "Content-Type": "application/octet-stream",
             "X-Filename": quote(gp_file.name)}
        status, _, body = request(port, "/api/nbs?list=1", data, h)
        info = json.loads(body) if status == 200 else {}
        expect(status == 200 and [t["name"] for t in info.get("tracks", [])] == ["Electric Bass", "Drumkit", "吉他"],
               f"workbench list: {status} {body[:200]!r}")
        status, _, body = request(port, "/api/nbs?list=1", sjis_mid.read_bytes(), dict(h, **{"X-Filename": "sjis.mid"}))
        expect(status == 200 and len(json.loads(body).get("tracks", [])) == 1,
               f"workbench list of a Shift-JIS name (U+0085): {status} {body[:200]!r}")
        status, headers, body = request(port, "/api/nbs?tracks=1,2", data, h)
        expect(status == 200 and body == cli_nbs.read_bytes(), f"workbench .nbs: {status}, not the command line's file")
        summary = json.loads(unquote(headers.get("X-NBS-Summary", "%7B%7D")))
        expect(set(summary) == {"bpm", "minecraft_bpm", "note_blocks", "seconds"} and summary["minecraft_bpm"] == 150
               and summary["note_blocks"] == len(read_nbs(cli_nbs)[1]), f"workbench summary: {summary}")
        for label, path, body, hdr, want in (
                ("no header", "/api/nbs?list=1", data, {"Host": "localhost"}, 403),
                ("foreign host", "/api/nbs?list=1", data, dict(h, Host="evil.example"), 403),
                ("not a score", "/api/nbs?list=1", b"hello world", h, 400),
                ("bad tracks", "/api/nbs?tracks=1;2", data, h, 400),
                ("non-ASCII digits", "/api/nbs?tracks=" + quote("١"), data, h, 400),
                ("unknown track", "/api/nbs?tracks=7", data, h, 422)):
            status, _, reply = request(port, path, body, hdr)
            expect(status == want, f"workbench {label}: {status} {reply[:120]!r}, want {want}")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)     # declared too large: refused unread
        conn.putrequest("POST", "/api/nbs?list=1")
        for k, v in dict(h, **{"Content-Length": str(65 * 1024 * 1024)}).items():
            conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        expect(r.status == 413, f"workbench too large: {r.status}")
        conn.close()
        expect(not (ROOT / "songs" / gp_file.name).exists(), "workbench: the upload landed in songs/")
        for _ in range(20):                       # the last reply can come just before its temp folder is removed
            if not any(tmp.iterdir()):
                break
            time.sleep(0.1)
        expect(not any(tmp.iterdir()), f"workbench left temp files: {[p.name for p in tmp.iterdir()]}")
    finally:
        server.terminate()
        server.wait(timeout=10)


# ---------- real files ----------

def check_real(path, label):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "song.nbs"
        done = run(path, "--out", out)
        if done.returncode or not expect(out.is_file(), f"{label}: exit 0 but no {out.name}"):
            return
        head, notes, layers = check_file(out, label)
        names = [l[0] for l in layers]
        expect(any(n[2] == DOUBLE_BASS for n in notes) or not any("bass" in n.lower() for n in names),
               f"{label}: no bass notes")
        expect(any(n[2] in (2, 3, 4) for n in notes), f"{label}: no drum notes")
        print(f"  {label}: {len(notes)} note blocks, {head['length']} ticks, {len(layers)} layers")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gp", action="append", default=[], help="also convert this Guitar Pro or MIDI file")
    ap.add_argument("--songs", help="also convert every song (MIDI and .gp) in this stems folder")
    ap.add_argument("--no-workbench", action="store_true", help="skip the workbench check")
    args = ap.parse_args()
    start = time.perf_counter()
    gp_file, cli_nbs = check_gp()
    check_midi()
    sjis = check_midi_edges()
    check_gp_edges()
    check_grids()
    check_song_folder()
    print("7. Command-line errors: checked with each case above")
    if not args.no_workbench:
        check_workbench(gp_file, cli_nbs, sjis)
    for path in args.gp:
        check_real(path, Path(path).name)
    if args.songs:
        for song in sorted(p for p in Path(args.songs).iterdir() if (p / "transcription").is_dir()):
            if any((song / "transcription").glob("*.mid")):
                check_real(song, f"{song.name} (MIDI)")
            for g in (song / "transcription").glob("*.gp"):
                check_real(g, f"{song.name} (.gp)")
    print(f"{len(FAILURES)} failures, {time.perf_counter() - start:.1f} s" if FAILURES
          else f"all checks passed, {time.perf_counter() - start:.1f} s")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
