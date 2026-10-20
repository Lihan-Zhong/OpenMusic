"""Write down the notes of each separated stem, for 扒谱 (working a part out by ear): a MIDI file to open in
Guitar Pro / MuseScore / a DAW, and a notes.json for the practice player's piano roll.

    python experiments/transcribe.py outputs/mp3
    python experiments/transcribe.py outputs/mp3 --song "Carsick Cars - 中南海" --stems bass,guitar --force
    python experiments/transcribe.py outputs/mp3 --stems guitar --frame 0.5     # fewer, surer guitar notes
    python experiments/transcribe.py outputs/mp3 --stems drums                  # only the drum hits

For each <stems_root>/<song>/ folder, every stem with audible content gets
  <song>/transcription/<stem>.mid          one note track, tempo map from <song>/analysis.json if present
  <song>/transcription/<stem>.notes.json   {"version": 1, "stem", "notes": [[start_s, end_s, pitch, velocity]],
                                            "pitch_range": [lo, hi], "method"}
Near-silent stems (e.g. an empty piano stem) are skipped, and old outputs of them removed. Outputs newer than
both the stem and analysis.json are kept unless --force.

Drums: drums.py finds the hits of the drum stem (kick, snare, three toms, closed and open hi-hat, crash, ride)
and drums.mid is a General MIDI drum track on channel 10 (kick 36, snare 38, toms 50 / 47 / 43, hi-hat 42 / 46,
crash 49, ride 51), so Guitar Pro / MuseScore open it as a drum kit; each hit is 0.1 s long. Its notes.json has
the GM note as "pitch" and also "kit": {GM note: name}.

Pitched notes come from Spotify's Basic Pitch (basic-pitch 0.4.0, code and model Apache-2.0,
https://github.com/spotify/basic-pitch): its ONNX model on onnxruntime and its own note decoding
(note_creation.model_output_to_notes). Per stem we only set Basic Pitch's own knobs (onset/frame thresholds,
shortest note, pitch range). On top of that, notes are dropped where the stem is near silent (Basic Pitch
rescales every 2 s window to full scale, so faint bleed in a separated stem would turn into notes), and
vocals and bass keep one voice, the strongest of overlapping notes.
basic-pitch 0.4.0 pins TensorFlow < 2.15.1, which has no Python 3.12 wheels, so install it without its
dependencies; the ONNX path needs none of TensorFlow, CoreML or TFLite (resampy 0.4.3 instead of its pinned
< 0.4.3, whose pkg_resources import fails on current setuptools; Basic Pitch only uses it for sonification):
    pip install onnxruntime==1.30.0 pretty_midi==0.2.11.post0 mido==1.3.3 mir_eval==0.8.2 resampy==0.4.3
    pip install --no-deps basic-pitch==0.4.0

Tempo map: with beats in analysis.json, the MIDI tempo follows the song: its beats and bar lines are used as they
are (analyze.py has already cleaned them up), so every beat of analysis.json is one MIDI beat (within
TEMPO_TOLERANCE_S) and every downbeat a MIDI bar line, and bar N in the MIDI is bar N in the practice player. The
tempo changes only where a steady tempo stops fitting the beats, which keeps the score readable. The time
before the first beat becomes lead-in beats, and everything before the first downbeat one short first bar (a
pickup, the player's bar 0) when it fits. Without beats: a fixed tempo (the analysis BPM, or 120). Either way
each note keeps its time in seconds, so the MIDI lines up with the audio in a DAW.
"""
import argparse
import json
import logging
import time
from importlib.metadata import version
from pathlib import Path

import librosa
import mido
import numpy as np

import drums
from common import load

SR = 22050                      # Basic Pitch's sample rate
PITCHED = ("vocals", "bass", "guitar", "piano", "other")      # through Basic Pitch
STEM_NAMES = ("vocals", "drums", "bass", "guitar", "piano", "other")
EXT_PREFERENCE = (".flac", ".wav", ".mp3")
DRUM_CHANNEL = 9                # MIDI channel 10, General MIDI percussion

# Basic Pitch settings per stem: onset / frame = its onset and note thresholds (its defaults are 0.5 / 0.3;
# higher = fewer, surer notes); min_ms = shortest note in Basic Pitch's terms (its default 127.7 ms would drop
# fast 16ths; it keeps notes one 11.6 ms frame longer than this, so 70 ms means 81 ms and 100 ms means 116 ms;
# 70 ms - 1 frame would keep more 16ths above ~170 BPM but also more short octave ghosts on guitar); lo / hi
# = MIDI pitch range; one_voice = keep only the strongest of overlapping notes; program = General MIDI sound
# (0-based). Note threshold 0.4 instead of 0.3 drops most ghost notes on the overtones of real ones; vocals
# use onset 0.6 because vibrato and glides set off extra onsets (experiments/tests/test_transcribe.py).
SETTINGS = {
    "vocals": dict(onset=0.6, frame=0.4, min_ms=100, lo=40, hi=84, one_voice=True, program=53),   # E2-C6
    "bass": dict(onset=0.5, frame=0.4, min_ms=70, lo=23, hi=67, one_voice=True, program=33),      # B0-G4
    "guitar": dict(onset=0.5, frame=0.4, min_ms=70, lo=35, hi=88, one_voice=False, program=27),   # B1-E6
    "piano": dict(onset=0.5, frame=0.4, min_ms=70, lo=21, hi=108, one_voice=False, program=0),    # A0-C8
    "other": dict(onset=0.5, frame=0.4, min_ms=70, lo=21, hi=108, one_voice=False, program=4),
}
TRACK_NAMES = {"vocals": "Vocals (melody)", "drums": "Drums", "bass": "Bass", "guitar": "Guitar", "piano": "Piano",
               "other": "Other"}

HOP = 256                       # Basic Pitch's frame hop (11.6 ms); the loudness envelope uses it too
SILENT_DB = -45.0               # a stem counts as audible where its RMS is above this (dBFS) ...
MIN_AUDIBLE_S = 2.0             # ... for at least this long; otherwise it is skipped
GATE_DB = 30.0                  # drop notes more than this below the stem's loud level (99th percentile)

PPQ = 480                       # MIDI ticks per beat
TEMPO_TOLERANCE_S = 0.025       # every analysis.json beat lands within this of its MIDI beat
DEFAULT_BPM = 120.0
FLAT_NAMES = {"C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb"}


# ---------- Basic Pitch ----------

_basic_pitch = {}


def basic_pitch():
    """Basic Pitch's modules and its ONNX model, loaded once (quietly: on import basic_pitch warns about
    each backend it lacks)."""
    if not _basic_pitch:
        logging.disable(logging.WARNING)
        try:
            from basic_pitch import FilenameSuffix, build_icassp_2022_model_path, constants, inference, \
                note_creation
        except (ImportError, NameError) as e:  # NameError: basic_pitch found no backend at all
            raise SystemExit("basic-pitch with onnxruntime is needed, see the install lines at the top of "
                             f"experiments/transcribe.py ({e})")
        finally:
            logging.disable(logging.NOTSET)
        _basic_pitch.update(constants=constants, inference=inference, note_creation=note_creation,
                            model=inference.Model(build_icassp_2022_model_path(FilenameSuffix.onnx)))
    return _basic_pitch


def model_output(y):
    """Basic Pitch's note / onset / contour activations for mono 22.05 kHz audio.

    The same steps as basic_pitch.inference.run_inference, but on audio already in memory: 2 s windows that
    overlap by 30 frames, then the overlaps cut off again. One difference: run_inference keeps 86 frames per
    second of audio, but its frames cover a little less than 1/86 s each (see
    note_creation.model_frames_to_time), so it drops the last 0.7 % of the audio (2.7 s of a 6:45 song). Here
    the frames are cut where their time passes the end of the audio.
    """
    bp = basic_pitch()
    n_overlap = 30
    overlap_len = n_overlap * bp["constants"].FFT_HOP
    padded = np.concatenate([np.zeros(overlap_len // 2, np.float32), y.astype(np.float32)])
    out = {"note": [], "onset": [], "contour": []}
    hop = bp["constants"].AUDIO_N_SAMPLES - overlap_len
    for window, _ in bp["inference"].window_audio_file(padded, hop):
        for k, v in bp["model"].predict(window[np.newaxis]).items():
            out[k].append(v)
    out = {k: bp["inference"].unwrap_output(np.concatenate(v), 2 * len(y), n_overlap) for k, v in out.items()}
    n = int(np.searchsorted(bp["note_creation"].model_frames_to_time(len(out["note"])), len(y) / SR))
    return {k: v[:n] for k, v in out.items()}


def basic_pitch_notes(y, s):
    """Notes [(start_s, end_s, pitch, amplitude 0-1)] from Basic Pitch's own decoding with settings s."""
    _, events = basic_pitch()["note_creation"].model_output_to_notes(
        model_output(y), onset_thresh=s["onset"], frame_thresh=s["frame"],
        min_note_len=int(round(s["min_ms"] / 1000 * SR / HOP)),
        min_freq=float(librosa.midi_to_hz(s["lo"])), max_freq=float(librosa.midi_to_hz(s["hi"] + 1)),
        include_pitch_bends=False, melodia_trick=True)
    return [(float(a), float(b), int(p), float(amp)) for a, b, p, amp, _ in events]


# ---------- clean-up ----------

def loudness_db(y):
    """RMS in dBFS per Basic Pitch frame."""
    rms = librosa.feature.rms(y=y, frame_length=4 * HOP, hop_length=HOP)[0]
    return 20 * np.log10(rms + 1e-10)


def gate(notes, db):
    """Drop notes that never get within GATE_DB of the stem's loud level: bleed and noise, not playing."""
    floor = max(np.percentile(db, 99) - GATE_DB, SILENT_DB - 10)
    kept = []
    for n in notes:
        a, b = int(n[0] * SR / HOP), int(np.ceil(n[1] * SR / HOP)) + 1
        if db[a:b].size and db[a:b].max() >= floor:
            kept.append(n)
    return kept


def one_voice(notes, min_s):
    """Keep the strongest of overlapping notes, then cut each note where the next one starts, so a melody or
    a bass line reads as a single line. Going from loudest x longest down, a note is dropped when half of it
    lies under kept notes that are at least as loud (a ghost under a real note). A louder note inside a longer,
    weaker one is kept and cuts it: Basic Pitch bridges short dips, so in a fast A-B-A figure the first A
    often lasts until the second and would otherwise swallow the B."""
    kept = []
    for n in sorted(notes, key=lambda n: -n[3] * (n[1] - n[0])):
        overlap = sum(max(0.0, min(n[1], k[1]) - max(n[0], k[0])) for k in kept if k[3] >= n[3])
        if overlap < 0.5 * (n[1] - n[0]):
            kept.append(n)
    kept.sort()
    out = []
    for i, (a, b, p, amp) in enumerate(kept):
        if i + 1 < len(kept):
            b = min(b, kept[i + 1][0])
        if b - a >= min_s:
            out.append((a, b, p, amp))
    return out


def untangle_same_pitch(notes):
    """MIDI cannot hold two overlapping notes of one pitch: end the earlier one where the later one starts."""
    out, last = [], {}
    for a, b, p, amp in sorted(notes):
        if p in last and out[last[p]][1] > a:
            out[last[p]] = out[last[p]][:1] + (a,) + out[last[p]][2:]
        last[p] = len(out)
        out.append((a, b, p, amp))
    return [n for n in out if n[1] > n[0]]


def transcribe(y, stem):
    """Notes [(start_s, end_s, pitch, velocity 1-127)] for one mono 22.05 kHz stem."""
    s = SETTINGS[stem]
    notes = gate(basic_pitch_notes(y, s), loudness_db(y))
    if s["one_voice"]:
        notes = one_voice(notes, s["min_ms"] / 1000)
    notes = untangle_same_pitch(notes)
    return [(round(a, 3), round(b, 3), p, int(np.clip(round(127 * amp), 1, 127))) for a, b, p, amp in notes]


# ---------- tempo map ----------

def steady_runs(beats):
    """Indices of the beats where the tempo changes: from one to the next, a constant tempo puts every
    beat within TEMPO_TOLERANCE_S of where it was detected. Greedy, longest run first."""
    ends, i = [], 0
    while i < len(beats) - 1:
        j = i + 1
        while j + 1 < len(beats):
            run = beats[i:j + 2]
            if np.max(np.abs(run - np.linspace(run[0], run[-1], len(run)))) > TEMPO_TOLERANCE_S:
                break
            j += 1
        ends.append(j)
        i = j
    return ends


def bar_signatures(starts, per_bar):
    """[(MIDI beat, beats per bar)] wherever the bar length changes, for bars starting at the given MIDI
    beats; before the first one come whole bars and a short first bar (pickup)."""
    first = starts[0]
    bars = [(0, first % per_bar)] if first % per_bar else []
    bars += [(b, per_bar) for b in range(first % per_bar, first, per_bar)]
    bars += [(b, nxt - b) for b, nxt in zip(starts, starts[1:])] + [(starts[-1], per_bar)]
    return [bars[0]] + [bar for prev, bar in zip(bars, bars[1:]) if bar[1] != prev[1]]


def tempo_map(analysis):
    """(knots, signatures, note): knots = [(seconds, MIDI beat)] with a constant tempo between neighbours
    and after the last; signatures = [(MIDI beat, beats per bar)]; note says what was done, for "method".
    Beat k of analysis.json is MIDI beat lead + k, and each of its downbeats starts a MIDI bar."""
    tempo = (analysis or {}).get("tempo") or {}
    beats = np.asarray(tempo.get("beats") or [], float)
    per_bar = tempo.get("beats_per_bar") or 4
    if len(beats) < 4 or np.any(np.diff(beats) <= 0):
        bpm = float(tempo.get("bpm") or DEFAULT_BPM)
        return [(0.0, 0.0), (60.0 / bpm, 1.0)], [(0, per_bar)], f"fixed tempo {bpm:g} BPM"
    period = float(np.median(np.diff(beats)))
    downbeats = tempo.get("downbeats") or []
    down_idx = sorted({i for i, d in ((int(np.argmin(np.abs(beats - d))), d) for d in downbeats)
                       if abs(beats[i] - d) < 0.25 * period})
    # lead-in beats before the first beat: as many as fit its length, but at least one (MIDI beat 0 is at 0 s)
    # and, when there are bars, few enough that everything before the first downbeat is one short bar
    lead = int(round(beats[0] / period))
    if down_idx:
        lead = min(lead, per_bar - 1 - down_idx[0])
    if beats[0] > 0.02:
        lead = max(lead, 1)
    lead = max(lead, 0)
    ends = steady_runs(beats)
    knots = [(0.0, 0.0)] + [(float(beats[i]), float(lead + i)) for i in ([0] if lead else []) + ends]
    note = (f"tempo map from the {len(beats)} analysis.json beats, one MIDI beat each after a lead-in of {lead} "
            f"beat{'' if lead == 1 else 's'} ({len(knots) - 1} tempos; every beat within "
            f"{TEMPO_TOLERANCE_S * 1000:.0f} ms of its MIDI beat")
    if not down_idx:
        return knots, [(0, per_bar)], note + f"), {per_bar} beats per bar"
    starts = [lead + i for i in down_idx]
    return knots, bar_signatures(starts, per_bar), note + f"), bars from its {len(down_idx)} downbeats"


def seconds_to_beats(t, knots):
    """MIDI beat position of times t (s): linear between knots, the last tempo carried on after them."""
    ts, bs = np.array(knots).T
    t = np.asarray(t, float)
    slope = (bs[-1] - bs[-2]) / (ts[-1] - ts[-2])
    return np.where(t <= ts[-1], np.interp(t, ts, bs), bs[-1] + (t - ts[-1]) * slope)


def mido_key(key):
    """'E minor' -> 'Em' for the MIDI key signature (None if unknown). Major keys mido only knows by their
    flat name (D# / G# / A# major) are written as Eb / Ab / Bb."""
    if not key or not key.get("label_en"):
        return None
    tonic, _, mode = key["label_en"].partition(" ")
    suffix = "m" if mode == "minor" else ""
    for name in (tonic + suffix, FLAT_NAMES.get(tonic, tonic) + suffix):
        try:
            mido.MetaMessage("key_signature", key=name)
            return name
        except (ValueError, KeyError):
            pass
    return None


def write_midi(path, notes, stem, knots, signatures, key=None):
    conductor = [(0, 0, mido.MetaMessage("track_name", name="Tempo map"))]
    for (t0, b0), (t1, b1) in zip(knots, knots[1:]):
        conductor.append((round(b0 * PPQ), 1, mido.MetaMessage("set_tempo",
                                                               tempo=round((t1 - t0) / (b1 - b0) * 1e6))))
    for beat, per_bar in signatures:
        conductor.append((beat * PPQ, 2, mido.MetaMessage("time_signature", numerator=per_bar, denominator=4)))
    if key:
        conductor.append((0, 3, mido.MetaMessage("key_signature", key=key)))

    channel = DRUM_CHANNEL if stem == "drums" else 0
    program = 0 if stem == "drums" else SETTINGS[stem]["program"]  # drums: the standard kit
    part = [(0, 0, mido.MetaMessage("track_name", name=TRACK_NAMES[stem])),
            (0, 1, mido.Message("program_change", program=program, channel=channel))]
    if notes:
        starts, ends, pitches, velocities = zip(*notes)
        on = np.round(seconds_to_beats(starts, knots) * PPQ).astype(int)
        off = np.maximum(np.round(seconds_to_beats(ends, knots) * PPQ).astype(int), on + 1)
        for a, b, p, v in zip(on, off, pitches, velocities):
            part.append((int(a), 3, mido.Message("note_on", note=p, velocity=v, channel=channel)))
            part.append((int(b), 2, mido.Message("note_off", note=p, velocity=0, channel=channel)))

    mid = mido.MidiFile(type=1, ticks_per_beat=PPQ)
    for events in (conductor, part):
        track, now = mido.MidiTrack(), 0
        for tick, _, msg in sorted(events, key=lambda e: e[:2]):  # at one tick: meta, note offs, note ons
            track.append(msg.copy(time=tick - now))
            now = tick
        mid.tracks.append(track)
    path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(str(path))


# ---------- songs ----------

def find_stems(song_dir):
    """{stem name: path} for the stems, taking .flac over .wav over .mp3."""
    found = {}
    for ext in reversed(EXT_PREFERENCE):
        for name in STEM_NAMES:
            if (song_dir / f"{name}{ext}").is_file():
                found[name] = song_dir / f"{name}{ext}"
    return found


def read_analysis(song_dir):
    path = song_dir / "analysis.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # half-written or broken: fall back to a fixed tempo


def method_text(stem, tempo_note):
    if stem == "drums":
        return (f"drums.py: onset candidates + a small neural network trained on E-GMD (CC BY 4.0), "
                f"{len(drums.PIECES)} kit pieces as General MIDI drums on channel 10; MIDI: {tempo_note}")
    s = SETTINGS[stem]
    lo, hi = librosa.midi_to_note(s["lo"], unicode=False), librosa.midi_to_note(s["hi"], unicode=False)
    return (f"basic-pitch {version('basic-pitch')} (ONNX, onnxruntime {version('onnxruntime')}), "
            f"onset {s['onset']}, frame {s['frame']}, min note {s['min_ms']} ms, pitch {lo}-{hi}"
            f"{', one voice' if s['one_voice'] else ', polyphonic'}, notes > {GATE_DB:g} dB below the stem's "
            f"loud level dropped; MIDI: {tempo_note}")


def up_to_date(outputs, inputs):
    if not all(p.exists() for p in outputs):
        return False
    return min(p.stat().st_mtime for p in outputs) > max(p.stat().st_mtime for p in inputs if p.exists())


def transcribe_song(song_dir, stems, wanted, force=False):
    analysis = read_analysis(song_dir)
    knots, signatures, tempo_note = tempo_map(analysis)
    key = mido_key((analysis or {}).get("key"))
    out_dir = song_dir / "transcription"
    for stem in [s for s in wanted if s in stems]:
        mid_path, json_path = out_dir / f"{stem}.mid", out_dir / f"{stem}.notes.json"
        if not force and up_to_date([mid_path, json_path], [stems[stem], song_dir / "analysis.json"]):
            print(f"  {stem}: up to date (use --force to redo)")
            continue
        start = time.perf_counter()
        sr = drums.SR if stem == "drums" else SR
        y = load(stems[stem], sr=sr).mean(axis=0)
        audible_s = float(np.sum(loudness_db(y) > SILENT_DB)) * HOP / sr
        if audible_s < MIN_AUDIBLE_S:
            print(f"  {stem}: skipped, near silent (only {audible_s:.1f} s above {SILENT_DB:g} dBFS)")
            for old in (mid_path, json_path):
                old.unlink(missing_ok=True)
            continue
        notes = drums.transcribe(y) if stem == "drums" else transcribe(y, stem)
        pitches = [n[2] for n in notes]
        pitch_range = [min(pitches), max(pitches)] if notes else []
        out = {"version": 1, "stem": stem, "notes": [list(n) for n in notes], "pitch_range": pitch_range,
               "method": method_text(stem, tempo_note)}
        if stem == "drums":
            out["kit"] = {str(drums.GM_NOTE[p]): drums.NAMES[p] for p in drums.PIECES}
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        write_midi(mid_path, notes, stem, knots, signatures, key)
        if stem == "drums":
            count = {p: sum(n[2] == drums.GM_NOTE[p] for n in notes) for p in drums.PIECES}
            span = ", ".join(f"{drums.NAMES[p].lower()} {n}" for p, n in count.items() if n) or "no hits"
            print(f"  drums: {len(notes)} hits ({span}), {time.perf_counter() - start:.1f} s")
            continue
        span = "-".join(librosa.midi_to_note(p, unicode=False) for p in pitch_range) or "no notes"
        print(f"  {stem}: {len(notes)} notes, {span}, {time.perf_counter() - start:.1f} s")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stems_root", type=Path)
    parser.add_argument("--song", help="only this song folder")
    parser.add_argument("--stems", default=",".join(STEM_NAMES), help="comma-separated, e.g. bass,guitar")
    parser.add_argument("--force", action="store_true", help="redo stems whose outputs are up to date")
    parser.add_argument("--onset", type=float, help="Basic Pitch onset threshold for these pitched stems (0-1; "
                                                    "implies --force); lower finds more notes")
    parser.add_argument("--frame", type=float, help="Basic Pitch note threshold for these pitched stems (0-1; "
                                                    "implies --force); lower finds more and longer notes")
    args = parser.parse_args()
    wanted = [s.strip() for s in args.stems.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in STEM_NAMES]
    if unknown:
        raise SystemExit(f"unknown stem(s) {unknown}; choose from {', '.join(STEM_NAMES)}")
    for name in [s for s in wanted if s in PITCHED]:  # Basic Pitch's knobs; drums have none
        for knob in ("onset", "frame"):
            if getattr(args, knob) is not None:
                SETTINGS[name][knob] = getattr(args, knob)
                args.force = True

    if not args.stems_root.is_dir():
        raise SystemExit(f"no such folder: {args.stems_root}")
    song_dirs = sorted(p for p in args.stems_root.iterdir() if p.is_dir())
    if args.song:
        song_dirs = [p for p in song_dirs if p.name == args.song]
    done = 0
    for song_dir in song_dirs:
        stems = find_stems(song_dir)
        if not stems:
            continue
        print(f"{song_dir.name}: {tempo_map(read_analysis(song_dir))[2]}")
        transcribe_song(song_dir, stems, wanted, args.force)
        done += 1
    if not done:
        raise SystemExit(f"no song folder with stems matched under {args.stems_root}")


if __name__ == "__main__":
    main()
