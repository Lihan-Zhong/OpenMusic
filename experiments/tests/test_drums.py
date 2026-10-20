"""Check experiments/drums.py on synthetic drum loops whose hits are known, and the drum MIDI of transcribe.py.

    python experiments/tests/test_drums.py

Writes small drum stems under outputs/_drums_test/<case>/drums.flac (git-ignored), each with its transcription/
next to it, so drums.mid can be opened in Guitar Pro or MuseScore by hand as well. The kits are synthesised by
experiments/drum_synth.py (its two fixed kits; training used other, random ones). The loops are 4/4 grooves a
drummer plays: a rock beat with a crash on the first beat, 16th hi-hats with a syncopated kick, a tom fill into a
crash, a ride pattern and an open hi-hat on the last 8th; humanised (+-5 ms, velocities 45-100 %). Cases: kit a
dry, kit b with room reverb and pitched bleed 25 dB down (what a separated drum stem sounds like), a fast 160 BPM
punk beat, a song whose drums stop for 20 s (no hits may be found in the silence) and a near-silent drum stem
(skipped).
Scores: mir_eval onset F-measure with 50 ms tolerance per kit piece and per group (hi-hat = closed + open, toms,
cymbals = crash + ride), and the mean timing error. The MIDI checks read drums.mid back with mido: every note on
channel 10 and a General MIDI drum note, every analysis.json beat on its own MIDI beat and the downbeats on bar
lines (test_transcribe.grid_problems).
Exits with status 1 if a score falls below its floor in FLOORS or a MIDI check fails.
"""
import json
import sys
import time
from pathlib import Path

import mido
import numpy as np
from mir_eval.util import match_events

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drum_synth as synth  # noqa: E402
import drums  # noqa: E402
import transcribe  # noqa: E402
from common import OUTPUTS, save  # noqa: E402
from test_transcribe import grid_problems  # noqa: E402

TEST_ROOT = OUTPUTS / "_drums_test"
SR = drums.SR
RNG = np.random.default_rng(0)
GROUPS = {"hi-hat": ("hihat", "hihat_open"), "toms": ("tom_hi", "tom_mid", "tom_lo"), "cymbals": ("crash", "ride")}

# lowest acceptable F per case and piece / group; set below what the current model reaches, as a regression guard
FLOORS = {
    "kit_a_dry": {"kick": 0.95, "snare": 0.95, "hi-hat": 0.9, "toms": 0.9, "cymbals": 0.6},
    "kit_b_room_bleed": {"kick": 0.95, "snare": 0.95, "hi-hat": 0.85, "toms": 0.85, "cymbals": 0.6},
    "punk_160": {"kick": 0.95, "snare": 0.95, "hi-hat": 0.9},
    "break_in_the_middle": {"kick": 0.95, "snare": 0.95, "hi-hat": 0.85, "toms": 0.8},
}

def make_cases():
    """{case: (audio, hits [(time_s, piece, velocity)], analysis.json dict, silent spans)}"""
    cases = {}
    bpm = 112.0
    for name, kit, room, bleed in (("kit_a_dry", "a", 0.0, None), ("kit_b_room_bleed", "b", 0.3, 25)):
        hits = synth.groove(RNG, bpm, 16)
        seconds = hits[-1][0] + 3
        cases[name] = (synth.render(hits, synth.KITS[kit], seconds, RNG, room, bleed), hits,
                       tempo_analysis(bpm, 1.0, seconds), [])
    hits = synth.groove(RNG, 160.0, 16, style="punk")
    seconds = hits[-1][0] + 3
    cases["punk_160"] = (synth.render(hits, synth.KITS["a"], seconds, RNG), hits, tempo_analysis(160.0, 1.0, seconds),
                         [])
    first = [h for h in synth.groove(RNG, bpm, 8) if h[0] < 1.0 + 8 * 4 * 60 / bpm - 0.05]
    second_start = first[-1][0] + 20
    hits = first + synth.groove(RNG, bpm, 8, start=second_start)
    seconds = hits[-1][0] + 3
    cases["break_in_the_middle"] = (synth.render(hits, synth.KITS["b"], seconds, RNG, 0.3, 25), hits, None,
                                    [(first[-1][0] + 2.5, second_start - 0.1)])
    cases["silent_stem"] = (10 ** (-70 / 20) * RNG.standard_normal(10 * SR), [], None, [])
    return cases


def tempo_analysis(bpm, start, seconds):
    beats = np.arange(start, seconds, 60 / bpm)
    return {"tempo": {"bpm": bpm, "beats": beats.tolist(), "downbeats": beats[::4].tolist(), "beats_per_bar": 4}}


# ---------- checks ----------

def f_measure(ref, est):
    ref, est = np.sort(ref), np.sort(est)
    if not len(ref) and not len(est):
        return None
    hit = len(match_events(ref, est, 0.05)) if len(ref) and len(est) else 0
    return 2 * hit / (len(ref) + len(est))


def check_case(name, y, hits, analysis, silent):
    folder = TEST_ROOT / name
    folder.mkdir(parents=True, exist_ok=True)
    save(folder / "drums.flac", np.stack([y, y]))
    if analysis:
        (folder / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    else:
        (folder / "analysis.json").unlink(missing_ok=True)
    start = time.perf_counter()
    transcribe.transcribe_song(folder, transcribe.find_stems(folder), ["drums"], force=True)
    took = time.perf_counter() - start
    out = folder / "transcription" / "drums.notes.json"
    failures = []
    if not hits:
        ok = not out.exists()
        print(f"  {name:22} skipped as near silent: {'yes' if ok else 'NO'}")
        return [] if ok else [name]
    data = json.loads(out.read_text(encoding="utf-8"))
    piece_of = {n: p for p, n in drums.GM_NOTE.items()}
    est = {p: np.array([n[0] for n in data["notes"] if piece_of.get(n[2]) == p]) for p in drums.PIECES}
    ref = {p: np.array([t for t, q, _ in hits if q == p]) for p in drums.PIECES}
    scores = {p: f_measure(ref[p], est[p]) for p in drums.PIECES}
    for g, pieces in GROUPS.items():
        scores[g] = f_measure(np.concatenate([ref[p] for p in pieces]), np.concatenate([est[p] for p in pieces]))
    errors = []
    for p in drums.PIECES:
        if len(ref[p]) and len(est[p]):
            errors += [est[p][j] - ref[p][i] for i, j in match_events(np.sort(ref[p]), np.sort(est[p]), 0.05)]
    line = "  ".join(f"{p} {'-' if scores[p] is None else f'{scores[p]:.2f}'}" for p in
                     ("kick", "snare", "hihat", "hihat_open", "tom_hi", "tom_mid", "tom_lo", "crash", "ride"))
    groups = "  ".join(f"{g} {'-' if scores[g] is None else f'{scores[g]:.2f}'}" for g in GROUPS)
    print(f"  {name:22} {len(hits)} hits, {len(data['notes'])} found, timing {1000 * np.mean(errors):+.1f} "
          f"+- {1000 * np.std(errors):.1f} ms, {took:.1f} s\n  {'':22} {line}\n  {'':22} {groups}")
    for key, floor in FLOORS.get(name, {}).items():
        if scores[key] is None or scores[key] < floor:
            failures.append(f"{name} {key}")
    for a, b in silent:
        stray = sum(a <= n[0] <= b for n in data["notes"])
        print(f"  {'':22} hits found in the {b - a:.0f} s without drums: {stray}")
        if stray:
            failures.append(f"{name} (hits in the silence)")
    failures += [f"{name} MIDI: {p}" for p in midi_problems(folder, analysis, data)]
    return failures


def midi_problems(folder, analysis, data):
    mid = mido.MidiFile(str(folder / "transcription" / "drums.mid"))
    notes = [m for m in mid.tracks[1] if m.type in ("note_on", "note_off")]
    problems = []
    if any(m.channel != transcribe.DRUM_CHANNEL for m in notes):
        problems.append("notes off channel 10")
    if not {m.note for m in notes} <= set(drums.GM_NOTE.values()):
        problems.append(f"unexpected notes {sorted({m.note for m in notes} - set(drums.GM_NOTE.values()))}")
    if sum(m.type == "note_on" for m in notes) != len(data["notes"]):
        problems.append("MIDI and notes.json differ in hit count")
    if set(data.get("kit", {})) != {str(n) for n in drums.GM_NOTE.values()}:
        problems.append("notes.json has no kit names")
    if analysis:
        problems += grid_problems(folder / "transcription" / "drums.mid", analysis)[0]
    return problems


def main():
    print(f"Drum transcription (model {drums.MODEL.relative_to(drums.MODEL.parent.parent.parent)}):")
    failures = []
    for name, case in make_cases().items():
        failures += check_case(name, *case)
    print("\nall checks passed" if not failures else f"\nFAILED: {', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
