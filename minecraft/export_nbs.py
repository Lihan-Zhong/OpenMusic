"""Turn a Guitar Pro score or a MIDI file into an Open Note Block Studio song (.nbs) for Minecraft note blocks.

    .venv/bin/python minecraft/export_nbs.py "songs/电动少女(Live).gp"
    .venv/bin/python minecraft/export_nbs.py "songs/电动少女(Live).gp" --list      # its tracks; writes nothing
    .venv/bin/python minecraft/export_nbs.py "outputs/roformer_sw_full/<song>"     # OpenMusic's own MIDI
    .venv/bin/python minecraft/export_nbs.py score.mid --tracks bass,drums,3 --out song.nbs

The input is a Guitar Pro file (.gp from Guitar Pro 7/8; also .gpx and .gp3-.gp5), a MIDI file, or an OpenMusic
song folder (its transcription/<stem>.mid files). A Guitar Pro file goes through alphaTab (vendored for
export_gp.py; experiments/gp7.py), which turns it into MIDI as Guitar Pro plays it: every voice, repeats and
endings in playing order, a tied note once (a dead-slapped beat stays silent, as it has no pitch). So the way to a
clean result is to fix a transcription in Guitar Pro 8 and convert the saved file. By default the bass and the
drums are converted (what OpenMusic transcribes best); --tracks names others: bass, drums, guitar, piano, melody,
all, or track numbers / names as --list shows them (a number always means the --list number).

Minecraft shapes the result:
- Time: redstone moves in steps of 0.1 s, so the song plays at 10 ticks per second (Note Block Studio's
  structure export then puts one redstone tick per tick) and every beat gets a whole number of ticks: 4 is
  150 BPM with exact 16th notes, 6 is 100 BPM with exact 8ths and triplets, 8 is 75 BPM, and so on.
  --ticks-per-beat chooses; by default it is the number that changes the tempo least without moving notes off
  their rhythm (a note more than 0.2 ticks from every tick counts as moved, and OFF_RHYTHM weighs that against
  the tempo change). Notes go to the nearest tick (OpenMusic's drum hits sit about 10 ms early, so cutting
  down would move most of them a whole 16th early). Tempo changes inside a song are not kept: it plays at one
  tempo, and its bars stay bars because every beat gets the same number of ticks.
- Pitch: a note block instrument covers two octaves (the Piano F#3-F#5, the Double Bass two octaves lower, ...).
  Notes outside go up or down whole octaves into range, so Note Block Studio's export leaves none of them out.
- Sound: a part plays on the instrument Open Note Block Studio's MIDI import gives its General MIDI program
  (PROGRAMS: a bass on the Double Bass, wood under the note block; a clean guitar on the Guitar, wool; a voice
  on the Flute, clay; a piano on the Piano), unless another of CHOICES holds BETTER_FIT more of its notes in its
  two octaves (most vocal melodies end up on the Piano). The drums play each kit piece on the instrument and at
  the pitch of Note Block Studio 3.12's drum map (DRUMS): kick and toms on the Bass Drum (stone), closed and
  pedal hi-hats on the Click (glass), the open hi-hat, snare and cymbals on the Snare Drum (sand), the ride bell
  on the Bell, the cowbell on the Cow Bell. --instrument TRACK=NAME sets a pitched part's instrument (NAME as in
  INSTRUMENTS; TRACK as for --tracks, and it must be one of the converted tracks).
- Loudness: note blocks have none, and Note Block Studio's structure export ignores velocities. So every note
  is written at full velocity and Note Block Studio plays the song as the structure will; --velocities keeps
  the score's (for Note Block Studio itself or a data pack).
- Height: Note Block Studio stacks note blocks four to a level, filling the lowest level from the highest
  layers. The Snare Drum sits on sand, which falls when nothing is under it, so the layers of sand
  instruments come last: they always fill the lowest level.
- Text: Note Block Studio keeps one byte per character, so names are written in ASCII; Chinese ones are
  replaced (the song by "OpenMusic song", a track by its kind and its --list number). The file name stays.

Each track gets its own layers in Note Block Studio, named after it (a drum kit one per kit piece). The file is
format version 5, which Note Block Studio 3.11 (Windows) and 3.12 (macOS too) both open; it is written with
pynbs (MIT). Open it in Note Block Studio, listen, adjust, and export it as a structure for a Java world (see
minecraft/README.md). The .nbs of a song folder goes to <song>/minecraft/<song>.nbs and that of a file next to
it, unless --out says where; the input is only read. Errors are one line on stderr and a non-zero exit code.
"""
import argparse
import io
import json
import math
import re
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import mido
import pynbs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))      # gp7 (alphaTab) for .gp files

for _stream in (sys.stdout, sys.stderr):           # Chinese names on a GBK / cp1252 console: '?' rather than a crash
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

TICKS_PER_SECOND = 10        # one redstone tick is 0.1 s
LOW, HIGH = 33, 57           # Note Block Studio keys (0 = A0 = MIDI 21) that note blocks play: F#3-F#5 on the Piano
MAX_TICK = 32767             # Note Block Studio reads ticks and layers as signed 16-bit numbers
FIT = 0.2                    # a note within 0.2 ticks (20 ms) of a tick keeps its rhythm
QUANTISED = 0.05             # notes whose median distance from 24ths of a beat is below 0.05 of a 24th sit on a grid
OFF_RHYTHM = 3.0             # choosing ticks per beat: every note moved off its rhythm weighs as a tempo change of e^3
BETTER_FIT = 0.15            # another instrument replaces the program's one when it holds 15% more of the notes
MAX_MIDI = 8 << 20           # a score's MIDI is ~100 KB; 8 MB is over a million notes, far more than a build holds
STEMS = ("vocals", "bass", "guitar", "piano", "other", "drums")   # an OpenMusic song's transcription/<stem>.mid
STEM_KINDS = {"vocals": "melody", "bass": "bass", "guitar": "guitar", "piano": "piano", "other": "melody",
              "drums": "drums"}
DEFAULT_KINDS = ("bass", "drums")
FALLBACK_TITLE = "OpenMusic song"

# Note block instruments: Note Block Studio's index, its name, the block under the note block, and the MIDI pitch
# of key 33 (the instrument's lowest note); None for the drums, whose key only sets how high they sound.
INSTRUMENTS = {
    "harp": (0, "Piano", "any other block, e.g. dirt", 54),
    "bass": (1, "Double Bass", "wood", 30),
    "basedrum": (2, "Bass Drum", "stone", None),
    "snare": (3, "Snare Drum", "sand", None),
    "hat": (4, "Click", "glass", None),
    "guitar": (5, "Guitar", "wool", 42),
    "flute": (6, "Flute", "clay", 66),
    "bell": (7, "Bell", "gold block", 78),
    "chime": (8, "Chime", "packed ice", 78),
    "xylophone": (9, "Xylophone", "bone block", 78),
    "iron_xylophone": (10, "Iron Xylophone", "iron block", 54),
    "cow_bell": (11, "Cow Bell", "soul sand", 66),
    "didgeridoo": (12, "Didgeridoo", "pumpkin", 30),
    "bit": (13, "Bit", "emerald block", 54),
    "banjo": (14, "Banjo", "hay bale", 54),
    "pling": (15, "Pling", "glowstone", 54),
}
# PROGRAMS and DRUMS are copied from Open Note Block Studio's MIDI import tables (scripts/midi_instruments/
# midi_instruments.gml at v3.12.0-rc.1; at v3.11.0 for the 12 programs where 3.12 uses its trumpets, which
# Minecraft only has since 26.1 and a version-5 file cannot hold), which 3.12 credits to u3002's "General MIDI <->
# Minecraft Note Block Mapping" (v2025-12-16, https://gist.github.com/u3002/cf4daa83bc82b5917fc86fb23815578a).
# The octave column is left out: every note is folded into its instrument's two octaves anyway. Both are MIT:
#   Copyright (c) 2024-2026 OpenNBS
#   Copyright (c) u3002
#   Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
#   documentation files (the "Software"), to deal in the Software without restriction, including without
#   limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the
#   Software, and to permit persons to whom the Software is furnished to do so, subject to the following
#   conditions: The above copyright notice and this permission notice shall be included in all copies or
#   substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
#   OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE
#   AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
#   OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
#   WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# PROGRAMS: the instrument each General MIDI program 0-127 plays on.
PROGRAMS = ("harp pling pling pling harp harp guitar banjo bell bell bell iron_xylophone iron_xylophone xylophone "
            "bell guitar flute iron_xylophone flute flute flute flute flute flute guitar guitar harp guitar bass "
            "didgeridoo guitar guitar bass bass bass bass guitar guitar bass pling flute flute flute didgeridoo "
            "flute bass harp snare flute flute flute flute flute flute flute snare flute flute flute didgeridoo "
            "flute didgeridoo didgeridoo flute flute flute flute flute flute flute flute flute flute flute flute "
            "flute flute flute flute flute bit flute flute flute guitar flute flute bass bell flute flute flute "
            "flute flute flute chime chime flute chime guitar pling flute flute guitar banjo banjo banjo guitar "
            "iron_xylophone flute flute flute chime cow_bell iron_xylophone xylophone basedrum basedrum basedrum "
            "snare hat flute chime flute bell basedrum snare snare").split()
# Per kind of part, the instruments that may replace the program's one when they fit its range clearly better.
CHOICES = {"bass": ["bass"], "guitar": ["guitar", "harp"], "piano": ["harp", "guitar", "flute"],
           "melody": ["harp", "flute", "guitar", "bell"]}
# DRUMS: General MIDI drum notes -> (kit piece, instrument, key) (3.12's drum map; see above). Keys outside 33-57
# are folded like notes. A note not listed plays as OTHER_DRUM.
DRUMS = {
    24: ("Concert Snare", "snare", 45), 25: ("Snare Roll", "snare", 38), 26: ("Finger Snap", "hat", 51),
    27: ("High Q", "snare", 35), 28: ("Slap", "snare", 42), 29: ("Scratch Push", "hat", 39),
    30: ("Scratch Pull", "hat", 35), 31: ("Sticks", "hat", 46), 32: ("Square Click", "hat", 42),
    33: ("Metronome Click", "hat", 48), 34: ("Metronome Bell", "chime", 51),
    35: ("Kick", "basedrum", 37), 36: ("Kick", "basedrum", 41), 37: ("Side Stick", "hat", 41),
    38: ("Snare", "snare", 48), 39: ("Hand Clap", "snare", 52), 40: ("Snare", "snare", 49),
    41: ("Floor Tom", "basedrum", 39), 42: ("Hi-hat", "hat", 54), 43: ("Floor Tom", "basedrum", 42),
    44: ("Pedal Hi-hat", "hat", 56), 45: ("Low Tom", "basedrum", 47), 46: ("Open Hi-hat", "snare", 55),
    47: ("Mid Tom", "basedrum", 50), 48: ("High Tom", "basedrum", 53), 49: ("Crash", "snare", 53),
    50: ("High Tom", "basedrum", 56), 51: ("Ride", "snare", 50), 52: ("China", "snare", 47),
    53: ("Ride Bell", "bell", 50), 54: ("Tambourine", "snare", 56), 55: ("Splash", "snare", 51),
    56: ("Cowbell", "cow_bell", 39), 57: ("Crash", "snare", 54), 58: ("Vibraslap", "hat", 50),
    59: ("Ride", "snare", 57), 60: ("High Bongo", "cow_bell", 49), 61: ("Low Bongo", "cow_bell", 42),
    62: ("Mute High Conga", "hat", 30), 63: ("Open High Conga", "cow_bell", 32), 64: ("Low Conga", "cow_bell", 24),
    65: ("High Timbale", "cow_bell", 38), 66: ("Low Timbale", "cow_bell", 29), 67: ("High Agogo", "xylophone", 45),
    68: ("Low Agogo", "xylophone", 38), 69: ("Cabasa", "hat", 68), 70: ("Maracas", "hat", 65),
    71: ("Short Whistle", "flute", 67), 72: ("Long Whistle", "flute", 66), 73: ("Short Guiro", "hat", 52),
    74: ("Long Guiro", "hat", 53), 75: ("Claves", "xylophone", 52), 76: ("High Wood Block", "xylophone", 40),
    77: ("Low Wood Block", "xylophone", 34), 78: ("Mute Cuica", "didgeridoo", 55), 79: ("Open Cuica", "didgeridoo", 46),
    80: ("Mute Triangle", "bell", 52), 81: ("Open Triangle", "chime", 52), 82: ("Shaker", "hat", 69),
    83: ("Jingle Bell", "bell", 54), 84: ("Bell Tree", "chime", 50), 85: ("Castanets", "xylophone", 48),
    86: ("Mute Surdo", "basedrum", 45), 87: ("Open Surdo", "basedrum", 40), 88: ("Applause", "snare", 43),
}
OTHER_DRUM = ("Percussion", "hat", 45)
KIND_NAMES = {"bass": "Bass", "drums": "Drums", "guitar": "Guitar", "piano": "Piano", "melody": "Melody"}


# ---------- reading ----------

def latin_words(text):
    """Whether every run of non-ASCII characters in a text read as Latin-1 sits inside a word: Latin-1 letters with
    an ASCII letter on each side, as in Canção or Größe. GB2312 bytes read so are seldom all letters, and a
    Chinese name seldom sits between ASCII letters."""
    runs = list(re.finditer(r"[^\x00-\x7f]+", text))
    return bool(runs) and all(
        m.start() > 0 and m.end() < len(text) and text[m.start() - 1].isascii() and text[m.start() - 1].isalpha()
        and text[m.end()].isascii() and text[m.end()].isalpha() and m.group().isalpha() for m in runs)


def fix_text(text):
    """mido reads MIDI text as Latin-1. Most files (alphaTab's too) hold UTF-8; older Chinese ones GB2312 (strict:
    GBK would also read many accented Latin-1 names as Chinese, so a Latin-1 reading that looks like European
    words stays); Western Windows ones cp1252 (its quotes and dashes are C1 controls in Latin-1)."""
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return text
    codecs = ["utf-8"] + ["gb2312"] * (not latin_words(text)) + ["cp1252"] * any("\x80" <= c <= "\x9f" for c in text)
    for codec in codecs:
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            pass
    return text


def read_midi(data):
    """(parts, tempos, signatures) of MIDI bytes: parts = {(track, channel): part} with notes as (beat, pitch,
    velocity), a beat being a MIDI quarter note; tempos = [(beat, BPM)]; signatures = [(beat, num, den)]."""
    if len(data) > MAX_MIDI:
        raise SystemExit(f"export_nbs.py: {len(data) / (1 << 20):.0f} MB of MIDI is more than note blocks can play "
                         f"(at most {MAX_MIDI >> 20} MB)")
    try:   # clip: alphaTab writes a drum track's program -1 as the data byte 0xFF, which strict mido refuses
        mid = mido.MidiFile(file=io.BytesIO(data), clip=True)
    except (OSError, EOFError, ValueError, IndexError, KeyError) as e:
        raise SystemExit(f"export_nbs.py: not a readable MIDI file ({e})")
    ppq = mid.ticks_per_beat
    parts, tempos, signatures, channel_programs = {}, [], [], {}
    for index, track in enumerate(mid.tracks):
        tick, name, programs = 0, None, {}
        for msg in track:
            tick += msg.time
            if msg.type == "set_tempo" and msg.tempo > 0:
                tempos.append((tick / ppq, mido.tempo2bpm(msg.tempo)))
            elif msg.type == "time_signature" and msg.numerator >= 1 and msg.denominator <= 64:
                signatures.append((tick / ppq, msg.numerator, msg.denominator))   # a broken one is left out
            elif msg.type == "track_name" and name is None:
                name = fix_text(msg.name).strip()
            elif msg.type == "program_change":
                programs[msg.channel] = channel_programs[msg.channel] = msg.program   # the last one counts
            elif msg.type == "note_on" and msg.velocity > 0:
                part = parts.setdefault((index, msg.channel), {"channel": msg.channel, "notes": []})
                part["notes"].append((tick / ppq, msg.note, msg.velocity))
        for (i, channel), part in parts.items():
            if i == index:
                part["name"] = name or ""
                part["program"] = programs.get(channel)
    for part in parts.values():   # channel state is the file's: a setup track may hold the program changes
        if part["program"] is None:
            part["program"] = channel_programs.get(part["channel"], 0)
    return parts, sorted(tempos) or [(0.0, 120.0)], sorted(signatures) or [(0.0, 4, 4)]


def score_type(data):
    """'guitarpro' for the files alphaTab reads (.gp = a zip, .gpx = BCFZ/BCFS, .gp3-.gp5 = 'FICHIER GUITAR
    PRO' after a length byte), 'midi', or None."""
    if data[:4] == b"PK\x03\x04" or data[:3] == b"BCF" or data[1:19] == b"FICHIER GUITAR PRO":
        return "guitarpro"
    return "midi" if data[:4] == b"MThd" else None


def kind_of(channel, program):
    if channel == 9:
        return "drums"
    if 32 <= program <= 39:
        return "bass"
    if 24 <= program <= 31:
        return "guitar"
    return "piano" if program <= 7 else "melody"


def load(path):
    """(song, parts): song = {title, artist, tempos, signatures, source}; parts in score order, each
    {number (1-based, as --list shows), name, kind, program, notes, muted}. A part is a Guitar Pro track, a MIDI
    track and channel, or an OpenMusic stem."""
    song, parts = read_input(Path(path))
    for i, p in enumerate(parts, 1):
        p["number"] = i
    return song, parts


def read_input(path):
    if path.is_dir():
        files = [(stem, path / "transcription" / f"{stem}.mid") for stem in STEMS]
        files = [(stem, f) for stem, f in files if f.is_file()]
        if not files:
            raise SystemExit(f"export_nbs.py: no transcription/<stem>.mid in {path}")
        parts, tempos, signatures = [], None, None
        for stem, f in files:
            found, t, s = read_midi(f.read_bytes())
            tempos, signatures = tempos or t, signatures or s     # every stem has the song's tempo map
            notes = sorted(n for part in found.values() for n in part["notes"])
            program = next((p["program"] for p in found.values()), 0)
            if notes:
                parts.append({"name": stem, "kind": STEM_KINDS[stem], "program": program, "notes": notes,
                              "muted": False})
        artist, _, title = path.name.rpartition(" - ")
        return {"title": title, "artist": artist, "tempos": tempos, "signatures": signatures,
                "source": path.name}, parts
    if not path.is_file():
        raise SystemExit(f"export_nbs.py: no such file or folder: {path}")
    data = path.read_bytes()
    if score_type(data) == "guitarpro":
        import gp7
        try:
            midi, info = gp7.to_midi(data)
        except Exception as e:   # an alphaTab error comes through V8 with its minified source: keep its first line
            reason = re.sub(r"^<anonymous>:\d+: ", "", (str(e).strip().splitlines() or [type(e).__name__])[0])
            raise SystemExit(f"export_nbs.py: {path.name}: not a Guitar Pro file alphaTab can read ({reason[:300]})")
        found, tempos, signatures = read_midi(midi)
        pickup = info.get("pickup") or 0   # Guitar Pro's anacrusis bar: alphaTab writes no time signature for it
        if signatures and 0 < pickup < signatures[0][1] * 4 / signatures[0][2] - 1e-9 and \
                not any(abs(at - pickup) < 1e-9 for at, _, _ in signatures):
            signatures = sorted(signatures + [(pickup, signatures[0][1], signatures[0][2])])
        tracks, merged = info["tracks"], {}
        for (index, channel), part in sorted(found.items()):
            if index >= len(tracks):
                continue
            t = tracks[index]                            # MIDI track i is the score's track i, on 1 or 2 channels
            program = min(max(int(t["program"] or 0), 0), 127)    # a drum track says -1
            entry = merged.setdefault(index, {"name": t["name"], "program": program, "notes": [],
                                              "kind": "drums" if t["percussion"] else kind_of(channel, program),
                                              "muted": t["mute"]})
            entry["notes"] += part["notes"]
        parts = [dict(merged[i], notes=sorted(merged[i]["notes"])) for i in sorted(merged)]
        return {"title": info["title"] or path.stem, "artist": info["artist"], "tempos": tempos,
                "signatures": signatures, "source": path.name}, parts
    if score_type(data) != "midi":
        raise SystemExit(f"export_nbs.py: {path.name} is neither a Guitar Pro file (.gp, .gpx, .gp5, .gp4, .gp3) "
                         "nor a MIDI file")
    found, tempos, signatures = read_midi(data)
    parts = [{"name": p["name"], "kind": kind_of(p["channel"], p["program"]), "program": p["program"],
              "notes": sorted(p["notes"]), "muted": False} for _, p in sorted(found.items())]
    return {"title": path.stem, "artist": "", "tempos": tempos, "signatures": signatures,
            "source": path.name}, parts


def matches(token, part):
    """Whether a --tracks / --instrument token names a part: its --list number (digits always mean the number,
    so a track named "2" is not picked by "2"), 'all', its kind, or its name (any case)."""
    t = token.strip().casefold()
    if t.isascii() and t.isdigit():
        return t == str(part["number"])
    return t in ("all", part["kind"], part["name"].strip().casefold())


def select(parts, spec):
    """The parts --tracks names (comma-separated kinds, --list numbers, names, or 'all'); by default the parts
    whose kind is bass or drums (the ones --list marks)."""
    if not spec:
        chosen = [p for p in parts if p["kind"] in DEFAULT_KINDS]
        if not chosen:
            raise SystemExit("export_nbs.py: no bass or drum track (see --list; --tracks all)")
        return chosen
    tokens = [t.strip() for t in spec.split(",") if t.strip()]
    unknown = [t for t in tokens if t.casefold() not in {"all", "drums", *CHOICES}
               and not any(matches(t, p) for p in parts)]
    if unknown:
        raise SystemExit(f"export_nbs.py: no track {', '.join(unknown)} (see --list)")
    chosen = [p for p in parts if any(matches(t, p) for t in tokens)]
    if not chosen:
        raise SystemExit("export_nbs.py: no track is " + " or ".join(tokens) + " (see --list; --tracks all)")
    return chosen


def instrument_choices(items, parts, chosen):
    """{part number: instrument} from --instrument TRACK=NAME items, TRACK as for --tracks: it applies to the
    converted pitched tracks it names. An error when it names no track, only drum tracks, or (a number, or a
    name or kind of which no track is converted) nothing that is converted."""
    out = {}
    for item in items:
        track, _, name = item.rpartition("=")
        if not track.strip() or name not in INSTRUMENTS or INSTRUMENTS[name][3] is None:
            raise SystemExit(f"export_nbs.py: --instrument {item}: write TRACK=NAME with NAME one of "
                             + ", ".join(n for n, v in INSTRUMENTS.items() if v[3] is not None))
        named = [p for p in parts if matches(track, p)]
        if not named:
            raise SystemExit(f"export_nbs.py: --instrument {item}: no track {track} (see --list)")
        pitched = [p for p in named if p["kind"] != "drums"]
        if not pitched:
            raise SystemExit(f"export_nbs.py: --instrument {item}: a drum kit keeps each piece's own instrument")
        converted = [p for p in pitched if any(p is c for c in chosen)]
        if not converted:
            numbers = ", ".join(str(p["number"]) for p in pitched)
            raise SystemExit(f"export_nbs.py: --instrument {item}: track {numbers} is not converted "
                             "(add it to --tracks)")
        out.update((p["number"], name) for p in converted)
    return out


# ---------- arranging ----------

def bpm_of(tempos, start, end):
    """The tempo most of start..end (in beats) is played at: the median weighted by beats. (The first tempo of
    an OpenMusic MIDI is often a squeezed lead-in beat, 1304 BPM in one song.)"""
    spans = []
    for i, (at, bpm) in enumerate(tempos):
        a, b = max(at, start), min(tempos[i + 1][0] if i + 1 < len(tempos) else math.inf, end)
        if b > a:
            spans.append((bpm, b - a))
    if not spans:
        return next((bpm for at, bpm in reversed(tempos) if at <= start), tempos[0][1])
    spans.sort()
    half, acc = sum(w for _, w in spans) / 2, 0.0
    for bpm, w in spans:
        acc += w
        if acc >= half:
            return bpm


def song_start(signatures, first):
    """(start beat, beats per bar): the start of the bar holding the first note, moved back so that a short
    first bar (a pickup) is a whole bar and the next bar starts on one of Note Block Studio's bar lines. A time
    signature event starts a bar, as in MIDI, and one that comes mid-bar cuts the bar before it short. Worked
    out from the signature in force, not by walking the bars from the start (a first note far in costs
    nothing)."""
    sigs = sorted(signatures)
    if sigs[0][0] > 0:
        sigs.insert(0, (0.0, 4, 4))
    i = max(j for j, (at, _, _) in enumerate(sigs) if at <= first + 1e-9)
    at, num, den = sigs[i]
    stop = sigs[i + 1][0] if i + 1 < len(sigs) else math.inf
    length = num * 4 / den
    start = at + max(0, math.floor((first - at) / length + 1e-9)) * length
    this = min(length, stop - start)                   # the next time signature may cut this bar short
    full = length if start + this < stop - 1e-9 else sigs[i + 1][1] * 4 / sigs[i + 1][2]
    return start - (full - this if this < full - 1e-9 else 0), full


def quantised(beats):
    """Whether the notes sit exactly on a score's grid (a Guitar Pro file, a quantised MIDI file): 16ths,
    32nds and triplets all lie on 24ths of a beat. A transcription's or a live recording's do not."""
    return statistics.median(abs(b * 24 - round(b * 24)) for b in beats) < QUANTISED


def grid_options(beats, bpm):
    """Ticks per beat 2..12 at 10 ticks per second, best first: [{tpb, bpm, off, cost}] where off is the share
    of notes that would move more than FIT ticks off their rhythm. Notes that are not quantised cannot tell
    16ths from triplets one by one (OpenMusic's sit up to 25 ms off), and a transcription hardly has triplets,
    so for them only the grids that keep 16ths exact (4, 8 or 12 ticks) are candidates."""
    exact, options = quantised(beats), []
    for tpb in range(2, 13):
        tempo = 60 * TICKS_PER_SECOND / tpb
        off = sum(abs(b * tpb - round(b * tpb)) > FIT for b in beats) / max(1, len(beats))
        cost = abs(math.log(tempo / bpm)) + (OFF_RHYTHM * off if exact else 0 if tpb % 4 == 0 else math.inf)
        options.append({"tpb": tpb, "bpm": tempo, "off": off, "cost": cost})
    return sorted(options, key=lambda o: o["cost"]), exact


def jitter_ms(beats, bpm):
    """Median distance of the notes from the 16th grid, in ms at the song's tempo."""
    return statistics.median(abs(b * 4 - round(b * 4)) for b in beats) / 4 * 60000 / bpm


def fold(key):
    while key < LOW:
        key += 12
    while key > HIGH:
        key -= 12
    return key


def held(name, pitches):
    low = INSTRUMENTS[name][3]
    return sum(low <= p <= low + HIGH - LOW for p in pitches) / max(1, len(pitches))


def pick_instrument(part):
    """The program's instrument (Note Block Studio's choice), or one of CHOICES whose two octaves hold clearly
    more of the part's notes, so that fewer notes jump an octave."""
    pitches = [p for _, p, _ in part["notes"]]
    own = PROGRAMS[part["program"]]
    if INSTRUMENTS[own][3] is None:                     # a drum sound for a pitched program (timpani, ...)
        own = CHOICES[part["kind"]][0]
    best = max(CHOICES[part["kind"]], key=lambda name: held(name, pitches))
    return best if held(best, pitches) > held(own, pitches) + BETTER_FIT else own


def arrange(parts, offset, tpb, instruments):
    """Layer groups [(name, {(tick, instrument, key): velocity})] and per-part stats. A note's tick is its beat
    rounded on the grid minus `offset` (the start's tick), in whole numbers, so the first note can never come out
    at -1 through float rounding. Notes of one group that land on one tick with the same instrument and key
    become one, the loudest."""
    groups, stats = [], []
    for part in parts:
        name = layer_name(part, part["number"])
        slots, moved = defaultdict(dict), 0
        for beat, pitch, velocity in part["notes"]:
            tick = math.floor(beat * tpb + 0.5) - offset
            if part["kind"] == "drums":
                piece, sound, key = DRUMS.get(pitch, OTHER_DRUM)
                key = fold(key)
            else:
                piece, sound = "", instruments[part["number"]]
                key = fold(pitch - INSTRUMENTS[sound][3] + LOW)
                moved += key != pitch - INSTRUMENTS[sound][3] + LOW
            slot = slots[piece]
            slot[(tick, sound, key)] = max(slot.get((tick, sound, key), 0), velocity)
        order = sorted(slots, key=lambda p: min(DRUMS_ORDER.get(p, 99), 99))
        for piece in order:
            groups.append((f"{name}: {piece}" if piece else name, slots[piece]))
        stats.append({"part": part, "name": name, "notes": len(part["notes"]),
                      "written": sum(len(s) for s in slots.values()), "octave_moved": moved,
                      "pieces": [(p, sorted({(s, k) for (_, s, k) in slots[p]})) for p in order]})
    return groups, stats


DRUMS_ORDER = {}
for _note, (_piece, _, _) in sorted(DRUMS.items()):
    DRUMS_ORDER.setdefault(_piece, len(DRUMS_ORDER))
DRUMS_ORDER[OTHER_DRUM[0]] = len(DRUMS_ORDER)


PUNCTUATION = str.maketrans({"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
                             "…": "...", "·": "-", "、": ",", "。": "."})


def ascii_text(text):
    """Printable ASCII of a text: accents dropped (Beyoncé -> Beyonce), full-width forms and dashes made ASCII;
    '' when letters of another script would have to go (a half-kept Chinese title such as '(Live)' says less
    than a plain fallback)."""
    if any("\x80" <= c <= "\x9f" for c in text or ""):   # C1 controls: bytes of another code page read as Latin-1
        return ""
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text or "").translate(PUNCTUATION))
    text = "".join(c for c in text if not unicodedata.combining(c))
    if any(c.isalpha() and not c.isascii() for c in text):
        return ""
    return " ".join("".join(c for c in text if " " <= c <= "~").split())


def layer_name(part, number):
    return ascii_text(part["name"]) or f"{KIND_NAMES[part['kind']]} {number}"


def build(song, parts, tpb=None, instruments=None, velocities=False):
    """The note block song: {notes: [(tick, layer, instrument index, key, velocity 0-100)] sorted by tick and
    layer, layers: [names], tpb, options, bpm, start, length, time_signature, stats, instruments}. instruments:
    {part number: instrument} for pitched parts (the rest are picked); velocities: keep the score's instead of
    100 for every note."""
    notes = [n for p in parts for n in p["notes"]]
    if not notes:
        raise SystemExit("export_nbs.py: the chosen tracks have no notes")
    beats = [n[0] for n in notes]
    bpm = bpm_of(song["tempos"], min(beats), max(beats))
    options, exact = grid_options(beats, bpm)          # moving the start by whole beats changes none of this
    choice = next((o for o in options if o["tpb"] == tpb), None) if tpb is not None else options[0]
    if choice is None:
        raise SystemExit(f"export_nbs.py: --ticks-per-beat must be 2..12, not {tpb}")
    # the first note as it will be placed: a transcribed note 15 ms before the first downbeat is on that
    # downbeat, not at the end of a pickup bar (which would add a whole silent bar before the song)
    first = min(math.floor(b * choice["tpb"] + 0.5) for b in beats) / choice["tpb"]
    start, bar_beats = song_start(song["signatures"], first)
    offset = math.floor(start * choice["tpb"] + 0.5)   # the start on the grid (bars of 3.5 beats, odd grids)
    start = offset / choice["tpb"]
    chosen = {p["number"]: (instruments or {}).get(p["number"]) or pick_instrument(p)
              for p in parts if p["kind"] != "drums"}
    groups, stats = arrange(parts, offset, choice["tpb"], chosen)
    # Note Block Studio fills a structure's lowest level from the highest layers: sand (the Snare Drum) goes last
    groups.sort(key=lambda g: any(sound == "snare" for _, sound, _ in g[1]))
    out, layers = [], []
    for name, slot in groups:
        by_tick = defaultdict(list)
        for (tick, sound, key), velocity in slot.items():
            by_tick[tick].append((key, sound, velocity))
        depth = max(len(v) for v in by_tick.values())
        for tick, here in by_tick.items():
            for i, (key, sound, velocity) in enumerate(sorted(here, reverse=True)):   # high notes on top
                out.append((tick, len(layers) + i, INSTRUMENTS[sound][0], key,
                            max(1, round(velocity * 100 / 127)) if velocities else 100))
        layers += [name] + [f"{name} {i + 1}" for i in range(1, depth)]
    out.sort()
    if out[0][0] < 0:                                  # cannot happen (the start is at or before the first note)
        raise SystemExit(f"export_nbs.py: a note at tick {out[0][0]}; please report this file")
    if out[-1][0] > MAX_TICK or len(layers) > MAX_TICK:
        raise SystemExit(f"export_nbs.py: {out[-1][0]} ticks / {len(layers)} layers is more than Note Block Studio "
                         f"holds ({MAX_TICK}); try fewer --ticks-per-beat")
    signature = bar_beats * choice["tpb"] / 4          # Note Block Studio draws a bar line every 4 x this ticks
    return {"notes": out, "layers": layers, "tpb": choice["tpb"], "options": options, "bpm": bpm,
            "quantised": exact, "jitter_ms": jitter_ms(beats, bpm),
            "start": start, "length": out[-1][0] + 1, "stats": stats, "instruments": chosen,
            "time_signature": int(signature) if signature == int(signature) and 2 <= signature <= 8 else 4}


# ---------- writing ----------

def nbs_bytes(song, result):
    header = pynbs.Header(song_name=ascii_text(song["title"]) or FALLBACK_TITLE,
                          original_author=ascii_text(song["artist"]), song_author="OpenMusic",
                          description=description(result), tempo=float(TICKS_PER_SECOND),
                          time_signature=result["time_signature"], song_origin=ascii_text(song["source"]))
    notes = [pynbs.Note(tick=int(t), layer=int(l), instrument=int(i), key=int(k), velocity=int(v))
             for t, l, i, k, v in result["notes"]]
    layers = [pynbs.Layer(id=i, name=name) for i, name in enumerate(result["layers"])]
    f = pynbs.File(header, notes, layers, [])
    f.update_header(5)                     # pynbs 1.1.0: song length = the last note's tick (they are sorted)
    buf = io.BytesIO()
    pynbs.Writer(buf).encode_file(f, 5)
    return buf.getvalue()


def write(data, path):
    """Write in one go, so that a failure never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def sound_names(stat, instruments):
    if stat["part"]["kind"] != "drums":
        return INSTRUMENTS[instruments[stat["part"]["number"]]][1]
    names = []
    for _, sounds in stat["pieces"]:
        for sound, _ in sounds:
            if INSTRUMENTS[sound][1] not in names:
                names.append(INSTRUMENTS[sound][1])
    return " / ".join(names)


def description(result):
    tracks = "; ".join(f"{s['name']}: {sound_names(s, result['instruments'])}" for s in result["stats"])
    return (f"Made with OpenMusic. {tracks}. {result['tpb']} ticks per beat at 10 ticks per second = "
            f"{60 * TICKS_PER_SECOND / result['tpb']:g} BPM (originally {result['bpm']:.0f} BPM).")


def report(song, result, path):
    chosen = next(o for o in result["options"] if o["tpb"] == result["tpb"])
    print(f"{song['artist'] + ' - ' if song['artist'] else ''}{song['title']}")
    timing = (f"{chosen['off']:.1%} of notes off their rhythm" if result["quantised"] else
              f"the notes are not quantised (median {result['jitter_ms']:.0f} ms from the 16th grid), so on 16ths")
    print(f"  tempo {result['bpm']:.0f} BPM -> {result['tpb']} ticks per beat = {chosen['bpm']:g} BPM "
          f"({chosen['bpm'] / result['bpm'] - 1:+.0%}); {timing}")
    others = [o for o in result["options"] if o["tpb"] != result["tpb"] and math.isfinite(o["cost"])][:3]
    print("  other choices (--ticks-per-beat): " + "; ".join(
        f"{o['tpb']} = {o['bpm']:g} BPM ({o['bpm'] / result['bpm'] - 1:+.0%}"
        + (f", {o['off']:.0%} off)" if result["quantised"] else ")") for o in others))
    for s in result["stats"]:
        p = s["part"]
        if p["kind"] == "drums":
            what = ", ".join(f"{piece} = " + "/".join(f"{INSTRUMENTS[n][1]} {k}" for n, k in sounds)
                             for piece, sounds in s["pieces"])
        else:
            name = result["instruments"][p["number"]]
            what = (f"{INSTRUMENTS[name][1]} ({INSTRUMENTS[name][2]} under the note block), "
                    f"{s['octave_moved']} notes moved by octaves into its range")
        print(f"  {s['name']} [{p['kind']}]: {s['notes']} notes -> {s['written']} note blocks; {what}")
    seconds = result["length"] / TICKS_PER_SECOND
    poly = max(Counter(n[0] for n in result["notes"]).values())
    print(f"  {len(result['notes'])} note blocks, {result['length']} ticks ({int(seconds // 60)}:{seconds % 60:04.1f} "
          f"at 10 ticks/s), {len(result['layers'])} layers, at most {poly} at once")
    print(f"  wrote {path}")


def track_list(song, parts):
    """The tracks as data (for --list --json and the workbench)."""
    return {"title": song["title"], "artist": song["artist"],
            "tracks": [{"number": p["number"], "name": p["name"], "kind": p["kind"], "program": p["program"],
                        "notes": len(p["notes"]), "low": min(n[1] for n in p["notes"]),
                        "high": max(n[1] for n in p["notes"]), "muted": p["muted"],
                        "default": p["kind"] in DEFAULT_KINDS} for p in parts]}


def list_tracks(song, parts):
    for t in track_list(song, parts)["tracks"]:
        print(f"{'*' if t['default'] else ' '} {t['number']}. {t['name'] or '(no name)'} [{t['kind']}, program "
              f"{t['program']}] {t['notes']} notes, pitches {t['low']}-{t['high']}" + (", muted" if t["muted"] else ""))
    print("* = converted by default (--tracks picks others)")


def summary(song, result):
    """The result as data (for --json and the workbench)."""
    chosen = next(o for o in result["options"] if o["tpb"] == result["tpb"])
    return {"title": song["title"], "artist": song["artist"], "bpm": round(result["bpm"], 2),
            "ticks_per_beat": result["tpb"], "minecraft_bpm": chosen["bpm"], "off_rhythm": round(chosen["off"], 4),
            "quantised": result["quantised"], "jitter_ms": round(result["jitter_ms"], 1),
            "options": [{"ticks_per_beat": o["tpb"], "bpm": o["bpm"], "off_rhythm": round(o["off"], 4)}
                        for o in result["options"]],
            "note_blocks": len(result["notes"]), "ticks": result["length"],
            "seconds": round(result["length"] / TICKS_PER_SECOND, 1), "layers": len(result["layers"]),
            "most_at_once": max(Counter(n[0] for n in result["notes"]).values()),
            "tracks": [{"name": s["name"], "kind": s["part"]["kind"], "notes": s["notes"], "note_blocks": s["written"],
                        "instruments": sound_names(s, result["instruments"]).split(" / "),
                        "octave_moved": s["octave_moved"]} for s in result["stats"]]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("input", help="a .gp or MIDI file, or an OpenMusic song folder")
    ap.add_argument("--tracks", help="comma-separated: bass, drums, guitar, piano, melody, all, numbers or names")
    ap.add_argument("--instrument", action="append", default=[], metavar="TRACK=NAME",
                    help="a track's (number or name) note block instrument: "
                         + ", ".join(n for n, v in INSTRUMENTS.items() if v[3] is not None))
    ap.add_argument("--ticks-per-beat", type=int, help="2..12 redstone ticks per beat (default: chosen)")
    ap.add_argument("--velocities", action="store_true",
                    help="keep the score's velocities (a structure plays every note alike, so by default all are 100)")
    ap.add_argument("--out", help="where to write the .nbs")
    ap.add_argument("--list", action="store_true", help="list the tracks and write nothing")
    ap.add_argument("--json", action="store_true", help="print the track list or the result as one line of JSON")
    args = ap.parse_args()

    song, parts = load(args.input)
    if not parts:
        raise SystemExit(f"export_nbs.py: no notes in {args.input}")
    if args.list:                          # JSON in ASCII: a name with U+2028 must not split the line
        if args.json:
            print(json.dumps(track_list(song, parts)))
        else:
            list_tracks(song, parts)
        return
    chosen = select(parts, args.tracks)
    result = build(song, chosen, args.ticks_per_beat, instrument_choices(args.instrument, parts, chosen),
                   args.velocities)
    src = Path(args.input)
    out = Path(args.out) if args.out else (src / "minecraft" / f"{src.name}.nbs" if src.is_dir()
                                           else src.with_suffix(".nbs"))
    write(nbs_bytes(song, result), out)
    if args.json:
        print(json.dumps(summary(song, result)))
    else:
        report(song, result, out)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:                  # one line on stderr, which the workbench shows
        raise SystemExit(f"export_nbs.py: {type(e).__name__}: {e}")
