"""Write a song's transcription as one Guitar Pro 5 file, so it opens as a score with tablature instead of
having to import and tidy up each MIDI file by hand.

    python experiments/export_gp.py outputs/mp3
    python experiments/export_gp.py outputs/mp3 --song "Carsick Cars - 中南海" --force

For each <stems_root>/<song>/ folder with transcription/*.notes.json (from transcribe.py) this writes
<song>/transcription/<song>.gp5 with one track per stem: guitar (6 strings, standard tuning, tab + notation),
bass (4 strings, tab + notation), vocals as a melody (notation only), piano and other (notation only) when
they have at least MIN_TRACK_NOTES notes, and drums when transcription/drums.mid exists (any General MIDI drum
file). Outputs newer than all their inputs are kept unless --force.

Bars and beats: the same tempo map as the MIDI files (transcribe.tempo_map on analysis.json), so bar N of the
.gp5 is bar N of the MIDI and of the practice player (bar 1 may be a short pickup bar). Every analysis beat is
a quarter note; each bar's time signature is its number of beats over 4. Guitar Pro keeps one whole-number tempo
at a time, so the written tempo follows the song's per-bar tempo and only changes when it moves by more than
TEMPO_STEP; inside Guitar Pro the playback is therefore close to, not locked to, the recording.
Rhythm: each beat goes on a grid of 16ths, or of 8th-note triplets when that fits its onsets clearly better
(TRIPLET_GAIN, TRIPLET_NEAR). Each track is written as one voice: notes that start together form a chord that
lasts until the next onset (or its own end); gaps shorter than LEGATO_BEATS (a whole beat for the vocal melody) are
closed, so there are fewer tiny rests. Durations are split at bar lines and chord changes and joined with ties.
Clean-up on top of transcribe.py: notes softer than GHOST_VELOCITY x the track's loud level are dropped, and in
the polyphonic stems so are overtone ghosts (a softer note an octave, a twelfth, two octaves, ... above a note
sounding at the same time). Notes the instrument cannot play are dropped too; a guitar or bass part with
enough low D / D# notes gets drop-D tuning instead.
Frets: dynamic programming over the chords of a track, with the fretting hand at a position p covering frets
p..p+MAX_SPAN: each chord is played on distinct strings with all fretted notes inside the window, and the path
minimises hand movement (cheaper after longer notes), then a slight preference for low positions and for
chord shapes whose strings do not cross. A chord with no playable fingering loses its softest notes first.
Chord names from analysis.json sit above every pitched track (Guitar Pro chord symbols, no diagrams); title,
artist, key and tempo go in the file's score information.

Guitar Pro 5 files have no fixed text encoding: Guitar Pro reads names with the computer's own code page.
The default here is this computer's locale encoding (UTF-8 on macOS, GBK on Chinese Windows); use
--encoding if the Chinese title shows up garbled. Track and chord names are plain ASCII either way.

Writes with PyGuitarPro (Sviatoslav Abakumov, LGPL-3.0, https://github.com/Perlence/PyGuitarPro), and reads
the file back with it to check the bar count, tempo and note counts.
"""
import argparse
import json
import locale
import time
from importlib.metadata import version
from itertools import combinations
from pathlib import Path

import guitarpro as gp
import mido
import numpy as np

from transcribe import SETTINGS, read_analysis, seconds_to_beats, tempo_map

TICKS = gp.Duration.quarterTime    # 960 ticks per beat; every analysis beat is written as a quarter note
STRAIGHT, TRIPLET = 4, 3           # grid steps per beat: 16ths, or 8th-note triplets
TRIPLET_GAIN = 0.5                 # a beat takes the triplet grid when that at least halves its onsets' summed
TRIPLET_NEAR = 0.06                # distance to the grid, and one onset is within 0.06 beat of 1/3 or 2/3
LEGATO_BEATS = 0.5                 # a note ending less than this before the next onset is held until it
MELODY_LEGATO_BEATS = 1.0          # the same for the vocal melody: sung syllables leave many short gaps
DRUM_BEATS = 1.0                   # a drum hit is written at most this long
GHOST_VELOCITY = 0.55              # notes softer than this x the track's 95th-percentile velocity are dropped
OVERTONES = (12, 19, 24, 28, 31)   # harmonics 2-6, in semitones above the note
OVERTONE_RATIO = 0.8               # an overtone ghost is at most this loud relative to the note under it
MIN_TRACK_NOTES = 64               # piano and other are only written with this many notes left
TEMPO_STEP = 0.03                  # the written tempo changes when the bar tempo moves more than 3 %
TEMPO_SMOOTH_BARS = 5              # bar tempos are smoothed with a running median over this many bars
DROP_D_SHARE = 0.01                # drop-D tuning when this share (and >= DROP_D_MIN) of notes need it
DROP_D_MIN = 8
W_MOVE = 1.0                       # cost per fret the hand moves, divided by (1 + beats since the last onset)
W_POSITION = 0.03                  # cost per fret of hand position per chord (a tie-break towards low positions)
W_CROSS = 1.0                      # cost per pair of chord notes whose strings are in the opposite order
MAX_FINGERINGS = 256               # enough for any chord within a hand span; caps big notation-only chords

GUITAR = (64, 59, 55, 50, 45, 40)            # string 1 (high E4) to string 6 (low E2)
BASS = (43, 38, 33, 28)                      # G2 D2 A1 E1
WIDE = (77, 70, 63, 56, 49, 42, 35)          # notation-only tracks: 7 strings a fifth apart, B1 up to F7
DRUM_STRINGS = (0,) * 6
# stem: track name, strings, frets the fingering may use, fretting-hand span (None: notation only, no tab),
# drop-D allowed, only written with MIN_TRACK_NOTES notes; the MIDI sound comes from transcribe.SETTINGS
TRACKS = {
    "guitar": dict(name="Guitar", strings=GUITAR, frets=22, span=4, drop_d=True, optional=False),
    "bass": dict(name="Bass", strings=BASS, frets=20, span=3, drop_d=True, optional=False),
    "vocals": dict(name="Vocals (melody)", strings=GUITAR, frets=24, span=None, drop_d=False, optional=False),
    "piano": dict(name="Piano", strings=WIDE, frets=24, span=None, drop_d=False, optional=True),
    "other": dict(name="Other", strings=WIDE, frets=24, span=None, drop_d=False, optional=True),
}
ORDER = ("guitar", "bass", "vocals", "piano", "other", "drums")   # Guitar Pro opens on the first track
POLYPHONIC = ("guitar", "piano", "other")
COLORS = {"guitar": gp.Color(232, 120, 40), "bass": gp.Color(60, 120, 220), "vocals": gp.Color(200, 60, 120),
          "piano": gp.Color(90, 160, 90), "other": gp.Color(130, 110, 180), "drums": gp.Color(120, 120, 120)}

CHORD_TYPES = {"": gp.ChordType.major, "m": gp.ChordType.minor, "7": gp.ChordType.seventh,
               "maj7": gp.ChordType.majorSeventh, "m7": gp.ChordType.minorSeventh,
               "sus4": gp.ChordType.suspendedFourth, "sus2": gp.ChordType.suspendedSecond,
               "dim": gp.ChordType.diminished, "5": gp.ChordType.power}
FIFTHS = {"major": dict(C=0, G=1, D=2, A=3, E=4, B=5, **{"F#": 6, "C#": 7}, F=-1, Bb=-2, Eb=-3, Ab=-4, Db=-5,
                        Gb=-6, Cb=-7),
          "minor": dict(A=0, E=1, B=2, **{"F#": 3, "C#": 4, "G#": 5, "D#": 6, "A#": 7}, D=-1, G=-2, C=-3, F=-4,
                        Bb=-5, Eb=-6, Ab=-7)}


# ---------- notes ----------

def drop_ghosts(notes, polyphonic):
    """Notes [(start_s, end_s, pitch, velocity)] without the soft ones and, if polyphonic, the overtone
    ghosts; and how many of each were dropped."""
    if not notes:
        return [], 0, 0
    floor = GHOST_VELOCITY * np.percentile([n[3] for n in notes], 95)
    loud = [n for n in notes if n[3] >= floor]
    if not polyphonic:
        return loud, len(notes) - len(loud), 0
    loud.sort()
    starts = np.array([n[0] for n in loud])
    longest = max(n[1] - n[0] for n in loud)
    kept = []
    for a, b, p, v in loud:
        lo, hi = np.searchsorted(starts, a - longest), np.searchsorted(starts, b)
        ghost = any(q[2] in [p - k for k in OVERTONES] and v <= OVERTONE_RATIO * q[3]
                    and min(b, q[1]) - max(a, q[0]) >= 0.5 * (b - a) for q in loud[lo:hi])
        if not ghost:
            kept.append((a, b, p, v))
    return kept, len(notes) - len(loud), len(loud) - len(kept)


def tuning(notes, spec):
    """The strings for a track: standard, or drop D when enough notes sit just below the low string."""
    strings = spec["strings"]
    low = strings[-1]
    if spec["drop_d"] and notes:
        n_low = sum(low - 2 <= n[2] < low for n in notes)
        if n_low >= max(DROP_D_MIN, DROP_D_SHARE * len(notes)):
            return strings[:-1] + (low - 2,), True
    return strings, False


def drum_hits(path):
    """Hits [(t_s, t_s, key, velocity)] of a General MIDI drum file, timed by its own tempo map."""
    hits, t = [], 0.0
    for msg in mido.MidiFile(str(path)):
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            hits.append((t, t, msg.note, msg.velocity))
    return hits


# ---------- rhythm ----------

def beat_grids(onsets, n_beats):
    """Grid steps per beat (STRAIGHT or TRIPLET) for beats 0..n_beats, from onsets in beats. A beat needs
    onsets at two or more places inside it to be a triplet: one stray late 8th is not a triplet figure."""
    grid = np.full(n_beats + 2, STRAIGHT)
    k = np.floor(onsets).astype(int)
    f = onsets - k
    for beat in np.unique(k):
        x = np.sort(f[k == beat])
        inner = x[(x > 0.08) & (x < 0.92)]
        if len(inner) < 2 or inner[-1] - inner[0] < 0.15 or not 0 <= beat < len(grid):
            continue
        straight = np.abs(x * 4 - np.round(x * 4)).sum() / 4
        triplet = np.abs(x * 3 - np.round(x * 3)).sum() / 3
        near = np.min(np.abs(inner[:, None] - [1 / 3, 2 / 3])) < TRIPLET_NEAR
        if near and triplet < TRIPLET_GAIN * straight:
            grid[beat] = TRIPLET
    return grid


def snap(beats, grid):
    """Positions in beats -> ticks at the nearest step of the grid of the beat they fall in."""
    beats = np.maximum(np.asarray(beats, float), 0)
    k = np.minimum(np.floor(beats + 1e-9).astype(int), len(grid) - 1)
    steps = grid[k]
    return k * TICKS + np.round((beats - k) * steps).astype(int) * (TICKS // steps)


def snap_ticks_down(ticks, grid):
    """A tick moved back to the step of its beat's grid at or before it."""
    k = min(ticks // TICKS, len(grid) - 1)
    unit = TICKS // grid[k]
    return k * TICKS + min((ticks - k * TICKS) // unit * unit, TICKS)


def unit_at(tick, grid):
    return TICKS // grid[min(tick // TICKS, len(grid) - 1)]


def chord_groups(notes, knots, grid, total, drums=False, legato=LEGATO_BEATS):
    """Notes -> [[start tick, end tick, {pitch: velocity}]]: notes on the same snapped onset form one chord,
    which lasts until the next onset (drums: at most DRUM_BEATS), gaps under legato beats closed."""
    if not notes:
        return []
    starts, ends, pitches, velocities = (np.array(c) for c in zip(*notes))
    on = snap(seconds_to_beats(starts, knots), grid)
    off = snap(seconds_to_beats(ends, knots), grid)
    groups = {}
    for a, b, p, v in zip(on.tolist(), off.tolist(), pitches.tolist(), velocities.tolist()):
        if a >= total:
            continue
        g = groups.setdefault(a, [a, a, {}])
        g[1] = max(g[1], b)
        g[2][p] = max(g[2].get(p, 0), v)
    out = sorted(groups.values())
    for i, g in enumerate(out):
        nxt = out[i + 1][0] if i + 1 < len(out) else total
        end = min(nxt, g[0] + round(DRUM_BEATS * TICKS)) if drums else min(g[1], nxt)
        if nxt - end < legato * TICKS:
            end = nxt
        end = snap_ticks_down(end, grid)
        g[1] = max(end, min(g[0] + unit_at(g[0], grid), nxt))
    return out


STRAIGHT_DURATIONS = {16: (1, False), 12: (2, True), 8: (2, False), 6: (4, True), 4: (4, False), 3: (8, True),
                      2: (8, False), 1: (16, False)}   # length in 16ths: (note value, dotted)


def straight_pieces(a, b, bar_start):
    """[(tick, Duration)] filling a..b (16th steps) in a bar: the longest value that fits, where values of a
    beat or more start on an 8th (a half note or longer on a beat) and shorter ones stay inside their beat."""
    unit = TICKS // 4
    p, end, out = (a - bar_start) // unit, (b - bar_start) // unit, []
    while p < end:
        for d in STRAIGHT_DURATIONS:
            if p + d <= end and (d < 8 or p % 4 == 0) and (d < 4 or p % 2 == 0) and \
                    (d >= 4 or p // 4 == (p + d - 1) // 4):
                break
        value, dotted = STRAIGHT_DURATIONS[d]
        out.append((bar_start + p * unit, gp.Duration(value=value, isDotted=dotted)))
        p += d
    return out


def triplet_pieces(a, b):
    """[(tick, Duration)] filling a..b inside one triplet beat: triplet 8ths and quarters, or a plain quarter."""
    unit, out = TICKS // 3, []
    while a < b:
        n = min((b - a) // unit, 2 if (a % TICKS) else 3)
        out.append((a, gp.Duration(value=4) if n == 3 else
                    gp.Duration(value=4 if n == 2 else 8, tuplet=gp.Tuplet(3, 2))))
        a += n * unit
    return out


# ---------- frets ----------

def fingerings(pitches, strings, frets, span):
    """Ways to play the pitches on distinct strings, as [(string number, fret)] in the pitches' order, with the
    fretted notes within span frets of each other (any span if None); at most MAX_FINGERINGS of them."""
    options = [[(s, p - open_) for s, open_ in enumerate(strings, 1) if 0 <= p - open_ <= frets] for p in pitches]
    found = []

    def extend(i, chosen, lo, hi):
        if i == len(pitches):
            found.append(tuple(chosen))
            return
        for s, f in options[i]:
            if len(found) >= MAX_FINGERINGS:
                return
            if any(s == c[0] for c in chosen):
                continue
            nlo, nhi = (min(lo, f), max(hi, f)) if f else (lo, hi)
            if span is None or nhi - nlo <= span:
                extend(i + 1, chosen + [(s, f)], nlo, nhi)

    extend(0, [], 99, -1)
    return found


def playable(group, strings, frets, span):
    """The group's pitches and their fingerings; when they cannot all be played at once, the largest subset
    that can, and of those the loudest."""
    pitches = sorted(group)
    for size in range(min(len(pitches), len(strings)), 0, -1):
        for subset in sorted(combinations(pitches, size), key=lambda s: -sum(group[p] for p in s)):
            found = fingerings(list(subset), strings, frets, span)
            if found:
                return list(subset), found
    return [], []


def crossings(fingering):
    """Pairs of notes (in rising pitch order) whose strings go the other way (higher pitch, lower string)."""
    s = [f[0] for f in fingering]
    return sum(s[i] < s[j] for i in range(len(s)) for j in range(i + 1, len(s)))


def assign_frets(groups, strings, frets, span):
    """Choose a fingering per chord group: [(pitches, [(string, fret)])], and the number of notes dropped.

    Dynamic programming over the hand position p (index finger at fret p, covering p..p+span): a fingering
    fits p when its fretted notes lie in that window. Cost = W_MOVE * |p - p'| / (1 + beats between the
    onsets) + W_POSITION * p + W_CROSS * string crossings. Notation-only tracks (span None) skip the hand."""
    positions = np.arange(frets + 1) if span is not None else np.zeros(1, int)
    n_pos = len(positions)
    total = np.zeros(n_pos)
    back, choice, chosen_pitches, dropped = [], [], [], 0
    prev_start = None
    for start, _, group in groups:
        pitches, found = playable(group, strings, frets, span)
        dropped += len(group) - len(pitches)
        chosen_pitches.append(pitches)
        cost = np.full(n_pos, np.inf)
        pick = np.zeros(n_pos, int)
        for i, fing in enumerate(found):
            fretted = [f for _, f in fing if f]
            base = W_CROSS * crossings(fing)
            if span is None:
                lo_p, hi_p = 0, 0
            elif fretted:
                lo_p, hi_p = max(0, max(fretted) - span), min(fretted)
            else:
                lo_p, hi_p = 0, frets
            c = base + W_POSITION * positions[lo_p:hi_p + 1]
            better = c < cost[lo_p:hi_p + 1]
            cost[lo_p:hi_p + 1][better] = c[better]
            pick[lo_p:hi_p + 1][better] = i
        if not found:  # nothing playable: the hand stays where it is
            cost, pick = np.zeros(n_pos), np.full(n_pos, -1)
        gap = (start - prev_start) / TICKS if prev_start is not None else 0.0
        move = W_MOVE * np.abs(positions[:, None] - positions[None, :]) / (1 + gap)
        step = total[None, :] + move
        back.append(np.argmin(step, axis=1))
        total = cost + step.min(axis=1)
        choice.append((found, pick))
        prev_start = start
    out = [None] * len(groups)
    p = int(np.argmin(total)) if groups else 0
    for g in range(len(groups) - 1, -1, -1):
        found, pick = choice[g]
        out[g] = (chosen_pitches[g], list(found[pick[p]]) if pick[p] >= 0 else [])
        p = int(back[g][p])
    return out, dropped


def fret_span(played):
    """Frets between the lowest and highest fretted note of one (pitches, fingering); open strings are free."""
    fretted = [f for _, f in played[1] if f]
    return max(fretted) - min(fretted) if fretted else 0


def drum_fingering(group):
    keys = sorted(group, key=lambda k: -group[k])[:len(DRUM_STRINGS)]
    return sorted(keys), [(s, k) for s, k in enumerate(sorted(keys), 1)]


# ---------- song layout ----------

def bar_list(signatures, total_beats):
    """[(start beat, beats)] for every bar, from transcribe.tempo_map's signatures, until total_beats."""
    bars = []
    for i, (beat, per_bar) in enumerate(signatures):
        stop = signatures[i + 1][0] if i + 1 < len(signatures) else None
        while (beat < stop) if stop is not None else (beat < total_beats or not bars):
            n = per_bar if stop is None else min(per_bar, stop - beat)
            bars.append((beat, n))
            beat += n
    return bars


def beats_to_seconds(beats, knots):
    ts, bs = np.array(knots).T
    beats = np.asarray(beats, float)
    slope = (ts[-1] - ts[-2]) / (bs[-1] - bs[-2])
    return np.where(beats <= bs[-1], np.interp(beats, bs, ts), ts[-1] + (beats - bs[-1]) * slope)


def bar_tempos(bars, knots):
    """Whole-number tempo per bar where the written tempo changes: {bar index: BPM}, bar 0 always."""
    starts = np.array([b for b, _ in bars], float)
    lengths = np.array([n for _, n in bars], float)
    seconds = beats_to_seconds(starts + lengths, knots) - beats_to_seconds(starts, knots)
    bpm = 60 * lengths / np.maximum(seconds, 1e-6)
    half = TEMPO_SMOOTH_BARS // 2
    smooth = [float(np.median(bpm[max(0, i - half):i + half + 1])) for i in range(len(bpm))]
    changes = {0: int(round(smooth[0]))}
    current = changes[0]
    for i, t in enumerate(smooth[1:], 1):
        if abs(t - current) > TEMPO_STEP * current:
            current = changes[i] = int(round(t))
    return changes


def key_signature(key):
    """analysis.json key -> gp.KeySignature (C major if unknown)."""
    if not key or not key.get("label_en"):
        return gp.KeySignature.CMajor
    tonic, _, mode = key["label_en"].partition(" ")
    fifths = FIFTHS.get(mode, {}).get(tonic)
    if fifths is None:
        return gp.KeySignature.CMajor
    return gp.KeySignature((fifths, int(mode == "minor")))


def chord_symbol(label, n_strings):
    """A Guitar Pro chord symbol (name, root, type, bass; no diagram) for an analysis.json chord label."""
    name, _, bass = label.partition("/")
    root = name[:2] if name[1:2] in ("#", "b") else name[:1]
    if root[:1] not in "ABCDEFG" or not root:  # "N.C."
        return gp.Chord(n_strings, sharp=True, add=False, name=label[:22], firstFret=1, show=True, newFormat=True)
    chord_type = CHORD_TYPES.get(name[len(root):], gp.ChordType.major)
    return gp.Chord(n_strings, sharp="b" not in root, root=gp.PitchClass(root), type=chord_type,
                    extension=gp.ChordExtension.none, bass=gp.PitchClass(bass or root),
                    tonality=gp.ChordAlteration.perfect, add=False, name=label[:22],
                    fifth=gp.ChordAlteration.perfect, ninth=gp.ChordAlteration.perfect,
                    eleventh=gp.ChordAlteration.perfect, firstFret=1, show=True, newFormat=True)


def chord_ticks(analysis, knots, total):
    """{tick: label} at each chord change (on its nearest beat), "N.C." where a bar or more has no chord."""
    out, last = {}, None
    for seg in ((analysis or {}).get("chords") or {}).get("segments") or []:
        a, b = seconds_to_beats([seg["start"], seg["end"]], knots)
        tick = int(round(a)) * TICKS
        label = seg["label"]
        if label == "N":
            if last is None or b - a < 4:
                continue
            label = "N.C."
        if 0 <= tick < total and label != last:
            out[tick] = label
            last = label
    return out


# ---------- writing ----------

def make_track(song, number, stem, strings, settings):
    channel = 9 if stem == "drums" else [0, 2, 4, 6, 10, 12, 14][number - 1]
    program = 0 if stem == "drums" else SETTINGS[stem]["program"]
    track = gp.Track(song, number=number, name=settings["name"], fretCount=24, isPercussionTrack=stem == "drums",
                     strings=[gp.GuitarString(i, v) for i, v in enumerate(strings, 1)],
                     channel=gp.MidiChannel(channel=channel, effectChannel=channel if stem == "drums" else channel + 1,
                                            instrument=program),
                     color=COLORS[stem],
                     settings=gp.TrackSettings(tablature=settings["span"] is not None, notation=True,
                                               diagramList=False, diagramsInScore=False))
    track.measures = [gp.Measure(track, header) for header in song.measureHeaders]
    return track


def fill_track(track, groups, played, grid, chords, tempos, bars):
    """Write the track's chord groups into its bars as one voice: notes, ties, rests, chord symbols, and on
    the first track the tempo changes."""
    total = (bars[-1][0] + bars[-1][1]) * TICKS
    segments, t = [], 0
    for (start, end, _), notes in zip(groups, played):
        if not notes[1]:
            continue
        if start > t:
            segments.append((t, start, None))
        segments.append((start, end, notes))
        t = end
    if t < total:
        segments.append((t, total, None))
    cuts = {b * TICKS for b, _ in bars} | set(chords)
    triplet_beats = np.flatnonzero(grid == TRIPLET)
    cuts |= {k * TICKS for k in triplet_beats} | {(k + 1) * TICKS for k in triplet_beats}
    cuts = sorted(c for c in cuts if 0 < c < total)
    bar_starts = [b * TICKS for b, _ in bars]
    tempo_ticks = {bars[i][0] * TICKS: bpm for i, bpm in tempos.items()} if tempos else {}
    written = 0
    for a, b, notes in segments:
        edges = [a] + [c for c in cuts[np.searchsorted(cuts, a, side="right"):np.searchsorted(cuts, b)]] + [b]
        first = True
        for x, y in zip(edges, edges[1:]):
            bar = int(np.searchsorted(bar_starts, x, side="right")) - 1
            if grid[min(x // TICKS, len(grid) - 1)] == TRIPLET:
                pieces = triplet_pieces(x, y)
            else:
                pieces = straight_pieces(x, y, bar_starts[bar])
            voice = track.measures[bar].voices[0]
            for tick, duration in pieces:
                beat = gp.Beat(voice, duration=duration,
                               status=gp.BeatStatus.normal if notes else gp.BeatStatus.rest)
                if notes:
                    for string, fret in notes[1]:
                        beat.notes.append(gp.Note(beat, value=fret, string=string, velocity=gp.Velocities.default,
                                                  type=gp.NoteType.normal if first else gp.NoteType.tie))
                    written += len(notes[1]) if first else 0
                    first = False
                if tick in chords:
                    beat.effect.chord = chord_symbol(chords[tick], len(track.strings))
                if tick in tempo_ticks:
                    beat.effect.mixTableChange = gp.MixTableChange(
                        tempo=gp.MixTableItem(value=tempo_ticks[tick], duration=0), hideTempo=False)
                voice.beats.append(beat)
    return written


def split_name(name):
    artist, sep, title = name.partition(" - ")
    return (title.strip(), artist.strip()) if sep else (name, "")


def safe_text(text, encoding):
    return text.encode(encoding, errors="replace").decode(encoding)


def read_notes(song_dir):
    """{stem: (notes, method text)} for the transcribed stems; drums from transcription/drums.mid."""
    out, tdir = {}, song_dir / "transcription"
    for stem in TRACKS:
        path = tdir / f"{stem}.notes.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            out[stem] = ([tuple(n) for n in data.get("notes") or []], data.get("method", ""))
    if (tdir / "drums.mid").is_file():
        out["drums"] = (drum_hits(tdir / "drums.mid"), "transcription/drums.mid")
    return out


def song_bars(analysis, stems):
    """(knots, tempo note, bars): the MIDI tempo map, and bars covering the song and every note in stems."""
    knots, signatures, tempo_note = tempo_map(analysis)
    duration = float((analysis or {}).get("duration_s") or 0)
    last_note = max((n[1] for notes, _ in stems.values() for n in notes), default=0.0)
    return knots, tempo_note, bar_list(signatures, float(seconds_to_beats(max(duration, last_note), knots)))


def build_song(song_dir, analysis, stems, encoding):
    """(gp.Song, report): every track laid out on the analysis beat grid. report has per-track counts."""
    knots, tempo_note, bars = song_bars(analysis, stems)
    total = (bars[-1][0] + bars[-1][1]) * TICKS
    tempos = bar_tempos(bars, knots)
    key = key_signature((analysis or {}).get("key"))
    chords = chord_ticks(analysis, knots, total)

    title, artist = split_name(song_dir.name)
    tempo_info = (analysis or {}).get("tempo") or {}
    key_text = ((analysis or {}).get("key") or {}).get("label_en")
    subtitle = " · ".join(filter(None, [key_text and f"Key: {key_text}",
                                        tempo_info.get("bpm") and f"{tempo_info['bpm']:g} BPM"]))
    song = gp.Song(title=safe_text(title, encoding), artist=safe_text(artist, encoding),
                   subtitle=safe_text(subtitle, encoding),
                   tab="OpenMusic (automatic draft)",
                   instructions="Automatic transcription: a draft to check by ear."
                                + (" Bar 1 is a pickup (bar 0 in the OpenMusic player)."
                                   if len(bars) > 1 and bars[0][1] < bars[1][1] else ""),
                   tempo=tempos[0], tempoName="", hideTempo=False, key=key)
    song.measureHeaders, song.tracks = [], []
    start = TICKS
    for number, (_, n) in enumerate(bars, 1):
        song.addMeasureHeader(gp.MeasureHeader(number=number, start=start, keySignature=key,
                                               timeSignature=gp.TimeSignature(numerator=n,
                                                                              denominator=gp.Duration(4))))
        start += n * TICKS

    report = {"bars": len(bars), "tempo": tempos[0], "tempo_changes": len(tempos) - 1, "tracks": {},
              "key": key_text, "chords": len(chords)}
    notice = [f"Bars and beats: {tempo_note}.",
              "Rhythm on a grid of 16ths and 8th-note triplets; ghost and overtone notes dropped."]
    for stem in [s for s in ORDER if s in stems]:
        notes, method = stems[stem]
        drums = stem == "drums"
        settings = TRACKS.get(stem, dict(name="Drums", strings=DRUM_STRINGS, span=None, optional=False))
        n_in = len(notes)
        quiet = overtones = 0
        if not drums:
            notes, quiet, overtones = drop_ghosts(notes, stem in POLYPHONIC)
        strings, drop_d = (DRUM_STRINGS, False) if drums else tuning(notes, settings)
        if not drums:
            lo, hi = strings[-1], strings[0] + settings["frets"]
            notes = [n for n in notes if lo <= n[2] <= hi]
        out_of_range = n_in - quiet - overtones - len(notes)
        if not notes or (settings["optional"] and len(notes) < MIN_TRACK_NOTES):
            report["tracks"][stem] = {"skipped": f"{len(notes)} notes left of {n_in}"}
            continue
        onsets = seconds_to_beats([n[0] for n in notes], knots)
        grid = beat_grids(onsets, int(total // TICKS))
        groups = chord_groups(notes, knots, grid, total, drums=drums,
                              legato=MELODY_LEGATO_BEATS if stem == "vocals" else LEGATO_BEATS)
        if drums:
            played, unplayable = [drum_fingering(g[2]) for g in groups], 0
        else:
            played, unplayable = assign_frets(groups, strings, settings["frets"], settings["span"])
        track = make_track(song, len(song.tracks) + 1, stem, strings, settings)
        song.tracks.append(track)
        written = fill_track(track, groups, played, grid, chords if not drums else {},
                             tempos if len(song.tracks) == 1 else None, bars)
        report["tracks"][stem] = {
            "name": settings["name"], "notes_in": n_in, "quiet": quiet, "overtones": overtones,
            "out_of_range": out_of_range, "unplayable": unplayable, "notes": written, "chords": len(groups),
            "triplet_beats": int(np.sum(grid == TRIPLET)), "drop_d": drop_d,
            "max_span": max(map(fret_span, played), default=0) if settings["span"] is not None else None,
            "tuning": strings}
        notice.append(f"{settings['name']}: {method}"[:250])
    if not song.tracks:
        return None, report
    song.notice = [safe_text(line, encoding) for line in notice]
    return song, report


def check(path, encoding, report):
    """Read the file back with PyGuitarPro: bar count, tempo, full bars, note counts as written."""
    song = gp.parse(str(path), encoding=encoding)
    problems = []
    if len(song.measureHeaders) != report["bars"]:
        problems.append(f"{len(song.measureHeaders)} bars, expected {report['bars']}")
    if song.tempo != report["tempo"]:
        problems.append(f"tempo {song.tempo}, expected {report['tempo']}")
    written = {r["name"]: r["notes"] for r in report["tracks"].values() if "notes" in r}
    for track in song.tracks:
        notes = 0
        for measure in track.measures:
            beats = measure.voices[0].beats
            if sum(b.duration.time for b in beats) != measure.length:
                problems.append(f"{track.name} bar {measure.number} is not full")
            notes += sum(n.type == gp.NoteType.normal for b in beats for n in b.notes)
        if notes != written.get(track.name):
            problems.append(f"{track.name}: {notes} notes read back, {written.get(track.name)} written")
    return problems


def up_to_date(out, inputs):
    return out.exists() and out.stat().st_mtime > max(p.stat().st_mtime for p in inputs if p.exists())


def export_song(song_dir, encoding, force=False):
    tdir = song_dir / "transcription"
    out = tdir / f"{song_dir.name}.gp5"
    inputs = list(tdir.glob("*.notes.json")) + [tdir / "drums.mid", song_dir / "analysis.json"]
    if not force and up_to_date(out, inputs):
        print(f"{song_dir.name}: {out.name} is up to date (use --force to redo)")
        return True
    start = time.perf_counter()
    stems = read_notes(song_dir)
    song, report = build_song(song_dir, read_analysis(song_dir), stems, encoding)
    if song is None:
        print(f"{song_dir.name}: nothing to write ({report['tracks']})")
        out.unlink(missing_ok=True)
        return False
    tmp = out.with_suffix(".gp5.tmp")
    gp.write(song, str(tmp), version=(5, 1, 0), encoding=encoding)
    problems = check(tmp, encoding, report)
    if problems:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"{song_dir.name}: the written file does not read back right: {'; '.join(problems)}")
    tmp.replace(out)
    changes = f" ({report['tempo_changes']} tempo changes)" if report["tempo_changes"] else ""
    print(f"{song_dir.name}: {out.name}, {report['bars']} bars, {report['tempo']} BPM{changes}, "
          f"key {report['key'] or '?'}, {report['chords']} chord symbols, {time.perf_counter() - start:.1f} s")
    for stem, r in report["tracks"].items():
        if "skipped" in r:
            print(f"  {stem}: skipped ({r['skipped']})")
            continue
        extra = ", drop D" if r["drop_d"] else ""
        extra += f", widest chord {r['max_span']} frets" if r["max_span"] is not None else ""
        print(f"  {stem}: {r['notes']} notes in {r['chords']} onsets, {r['triplet_beats']} triplet beats; dropped "
              f"{r['quiet']} soft, {r['overtones']} overtone, {r['out_of_range']} out of range, "
              f"{r['unplayable']} unplayable{extra}")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("stems_root", type=Path)
    parser.add_argument("--song", help="only this song folder")
    parser.add_argument("--force", action="store_true", help="redo songs whose .gp5 is up to date")
    default_encoding = getattr(locale, "getencoding", lambda: locale.getpreferredencoding(False))()
    parser.add_argument("--encoding", default=default_encoding,
                        help="text encoding of the title in the file (default: this computer's, %(default)s)")
    args = parser.parse_args()
    if not args.stems_root.is_dir():
        raise SystemExit(f"no such folder: {args.stems_root}")
    song_dirs = sorted(p for p in args.stems_root.iterdir() if p.is_dir() and (p / "transcription").is_dir())
    if args.song:
        song_dirs = [p for p in song_dirs if p.name == args.song]
    song_dirs = [p for p in song_dirs if any((p / "transcription").glob("*.notes.json"))]
    if not song_dirs:
        raise SystemExit(f"no transcribed song under {args.stems_root} (run transcribe.py first)")
    print(f"PyGuitarPro {version('PyGuitarPro')}, text encoding {args.encoding}")
    for song_dir in song_dirs:
        export_song(song_dir, args.encoding, args.force)


if __name__ == "__main__":
    main()
