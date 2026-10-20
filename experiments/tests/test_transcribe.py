"""Check experiments/transcribe.py on synthetic stems whose notes are known, and check its MIDI tempo map.

    python experiments/tests/test_transcribe.py

Writes small stem folders under outputs/_transcribe_test/<case>/<stem>.flac (git-ignored), each with its
transcription/ next to it, so the MIDI files can be opened in Guitar Pro or MuseScore by hand as well.
Cases: a sine melody; a sung-like melody (legato syllables with glides, consonants, delayed vibrato, repeated
pitches, breaths between phrases); a bass line (8ths and fast 16ths, E1-E3); a fast bass riff (16ths at 130 BPM
with A-B-A turns and octave jumps); plucked guitar (Karplus-Strong) single notes, two-note chords, and
overdriven power chords; piano-like triads with a top line; sparse notes over 5 minutes (timing drift, and a
note in the last second); a melody that drops to bleed level (gate); a near-silent stem.
Scores are mir_eval note F-measures: onset within 50 ms and pitch within 50 cents ("F onset"), the same with
the offset within 20 % of the note length or 50 ms ("F on+off"), and onset-only with octave errors forgiven
("F chroma"). The tempo-map checks read the MIDI back with mido and pretty_midi and need no audio; one of them
runs made-up beat-tracker slips through analyze.py's clean-up first, as on a real song.
Each case also checks that transcribing left the stem audio files byte for byte unchanged (it only reads them).
Exits with status 1 if a score falls below its floor in FLOORS or a stem file changed.

    python experiments/tests/test_transcribe.py --songs outputs/mp3   # only check transcribed songs' MIDI files

--songs checks every <song>/transcription/*.mid under a stems folder against the song's analysis.json: one MIDI
beat per analysis beat, and MIDI bar lines exactly on its downbeats (so bar N is the same in the player).
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import mido
import numpy as np
import pretty_midi
from mir_eval.transcription import match_notes, precision_recall_f1_overlap

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402
import transcribe  # noqa: E402
from test_analyze import tracker_slips  # noqa: E402
from common import OUTPUTS, save  # noqa: E402

TEST_ROOT = OUTPUTS / "_transcribe_test"
SR = 44100
RNG = np.random.default_rng(0)

# lowest acceptable F onset per case; set below what the current settings reach, as a regression guard
FLOORS = {"melody_sine": 0.9, "melody_voice": 0.8, "bass_line": 0.85, "bass_fast_riff": 0.9,
          "guitar_single": 0.85, "guitar_two_note_chords": 0.75, "guitar_distorted_power_chords": 0.5,
          "piano_triads": 0.75, "long_timing": 0.9, "bleed_gate": 0.85}


def hz(midi):
    return 440.0 * 2 ** ((np.asarray(midi, float) - 69) / 12)


# ---------- synthesis ----------

def envelope(n, attack=0.01, release=0.03, decay=0.0):
    t = np.arange(n) / SR
    env = np.minimum(t / attack, 1) * np.exp(-decay * t)
    r = int(release * SR)
    env[-r:] *= np.linspace(1, 0, r)
    return env


def harmonic_tone(midi, seconds, n_harmonics=1, rolloff=1.0, vibrato_cents=0.0, decay=0.0, attack=0.01):
    """Sum of harmonics 1/k**rolloff; optional 5.5 Hz vibrato; exponential decay."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    f = hz(midi) * 2 ** (vibrato_cents / 1200 * np.sin(2 * np.pi * 5.5 * t))
    phase = 2 * np.pi * np.cumsum(f) / SR
    y = sum(np.sin(k * phase) / k ** rolloff for k in range(1, n_harmonics + 1) if hz(midi) * k < SR / 2)
    return y * envelope(n, attack=attack, decay=decay)


def pluck(midi, seconds, brightness=0.5):
    """Karplus-Strong plucked string: a noise burst circulating in a delay line with a 2-point average."""
    period = max(2, int(round(SR / hz(midi) - 0.5)))
    n = int(seconds * SR)
    burst = RNG.uniform(-1, 1, period)
    burst = brightness * burst + (1 - brightness) * np.convolve(burst, np.ones(4) / 4, "same")
    out = np.zeros(n + 2 * period)
    out[1:period + 1] = burst
    for start in range(period + 1, len(out), period):
        stop = min(start + period, len(out))
        out[start:stop] = 0.498 * (out[start - period:stop - period] + out[start - period - 1:stop - period - 1])
    return out[1:n + 1] * envelope(n, attack=0.001)


def piano_tone(midi, seconds):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    f0 = hz(midi)
    y = sum(np.sin(2 * np.pi * f0 * k * t) * np.exp(-t * (0.8 + 0.6 * k)) / k
            for k in range(1, 11) if f0 * k < SR / 2)
    return y * envelope(n, attack=0.004)


def sung_phrase(notes, start, end):
    """A legato sung phrase over notes [(start_s, end_s, midi)]: one voice gliding 40 ms between notes,
    vibrato (5.5 Hz, +-40 cents) fading in after 150 ms, a dip and a consonant noise burst at each syllable."""
    t = np.arange(int((end - start) * SR)) / SR + start
    pitch = np.full(len(t), float(notes[0][2]))
    depth, onset_dist = np.zeros(len(t)), np.full(len(t), 1.0)
    for a, b, m in notes:
        inside = (t >= a) & (t < b)
        pitch[t >= a] = m
        depth[inside] = 40 * np.clip((t[inside] - a - 0.15) / 0.2, 0, 1)
        onset_dist = np.minimum(onset_dist, np.abs(t - a))
    glide = np.hanning(int(0.04 * SR))
    pitch = np.convolve(np.pad(pitch, len(glide)), glide / glide.sum(), "same")[len(glide):-len(glide)]
    cents = depth * np.sin(2 * np.pi * 5.5 * t)
    phase = 2 * np.pi * np.cumsum(hz(pitch + cents / 100)) / SR
    y = sum(np.sin(k * phase) / k ** 1.5 for k in range(1, 13))
    y *= 1 - 0.6 * np.exp(-(onset_dist / 0.025) ** 2)                         # syllable dips
    consonant = np.diff(RNG.standard_normal(len(t) + 1)) * np.exp(-(onset_dist / 0.012) ** 2)
    return (y + 0.4 * consonant) * envelope(len(t), attack=0.04, release=0.06)


def breath(seconds):
    """Unpitched breath: band-limited noise with a soft swell."""
    n = int(seconds * SR)
    noise = np.convolve(RNG.standard_normal(n), np.ones(8) / 8, "same")
    return 0.3 * noise * np.sin(np.pi * np.arange(n) / n) ** 2


def render(notes, synth, length=None, extra=()):
    """Mix notes [(start_s, end_s, midi)] made by synth(midi, seconds), plus extra [(start_s, sound)]."""
    length = length or max(n[1] for n in notes) + 1.0
    y = np.zeros(int(length * SR))
    for start, end, midi in notes:
        tone = synth(midi, end - start)
        i = int(start * SR)
        y[i:i + len(tone)] += tone[:len(y) - i]
    for start, sound in extra:
        i = int(start * SR)
        y[i:i + len(sound)] += sound[:len(y) - i]
    return y


def melody(n_notes, lo, hi, start=0.5, durations=(0.2, 0.6), gaps=(0.0, 0.15), steps=(-5, -4, -3, -2, -1,
                                                                                     1, 2, 3, 4, 5, 7)):
    notes, t, prev = [], start, (lo + hi) // 2
    for _ in range(n_notes):
        d = RNG.uniform(*durations)
        midi = int(np.clip(prev + RNG.choice(steps), lo, hi))
        notes.append((t, t + d, midi))
        t += d + RNG.uniform(*gaps)
        prev = midi
    return notes


def bass_line():
    """Two bars of rock 8ths at 120 BPM (E1-E3) four times, then a bar of 16ths."""
    eighth = 0.25
    riff = [28, 28, 40, 28, 31, 31, 33, 35, 36, 36, 43, 36, 38, 38, 40, 52]
    notes, t = [], 0.5
    for _ in range(4):
        for midi in riff:
            notes.append((t, t + eighth * 0.9, midi))
            t += eighth
    for midi in [40, 43, 45, 47, 48, 47, 45, 43, 40, 43, 45, 47, 48, 50, 52, 50]:
        notes.append((t, t + eighth / 2 * 0.9, midi))
        t += eighth / 2
    return notes


def fast_bass_riff():
    """16ths at 130 BPM (115 ms, D2-B2): turn-around figures (A-B-A) and octave jumps, the case where one voice
    must not let a long first A swallow the B."""
    sixteenth = 60 / 130 / 4
    riff = [40, 40, 43, 45, 47, 45, 43, 40, 38, 40, 43, 40, 47, 45, 43, 45,
            40, 52, 40, 52, 43, 55, 43, 55, 45, 57, 45, 57, 47, 47, 45, 43] * 2
    return [(0.5 + i * sixteenth, 0.5 + (i + 0.9) * sixteenth, midi) for i, midi in enumerate(riff)]


def two_note_chords():
    """Dyads a guitarist plays: thirds, fourths, fifths, sixths and octaves, each chord 0.8 s."""
    notes, t = [], 0.5
    for root in (40, 45, 47, 50, 52, 55, 57, 59):
        for interval in (3, 4, 5, 7, 9, 12):
            if root + interval <= 76:
                notes += [(t, t + 0.7, root), (t + 0.01, t + 0.7, root + interval)]  # 10 ms strum
                t += 0.8
    return notes


def power_chords():
    """Root-fifth-octave power chords (E2-E3 roots), 8ths at 120 BPM, two per root, strummed in 8 ms."""
    notes, t = [], 0.5
    for root in (40, 43, 45, 47, 48, 50, 52, 45, 40, 47):
        for _ in range(2):
            notes += [(t + 0.004 * i, t + 0.23, root + iv) for i, iv in enumerate((0, 7, 12))]
            t += 0.25
    return notes


def distorted(y, gain=6.0):
    """An overdriven amp: soft clipping, then a gentle low-pass for the speaker cabinet."""
    return np.convolve(np.tanh(gain * y) / np.tanh(gain), np.ones(6) / 6, "same")


def triads():
    """Close-position major and minor triads (C3-C5) for 1 s each, with a melody note on top every half."""
    notes, t = [], 0.5
    for root in (48, 53, 55, 57, 50, 52, 60, 45):
        for third in (4, 3):
            for iv in (0, third, 7):
                notes.append((t, t + 0.95, root + iv))
            notes.append((t, t + 0.45, root + 12))
            notes.append((t + 0.5, t + 0.95, root + 14))
            t += 1.0
    return notes


def make_cases():
    """{case: (stem, mono audio, reference notes, spans)}: notes are (start_s, end_s, midi); spans are
    (start_s, end_s) stretches where no note should be found (breaths, bleed)."""
    cases = {}
    notes = melody(30, 57, 76)
    cases["melody_sine"] = ("vocals", 0.3 * render(notes, harmonic_tone), notes, [])

    phrases, breaths, t = [], [], 0.5
    voice = np.zeros(int(40 * SR))
    for _ in range(4):  # legato phrases of 10 syllables, some repeating a pitch; a breath between phrases
        phrase = melody(10, 52, 72, start=t, durations=(0.2, 0.7), gaps=(0.0, 0.0),
                        steps=(-4, -3, -2, -1, 0, 0, 1, 2, 3, 4, 5))
        phrases += phrase
        i = int(t * SR)
        sung = sung_phrase(phrase, t, phrase[-1][1])
        voice[i:i + len(sung)] += sung
        breaths.append((phrase[-1][1] + 0.15, phrase[-1][1] + 0.6))
        b = breath(0.45)
        voice[int(breaths[-1][0] * SR):int(breaths[-1][0] * SR) + len(b)] += b
        t = phrase[-1][1] + 0.8
    cases["melody_voice"] = ("vocals", 0.15 * voice[:int((t + 0.5) * SR)], phrases, breaths)

    notes = bass_line()
    cases["bass_line"] = ("bass", 0.25 * render(notes, lambda m, s: harmonic_tone(
        m, s, n_harmonics=30, rolloff=1.5, decay=2.5, attack=0.003)), notes, [])
    notes = fast_bass_riff()
    cases["bass_fast_riff"] = ("bass", 0.25 * render(notes, lambda m, s: harmonic_tone(
        m, s, n_harmonics=30, rolloff=1.5, decay=2.5, attack=0.003)), notes, [])

    notes = melody(30, 40, 76, durations=(0.25, 0.5), gaps=(0.0, 0.05))
    cases["guitar_single"] = ("guitar", 0.5 * render(notes, pluck), notes, [])
    notes = two_note_chords()
    cases["guitar_two_note_chords"] = ("guitar", 0.4 * render(notes, pluck), notes, [])
    notes = power_chords()
    cases["guitar_distorted_power_chords"] = ("guitar", 0.5 * distorted(0.3 * render(notes, pluck)), notes, [])
    notes = triads()
    cases["piano_triads"] = ("piano", 0.12 * render(notes, piano_tone), notes, [])

    notes = [(t, t + 0.5, int(m)) for t, m in zip(np.arange(1.0, 300, 10.0), RNG.integers(48, 72, 30))]
    notes.append((300.9, 301.4, 60))  # in the last 0.7 % of the audio, which Basic Pitch's run_inference drops
    cases["long_timing"] = ("other", 0.3 * render(notes, piano_tone, length=302), notes, [])

    notes = melody(20, 57, 76)
    loud = render(notes, harmonic_tone)
    quiet_part = [(a + 15, b + 15, m) for a, b, m in melody(20, 57, 76)]
    bleed = render(quiet_part, harmonic_tone, length=len(loud) / SR + 16)
    loud = np.pad(loud, (0, len(bleed) - len(loud)))
    cases["bleed_gate"] = ("vocals", 0.3 * loud + 0.3 * 10 ** (-40 / 20) * bleed, notes,
                           [(quiet_part[0][0], quiet_part[-1][1])])

    cases["silent_stem"] = ("piano", 10 ** (-70 / 20) * RNG.standard_normal(10 * SR), [], [])
    return cases


def write_cases(cases):
    for case, (stem, y, notes, _) in cases.items():
        folder = TEST_ROOT / case
        folder.mkdir(parents=True, exist_ok=True)
        save(folder / f"{stem}.flac", np.stack([y, y]))


# ---------- scoring ----------

def scores(ref, est):
    """F onset, F on+off, F chroma, mean onset error (ms) of matched notes."""
    if not ref:
        return {}
    ref_iv = np.array([[a, b] for a, b, _ in ref])
    ref_p = np.array([m for _, _, m in ref])
    est_iv = np.array([[a, b] for a, b, *_ in est]).reshape(-1, 2)
    est_p = np.array([m for _, _, m, *_ in est])

    def f(rp, ep, offset_ratio):
        return precision_recall_f1_overlap(ref_iv, hz(rp), est_iv, hz(ep), onset_tolerance=0.05,
                                           offset_ratio=offset_ratio)

    p, r, f_on, _ = f(ref_p, est_p, None)
    f_off = f(ref_p, est_p, 0.2)[2]
    f_chroma = f(ref_p % 12 + 60, est_p % 12 + 60, None)[2] if len(est) else 0.0
    pairs = match_notes(ref_iv, hz(ref_p), est_iv, hz(est_p), onset_tolerance=0.05, offset_ratio=None) \
        if len(est) else []
    err = np.mean([abs(est_iv[j, 0] - ref_iv[i, 0]) for i, j in pairs]) * 1000 if len(pairs) else np.nan
    return {"P": p, "R": r, "F onset": f_on, "F on+off": f_off, "F chroma": f_chroma, "onset err ms": err}


def audio_hashes(folder):
    """sha256 of every audio file in a song folder: transcribing must only read the stems, never change them."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())
            if p.suffix.lower() in (".wav", ".flac", ".mp3")}


def check_transcription(cases):
    print(f"\nTranscription (basic-pitch {transcribe.version('basic-pitch')}, settings per stem as in "
          "transcribe.SETTINGS):")
    print(f"{'case':24} {'stem':7} {'ref':>4} {'est':>4} {'P':>5} {'R':>5} {'F onset':>8} {'F on+off':>8} "
          f"{'F chroma':>8} {'err ms':>6} {'s':>5}")
    failures = []
    for case, (stem, _, ref, empty_spans) in cases.items():
        folder = TEST_ROOT / case
        before = audio_hashes(folder)
        start = time.perf_counter()
        transcribe.transcribe_song(folder, transcribe.find_stems(folder), [stem], force=True)
        took = time.perf_counter() - start
        if audio_hashes(folder) != before:
            print(f"{case:24} the stem audio files CHANGED")
            failures.append(f"{case} (stem audio changed)")
        out = folder / "transcription" / f"{stem}.notes.json"
        if not ref:
            ok = not out.exists()
            print(f"{case:24} {stem:7} {0:4d}  skipped as near silent: {'yes' if ok else 'NO'}")
            if not ok:
                failures.append(case)
            continue
        est = json.loads(out.read_text(encoding="utf-8"))["notes"]
        s = scores(ref, est)
        print(f"{case:24} {stem:7} {len(ref):4d} {len(est):4d} {s['P']:5.2f} {s['R']:5.2f} {s['F onset']:8.2f} "
              f"{s['F on+off']:8.2f} {s['F chroma']:8.2f} {s['onset err ms']:6.1f} {took:5.1f}")
        if empty_spans:
            stray = sum(any(a - 0.05 <= n[0] <= b for a, b in empty_spans) for n in est)
            what = "breaths" if case == "melody_voice" else "the -40 dB bleed"
            print(f"{'':24} notes starting in {what}: {stray}")
            if stray > 1:
                failures.append(f"{case} (stray notes)")
        if case == "long_timing":
            late = [abs(e[0] - r[0]) * 1000 for r in ref if r[0] > 200 for e in est
                    if e[2] == r[2] and abs(e[0] - r[0]) < 0.1]
            last = any(e[2] == ref[-1][2] and abs(e[0] - ref[-1][0]) < 0.05 for e in est)
            print(f"{'':24} onset error of notes after 200 s: max {max(late, default=np.nan):.1f} ms; last "
                  f"note ({ref[-1][0]:.1f} of {len(cases[case][1]) / SR:.0f} s) found: {'yes' if last else 'NO'}")
            if not last:
                failures.append(f"{case} (end of the audio)")
        if s["F onset"] < FLOORS.get(case, 0):
            failures.append(case)
    return failures


# ---------- tempo map ----------

def midi_bar_starts(mid, end=None):
    """Ticks where bars start, from the time signatures in the MIDI file, up to the last event (or tick end)."""
    sigs, now = [], 0
    for msg in mid.tracks[0]:
        now += msg.time
        if msg.type == "time_signature":
            sigs.append((now, msg.numerator * mid.ticks_per_beat * 4 // msg.denominator))
    end = max(sum(m.time for m in track) for track in mid.tracks) if end is None else end
    starts = []
    for i, (tick, length) in enumerate(sigs):
        stop = sigs[i + 1][0] if i + 1 < len(sigs) else end + 1
        starts += list(range(tick, stop, length))
    return set(starts)


def tempo_grids():
    """{name: (analysis, true beat times, indices of the true downbeats, most time signatures expected)}."""
    grids = {}
    # a live band: 1.3 s lead-in, one pickup beat, 92 -> 118 BPM, 6 ms jitter, one 2/4 bar (beats 61-62)
    periods = 60 / np.concatenate([np.full(40, 92.0), np.linspace(92, 118, 60), np.full(60, 118.0)])
    beats = 1.3 + np.concatenate([[0], np.cumsum(periods[:-1])]) + RNG.normal(0, 0.006, len(periods))
    down = list(range(1, 62, 4)) + list(range(63, len(beats), 4))
    grids["drifting_92_to_118"] = ({"tempo": {"beats": beats.tolist(), "downbeats": beats[down].tolist(),
                                              "beats_per_bar": 4, "bpm": 105.0}}, beats, down, 4)
    beats = 0.5 + 0.5 * np.arange(200) + RNG.normal(0, 0.004, 200)
    down = list(range(0, 200, 4))
    grids["steady_120"] = ({"tempo": {"beats": beats.tolist(), "downbeats": beats[down].tolist(),
                                      "beats_per_bar": 4, "bpm": 120.0}}, beats, down, 2)
    # what the beat tracker does on a noisy rock song (test_analyze.tracker_slips: extra beats, near-duplicates,
    # stretches counted in half, double and quadruple time, a break, stray and missing downbeats), cleaned up by
    # analyze.py as it does for a real song
    detected, said_down, confidence, beats, down = tracker_slips()
    found, bars, _ = analyze.regular_grid(detected, said_down, confidence)
    grids["tracker_slips"] = ({"tempo": {"beats": found.tolist(), "downbeats": bars.tolist(),
                                         "beats_per_bar": analyze.beats_per_bar(found, bars), "bpm": 140.0}},
                              beats, list(down), 2)
    grids["no_analysis"] = (None, np.array([]), [], 1)
    return grids


def grid_problems(mid_path, analysis):
    """Compare a MIDI file with the analysis.json it was written from: beat k of analysis.json must be MIDI beat
    lead + k (within TEMPO_TOLERANCE_S), and from the first downbeat to the last beat the MIDI bar lines must be
    exactly the analysis downbeats; before the first downbeat, one short bar (the player's bar 0) at most.
    Returns (problems, one-line summary)."""
    mid, pm = mido.MidiFile(str(mid_path)), pretty_midi.PrettyMIDI(str(mid_path))
    tpb = mid.ticks_per_beat
    beats, downbeats = np.array(analysis["tempo"]["beats"]), np.array(analysis["tempo"]["downbeats"])
    midi_beat = np.round([pm.time_to_tick(t) / tpb for t in beats]).astype(int)
    off_ms = np.abs([pm.tick_to_time(int(k * tpb)) - t for k, t in zip(midi_beat, beats)]) * 1000
    problems = []
    if np.any(np.diff(midi_beat) != 1):
        problems.append(f"{int(np.sum(np.diff(midi_beat) != 1))} analysis beats not one MIDI beat apart")
    if off_ms.max() > transcribe.TEMPO_TOLERANCE_S * 1000 + 2:
        problems.append(f"a beat {off_ms.max():.0f} ms off its MIDI beat")
    summary = (f"{len(beats)} analysis beats -> MIDI beats {midi_beat[0]}..{midi_beat[-1]} "
               f"({midi_beat[-1] - midi_beat[0] + 1}), {off_ms.max():.1f} ms off at most")
    if len(downbeats):
        down_ticks = {int(midi_beat[np.argmin(np.abs(beats - d))]) * tpb for d in downbeats}
        first, last = min(down_ticks), int(midi_beat[-1]) * tpb
        bars = midi_bar_starts(mid, end=last)
        inside = {b for b in bars if first <= b <= last}
        before = sorted(b for b in bars if b < first)
        if inside != down_ticks:
            problems.append(f"{len(inside ^ down_ticks)} bar lines differ from the analysis downbeats")
        if len(before) > 1:
            problems.append(f"{len(before)} MIDI bars before the first downbeat (the player has one)")
        summary += (f"; {len(downbeats)} downbeats -> {len(inside)} MIDI bar lines, {len(before)} bar before the "
                    f"first ({(first - before[0]) // tpb if before else 0} beats)")
    return problems, summary


def check_songs(stems_root):
    """grid_problems for every transcription/*.mid of the songs under stems_root."""
    print(f"\nMIDI against analysis.json, songs under {stems_root}:")
    failures = []
    for song in sorted(p for p in Path(stems_root).iterdir() if (p / "analysis.json").is_file()):
        analysis = json.loads((song / "analysis.json").read_text(encoding="utf-8"))
        mids = sorted((song / "transcription").glob("*.mid"))
        for mid_path in mids:
            problems, summary = grid_problems(mid_path, analysis)
            if mid_path == mids[0] or problems:
                print(f"  {song.name} {mid_path.stem}: {summary}{'; ' + '; '.join(problems) if problems else ''}")
            if problems:
                failures.append(f"{song.name} {mid_path.stem}")
        print(f"  {song.name}: {len(mids)} MIDI files checked")
    return failures


def check_tempo_map():
    """Write a MIDI file with a note on every true beat and read it back: every beat should land on its own
    MIDI beat (consecutive beats one MIDI beat apart), every true downbeat on a bar line, few time signatures."""
    print("\nTempo map:")
    failures = []
    for name, (analysis, beats, down, max_sigs) in tempo_grids().items():
        knots, sigs, note = transcribe.tempo_map(analysis)
        notes = [(round(float(t), 3), round(float(t) + 0.2, 3), 60, 100) for t in beats] or \
            [(1.0 * i, 1.0 * i + 0.2, 60, 100) for i in range(10)]
        path = TEST_ROOT / "tempo_map" / f"{name}.mid"
        transcribe.write_midi(path, notes, "guitar", knots, sigs, key="Em")
        mid = mido.MidiFile(str(path))
        ticks, now = [], 0
        for msg in mid.tracks[1]:
            now += msg.time
            if msg.type == "note_on":
                ticks.append(now)
        pm = pretty_midi.PrettyMIDI(str(path))  # reads the tempo map on its own
        back = np.array(sorted(n.start for n in pm.instruments[0].notes))
        roundtrip_ms = np.max(np.abs(back - np.array([n[0] for n in notes]))) * 1000
        line = (f"  {name:20} {note}\n  {'':20} note times after the MIDI round trip: "
                f"{roundtrip_ms:.1f} ms off at most")
        ok = roundtrip_ms < 3 and len(sigs) <= max_sigs
        if len(beats):
            midi_beat = np.round(np.array(ticks) / mid.ticks_per_beat).astype(int)
            off_ms = np.abs([pm.tick_to_time(int(k * mid.ticks_per_beat)) - t
                             for k, t in zip(midi_beat, beats)]) * 1000
            steps_ok = bool(np.all(np.diff(midi_beat) == 1))
            bars = midi_bar_starts(mid)
            on_bar = np.mean([int(midi_beat[i]) * mid.ticks_per_beat in bars for i in down])
            n_tempi = sum(m.type == "set_tempo" for m in mid.tracks[0])
            line += (f"\n  {'':20} beats off their MIDI beat: max {off_ms.max():.1f} ms; consecutive beats one "
                     f"MIDI beat apart: {steps_ok}; downbeats on a bar line: {on_bar:.0%}; {n_tempi} tempo "
                     f"events; time signatures {sigs[:5]}{' ...' if len(sigs) > 5 else ''}")
            ok &= off_ms.max() <= transcribe.TEMPO_TOLERANCE_S * 1000 + 2 and steps_ok and on_bar == 1.0
            problems, summary = grid_problems(path, analysis)
            line += f"\n  {'':20} against analysis.json: {summary}{'; ' + '; '.join(problems) if problems else ''}"
            ok &= not problems
        print(line)
        if not ok:
            failures.append(f"tempo_map {name}")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--songs", type=Path, help="only check the MIDI files of the songs under this stems folder")
    args = parser.parse_args()
    if args.songs:
        failures = check_songs(args.songs)
        print("\nall checks passed" if not failures else f"\nFAILED: {', '.join(failures)}")
        sys.exit(1 if failures else 0)
    cases = make_cases()
    write_cases(cases)  # a few seconds; always fresh, so the audio matches the reference notes
    failures = check_transcription(cases) + check_tempo_map()
    print("\nall checks passed" if not failures else f"\nFAILED: {', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
