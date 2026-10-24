"""Check experiments/analyze.py on synthetic songs whose tempo, key and chords are known.

    python experiments/tests/test_analyze.py            # quick checks (~20 s), exit code 1 on failure
    python experiments/tests/test_analyze.py --full     # every song, with the tables used to pick the settings
    python experiments/tests/test_analyze.py --remake   # synthesize the songs again first

The first run synthesizes the songs (about 2.5 minutes, 320 MB) under outputs/_analysis_test/ (git-ignored):
  tempo/<name>/{drums,bass}.flac  drum + bass loops at 70-174 BPM, one 3/4 song, one tempo change, one speed-up
  key/<kind>_<tonic>/{piano,bass,vocals,drums}.flac  chords, bass line and melody in each of the 24 keys:
      major I-V-vi-IV, minor i-VI-iv-V (harmonic minor), and a harder minor set i-VI-III-VII ("aeolian"),
      which uses exactly the chords of its relative major, so its key is ambiguous by construction
  chords_<split>/<nn>/{guitar,piano,bass,vocals,drums}.flac + truth.json  random 16-bar progressions in a random
      key, tempo and tuning: strummed guitar ("rock" songs: overdriven, with some power chords), an optional
      pad, a bass line, a sung-like melody with passing notes and vibrato, drums and vocals leaking into the
      guitar, room reverb; 7th, sus, dim and slash chords, and some borrowed chords.
      "train" songs were used to choose analyze.py's settings; "test" songs are only scored (AGENTS.md).
  edge/<name>/  cut from key/major_E: 2 s long, silent, drums only, no bass, faint noise as the bass stem
Beat grid: made-up beat-tracker output with known true beats and bars (extra and missed beats, near-duplicates,
half / double / quadruple time, a break, stray and unsure downbeats, a real 2/4 bar, a real tempo change) through
analyze.regular_grid; no audio needed.
Tempo passes within 1 % of the true BPM (half or double time counted separately); keys are scored as exact /
fifth / relative / parallel / other, like the MIREX key task; chords with mir_eval's standard scores (root,
majmin, mirex, thirds, triads, sevenths, tetrads), weighted by duration. Needs mir_eval (MIT, dev only).
"""
import argparse
import json
import sys
import time
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import analyze  # noqa: E402
from common import OUTPUTS, load, save  # noqa: E402

TEST_ROOT = OUTPUTS / "_analysis_test"
SR = 44100

TEMPO_SONGS = {  # name: (list of (bpm, seconds) sections, beats per bar)
    "070bpm": ([(70, 40)], 4), "090bpm": ([(90, 40)], 4), "120bpm": ([(120, 40)], 4),
    "128bpm": ([(128, 40)], 4), "150bpm": ([(150, 40)], 4), "174bpm": ([(174, 40)], 4),
    "waltz_120bpm_3-4": ([(120, 40)], 3),
    "change_100_to_130bpm": ([(100, 24), (130, 24)], 4),
    "speedup_90_to_120bpm": ([(90 + 30 * i / 19, 2.4) for i in range(20)], 4),
}
MAJOR_SCALE = (0, 2, 4, 5, 7, 9, 11)
HARMONIC_MINOR = (0, 2, 3, 5, 7, 8, 11)
NATURAL_MINOR = (0, 2, 3, 5, 7, 8, 10)
PROGRESSIONS = {  # chord roots (semitones above the tonic) and qualities
    "major": ([(0, "maj"), (7, "maj"), (9, "min"), (5, "maj")], MAJOR_SCALE),
    "minor": ([(0, "min"), (8, "maj"), (5, "min"), (7, "maj")], HARMONIC_MINOR),
    "aeolian": ([(0, "min"), (8, "maj"), (3, "maj"), (10, "maj")], NATURAL_MINOR),
}
# Chord pools for the random progressions: (degree, quality, weight), then (degree, quality) -> variants with
# their chance; "/3" and "/5" put the third or the fifth in the bass.
CHORD_POOL = {
    "major": [(0, "maj", 3), (2, "min", 1.5), (4, "min", 1), (5, "maj", 2.5), (7, "maj", 2.5), (9, "min", 2),
              (10, "maj", 0.4), (5, "min", 0.3)],
    "minor": [(0, "min", 3), (3, "maj", 2), (5, "min", 2), (7, "min", 1), (7, "maj", 1.5), (8, "maj", 2),
              (10, "maj", 2), (2, "dim", 0.4)],
}
VARIANTS = {
    (7, "maj"): [("7", None, 0.3), ("maj", 3, 0.15), ("sus4", None, 0.1)],
    (0, "maj"): [("maj", 3, 0.1), ("sus2", None, 0.08), ("maj7", None, 0.08), ("sus4", None, 0.07)],
    (5, "maj"): [("maj7", None, 0.15), ("maj", 5, 0.08)],
    (2, "min"): [("min7", None, 0.3)], (9, "min"): [("min7", None, 0.15)],
    (0, "min"): [("min7", None, 0.15), ("min", 5, 0.08)], (5, "min"): [("min7", None, 0.2)],
    (8, "maj"): [("maj7", None, 0.15)],
}
HARTE = {"maj": "maj", "min": "min", "7": "7", "maj7": "maj7", "min7": "min7", "sus4": "sus4", "sus2": "sus2",
         "dim": "dim", "5": "5"}


def rng_for(name):
    return np.random.default_rng(zlib.crc32(name.encode("utf-8")))


# ---------- synthesis ----------

def tone(midi, seconds, harmonics=6, decay=1.5, amp=0.2, cents=0.0, vibrato=0.0):
    """A note with harmonics 1/k, a 10 ms attack and an exponential decay; vibrato in cents at 5.5 Hz."""
    t = np.arange(int(seconds * SR)) / SR
    f0 = 440.0 * 2 ** ((midi - 69 + cents / 100) / 12)
    phase = 2 * np.pi * f0 * np.cumsum(2 ** (vibrato * np.sin(2 * np.pi * 5.5 * t) / 1200)) / SR
    y = sum(np.sin(k * phase) / k for k in range(1, harmonics + 1) if f0 * k < SR / 2)
    attack = np.minimum(t / 0.01, 1)
    release = np.minimum((seconds - t) / 0.02, 1)
    return amp * y * attack * release * np.exp(-decay * t)


def drum_hit(kind, rng):
    t = np.arange(int(0.4 * SR)) / SR
    if kind == "kick":
        phase = 2 * np.pi * np.cumsum(45 + 80 * np.exp(-t / 0.03)) / SR
        return 0.9 * np.sin(phase) * np.exp(-t / 0.12)
    if kind == "snare":
        return (0.5 * rng.standard_normal(len(t)) + 0.3 * np.sin(2 * np.pi * 185 * t)) * np.exp(-t / 0.06)
    return 0.25 * np.diff(rng.standard_normal(len(t) + 1)) * np.exp(-t / 0.015)  # hi-hat


def add(track, sound, at):
    i = int(at * SR)
    n = min(len(sound), len(track) - i)
    if n > 0 and i >= 0:
        track[i:i + n] += sound[:n]


def beat_grid(sections, rng, jitter_s=0.003):
    """Beat times for a list of (bpm, seconds) sections, with a little human timing jitter."""
    times, t = [], 0.5
    for bpm, seconds in sections:
        end = t + seconds
        while t < end - 1e-9:
            times.append(t)
            t += 60.0 / bpm
    return np.array(times) + rng.normal(0, jitter_s, len(times))


def drums_for(beats, per_bar, rng):
    length = beats[-1] + 2.0
    drums = np.zeros(int(length * SR))
    for i, t in enumerate(beats):
        pos = i % per_bar
        nxt = beats[i + 1] if i + 1 < len(beats) else t + (t - beats[i - 1])
        add(drums, drum_hit("kick" if pos == 0 or (per_bar == 4 and pos == 2) else "snare", rng), t)
        add(drums, drum_hit("hat", rng), t)
        add(drums, drum_hit("hat", rng), (t + nxt) / 2)
    return drums


def write_stems(folder, stems):
    n = max(len(v) for v in stems.values())
    for name, y in stems.items():
        y = np.pad(y, (0, n - len(y)))
        save(folder / f"{name}.flac", np.stack([y, y]) * 0.5)


def make_tempo_songs():
    for name, (sections, per_bar) in TEMPO_SONGS.items():
        rng = rng_for(name)
        beats = beat_grid(sections, rng)
        drums = drums_for(beats, per_bar, rng)
        bass = np.zeros_like(drums)
        for i, t in enumerate(beats):
            if i % per_bar == 0:
                add(bass, tone(40 + (5 if (i // per_bar) % 2 else 0), 1.0, decay=3, amp=0.5), t)
        write_stems(TEST_ROOT / "tempo" / name, {"drums": drums, "bass": bass})


def make_key_song(folder, tonic, kind, bpm=100, loops=4):
    rng = rng_for(folder.name)
    chords, scale = PROGRESSIONS[kind]
    beat = 60.0 / bpm
    bars = [c for _ in range(loops) for c in chords] + [chords[0]]  # end on the tonic chord
    beats = np.arange(len(bars) * 4) * beat
    length = len(bars) * 4 * beat + 2.0
    piano, bass, vocals = (np.zeros(int(length * SR)) for _ in range(3))
    for b, (root, quality) in enumerate(bars):
        start = b * 4 * beat
        triad = analyze.CHORD_TYPES[quality][0]
        for iv in triad:  # chord voiced between MIDI 55 and 66
            note = 55 + (tonic + root + iv - 55) % 12
            add(piano, tone(note, 4 * beat, decay=0.6, amp=0.12), start)
        bass_note = 36 + (tonic + root) % 12
        add(bass, tone(bass_note, 2 * beat, decay=2.0, amp=0.35), start)
        add(bass, tone(bass_note + (7 if b % 2 else 0), 2 * beat, decay=2.0, amp=0.3), start + 2 * beat)
        for k in range(4):  # melody: root on beat 1, third on beat 3, the next scale step after each
            pc = (root + triad[k // 2]) % 12
            if k % 2:
                pc = scale[(scale.index(pc) + 1) % 7]
            add(vocals, tone(67 + (tonic + pc - 67) % 12, beat, harmonics=3, decay=1.0, amp=0.15),
                start + k * beat)
    drums = drums_for(beats, 4, rng)
    write_stems(folder, {"piano": piano, "bass": bass, "vocals": vocals, "drums": drums[:len(piano)]})


def make_key_songs():
    for kind in PROGRESSIONS:
        for tonic in range(12):
            make_key_song(TEST_ROOT / "key" / f"{kind}_{analyze.NAMES[tonic]}", tonic, kind)


def random_progression(rng, mode, n_beats=64):
    """[(degree, quality, bass note as an interval above the root or None, beats)], from and to the tonic chord."""
    pool = CHORD_POOL[mode]
    weights = np.array([w for *_, w in pool]) / sum(w for *_, w in pool)
    out, used = [], 0
    while used < n_beats:
        beats = int(rng.choice([2, 4, 4, 4, 8])) if used % 4 == 0 else 2
        beats = min(beats, n_beats - used)
        degree, quality, _ = pool[0] if used == 0 or used + beats == n_beats else pool[rng.choice(len(pool), p=weights)]
        bass = None
        for q, inversion, chance in VARIANTS.get((degree, quality), []):
            if rng.random() < chance:
                tones = analyze.CHORD_TYPES[quality][0]
                quality, bass = q, (None if inversion is None else tones[1 if inversion == 3 else 2])
                break
        if out and out[-1][:3] == (degree, quality, bass):
            out[-1] = (degree, quality, bass, out[-1][3] + beats)
        else:
            out.append((degree, quality, bass, beats))
        used += beats
    return out


def guitar_voicing(root, quality, low_pc):
    """A guitar-like spread voicing: the bass note between E2 and D#3, then chord tones a third or more apart."""
    pcs = {(root + i) % 12 for i in analyze.CHORD_TYPES[quality][0]}
    notes = [40 + (low_pc - 4) % 12]
    for midi in range(notes[0] + 3, 77):
        if midi % 12 in pcs and midi - notes[-1] >= 3 and len(notes) < 6:
            notes.append(midi)
    for pc in pcs - {n % 12 for n in notes}:
        notes.append(60 + (pc - 60) % 12)
    return sorted(notes)


def reverb(y, rng, seconds=1.2, wet=0.25):
    """A plain room: convolve with exponentially decaying noise."""
    from scipy.signal import fftconvolve
    t = np.arange(int(seconds * SR)) / SR
    ir = rng.standard_normal(len(t)) * np.exp(-6.9 * t / seconds)
    return y + wet * fftconvolve(y, ir / np.sqrt(np.sum(ir ** 2)))[:len(y)]


def make_chord_song(folder, seed):
    """One random 16-bar song and its chords. "rock" songs have a distorted guitar and some power chords (X:5,
    no third anywhere), "clean" songs a strummed clean guitar; both get a melody with passing notes and vibrato,
    drums and vocals leaking into the guitar stem (as after separation), a room reverb and a tuning offset."""
    rng = np.random.default_rng(seed)
    tonic, mode = int(rng.integers(12)), ("major", "minor")[int(rng.integers(2))]
    bpm, cents = float(rng.uniform(80, 140)), float(rng.uniform(-30, 30))
    rock = rng.random() < 0.5
    scale = MAJOR_SCALE if mode == "major" else NATURAL_MINOR
    progression = random_progression(rng, mode)
    beat = 60.0 / bpm
    n_beats = sum(p[3] for p in progression)
    beats = beat_grid([(bpm, n_beats * beat)], rng)
    length = beats[-1] + 3.0
    guitar, pad, bass, vocals = (np.zeros(int(length * SR)) for _ in range(4))
    has_pad = rng.random() < 0.6
    truth, b = [], 0
    for degree, quality, bass_iv, n in progression:
        root = (tonic + degree) % 12
        low = (root + (bass_iv or 0)) % 12
        if rock and quality in ("maj", "min") and bass_iv is None and rng.random() < 0.4:
            quality = "5"
        start, end = beats[b], (beats[b + n] if b + n < len(beats) else beats[-1] + beat)
        truth.append([round(start, 3), round(end, 3), f"{analyze.NAMES[root]}:{HARTE[quality]}"
                      + (f"/{ {3: 'b3', 4: '3', 7: '5'}[bass_iv]}" if bass_iv else "")])
        chord_tones = analyze.CHORD_TYPES[quality][0]
        voicing = guitar_voicing(root, quality, low)
        for k in range(n):  # strum on every beat, an upstroke on the off-beat now and then
            t = beats[b + k]
            for strum_t, up in [(t, False)] + ([(t + beat / 2, True)] if rng.random() < 0.5 else []):
                for j, note in enumerate(reversed(voicing) if up else voicing):
                    add(guitar, tone(note, beat * 0.95, harmonics=8, decay=1.0 if rock else 2.5,
                                     amp=0.06 if up else 0.09, cents=cents + rng.normal(0, 4)), strum_t + 0.011 * j)
            bass_note = 28 + (low - 4) % 12
            if k % 2 == 1:
                bass_note += int(rng.choice([0, 7, 12])) if bass_iv is None else 0
            add(bass, tone(bass_note, beat * 0.9, harmonics=5, decay=1.5, amp=0.4, cents=cents), t)
            if rng.random() < 0.85:  # melody: a chord tone on the beat, often a passing scale step after it
                pc = (root + int(rng.choice(chord_tones))) % 12
                note = 64 + (pc - 64) % 12
                held = beat if rng.random() < 0.4 else beat / 2
                add(vocals, tone(note, held, harmonics=4, decay=0.5, amp=0.15, cents=cents, vibrato=30), t)
                if held < beat and rng.random() < 0.6:  # the next note of the scale above
                    up = min((s - (note - tonic)) % 12 or 12 for s in scale)
                    add(vocals, tone(note + up, beat / 2, harmonics=4, decay=0.5, amp=0.12, cents=cents,
                                     vibrato=30), t + beat / 2)
        if has_pad:
            for iv in chord_tones:
                add(pad, tone(55 + (root + iv - 55) % 12, end - start, harmonics=4, decay=0.3, amp=0.05,
                              cents=cents), start)
        b += n
    if rock:  # overdrive: tanh saturation of the whole guitar part, as an amp does
        drive = rng.uniform(3, 8)
        guitar = 0.3 * np.tanh(drive * guitar) / np.tanh(drive * 0.3)
    drums = drums_for(beats, 4, rng)[:len(guitar)]
    drums = np.pad(drums, (0, len(guitar) - len(drums)))
    guitar, pad, vocals = (reverb(y, rng) for y in (guitar, pad, vocals))
    stems = {"guitar": guitar + 0.08 * drums + 0.1 * vocals, "bass": bass, "vocals": vocals, "drums": drums}
    if has_pad:
        stems["piano"] = pad
    write_stems(folder, stems)
    meta = {"key": f"{analyze.NAMES[tonic]} {mode}", "bpm": round(bpm, 2), "cents": round(cents, 1),
            "style": "rock" if rock else "clean", "chords": truth}
    (folder / "truth.json").write_text(json.dumps(meta), encoding="utf-8")


def make_chord_songs(split, count=24):
    for i in range(count):
        make_chord_song(TEST_ROOT / f"chords_{split}" / f"{i:02d}", seed=(1000 if split == "train" else 2000) + i)


# ---------- checks ----------

def tempo_verdict(found, true):
    if found is None:
        return "none"
    for factor, label in ((1, "ok"), (0.5, "half"), (2, "double"), (2 / 3, "2/3"), (1.5, "3/2")):
        if abs(found / (true * factor) - 1) <= 0.01:
            return label
    return "WRONG"


def key_relation(found, true):
    """MIREX categories for estimated vs true key index (0-11 major, 12-23 minor)."""
    if found == true:
        return "exact"
    same_mode = (found >= 12) == (true >= 12)
    if same_mode and (found - true) % 12 in (5, 7):
        return "fifth"
    if found == analyze.relative_key(true):
        return "relative"
    if found % 12 == true % 12:
        return "parallel"
    return "other"


def harte(segment):
    """analyze.py's chord segment as a Harte label for mir_eval ("G/B" -> "G:maj/3")."""
    if segment["quality"] == "N":
        return "N"
    label = f"{segment['root']}:{HARTE[segment['quality']]}"
    if "/" in segment["label"]:
        name = segment["label"].split("/")[1]
        pc = (analyze.NAMES if name in analyze.NAMES else analyze.FLATS).index(name)
        interval = (pc - analyze.NAMES.index(segment["root"])) % 12
        label += "/" + {3: "b3", 4: "3", 7: "5", 10: "b7", 11: "7", 6: "b5", 5: "4", 2: "2"}[interval]
    return label


def chord_scores(truth, segments):
    import mir_eval
    ref_iv = np.array([[a, b] for a, b, _ in truth])
    ref_lab = [lab for *_, lab in truth]
    est_iv = np.array([[s["start"], s["end"]] for s in segments])
    est_lab = [harte(s) for s in segments]
    est_iv, est_lab = mir_eval.util.adjust_intervals(est_iv, est_lab, ref_iv.min(), ref_iv.max(), "N", "N")
    scores = mir_eval.chord.evaluate(ref_iv, ref_lab, est_iv, est_lab)
    return {k: scores[k] for k in ("root", "majmin", "mirex", "thirds", "triads", "sevenths", "tetrads")}


def check_tempo():
    """Constant-tempo songs: BPM within 1 % and the right beats per bar. Tempo changes: the curve starts and
    ends within 3 % of the true tempos, and the song is not called stable."""
    ok = 0
    print("\nTempo (beat_this on the mix):")
    print(f"{'song':24} {'true':>7} {'found':>7} {'verdict':>7} {'bars':>5} {'stable':>6}  seconds")
    for name, (sections, per_bar) in TEMPO_SONGS.items():
        start = time.perf_counter()
        audio, n = analyze.stem_audio(analyze.find_stems(TEST_ROOT / "tempo" / name))
        t, _ = analyze.analyze_tempo(sum(audio.values()), n / analyze.SR)
        took = time.perf_counter() - start
        curve = [b for _, b in t["tempo_curve"]]
        if len(sections) == 1:
            true, verdict = f"{sections[0][0]}", tempo_verdict(t["bpm"], sections[0][0])
            good = verdict == "ok" and t["beats_per_bar"] == per_bar
        else:
            first, last = sections[0][0], sections[-1][0]
            true = f"{first:.0f}-{last:.0f}"
            good = abs(curve[0] / first - 1) <= 0.03 and abs(curve[-1] / last - 1) <= 0.03 and not t["stable"]
            verdict = f"curve {curve[0]:.0f}..{curve[-1]:.0f}"
        ok += good
        print(f"{name:24} {true:>7} {t['bpm']:7.1f} {verdict:>7} {str(t['beats_per_bar']) + '/' + str(per_bar):>5} "
              f"{str(t['stable']):>6}  {took:5.1f}{'' if good else '  FAIL'}")
    return ok, len(TEMPO_SONGS)


def key_songs(tonics=range(12), splits=()):
    """(set name, true key index, folder): the 3 x 12 key songs, then the chord songs of the given splits."""
    for kind in PROGRESSIONS:
        for tonic in tonics:
            yield kind, tonic + (0 if kind == "major" else 12), TEST_ROOT / "key" / f"{kind}_{analyze.NAMES[tonic]}"
    for split in splits:
        for i in range(24):
            folder = TEST_ROOT / f"chords_{split}" / f"{i:02d}"
            tonic, mode = json.loads((folder / "truth.json").read_text(encoding="utf-8"))["key"].split()
            yield f"chords_{split}", analyze.NAMES.index(tonic) + (12 if mode == "minor" else 0), folder


def check_keys(tonics=range(12), splits=(), variants=False):
    """Key results per song set for S-KEY, the configured profiles, their mean (what analyze.py reports) and,
    with variants=True, every profile x bass weight. Returns the exact hits per method."""
    cats = ("exact", "fifth", "relative", "parallel", "other")
    weight = {"exact": 1, "fifth": 0.5, "relative": 0.3, "parallel": 0.2, "other": 0}  # MIREX weighted score
    combos = [(p, w) for p in analyze.PROFILES for w in (0.0, 0.5, 1.0)] if variants else []
    tally, kinds = {}, []
    for kind, true, folder in key_songs(tonics, splits):
        kinds += [kind] if kind not in kinds else []
        audio, n = analyze.stem_audio(analyze.find_stems(folder))
        mix, harmonic, bass = analyze.song_signals(audio, n)
        chroma = analyze.song_chroma(harmonic, bass)
        hp, bp = analyze.mean_profile(chroma["harmonic"]), analyze.mean_profile(chroma["bass"])
        probs = {"S-KEY": analyze.skey_key_probs(mix), "profiles": analyze.profile_key_probs(hp, bp)}
        probs["mean (reported)"] = (probs["S-KEY"] + probs["profiles"]) / 2
        probs.update({f"{p} + bass {w}": analyze.profile_key_probs(hp, bp, p, w) for p, w in combos})
        for m, prob in probs.items():
            rel = key_relation(int(np.argmax(prob)), true)
            tally.setdefault(m, {}).setdefault(kind, dict.fromkeys(cats, 0))[rel] += 1
    print(f"\nKeys (exact / fifth / relative / parallel / other; 'weighted' = MIREX score without the ambiguous "
          f"aeolian set):\n{'method':22} " + "  ".join(f"{k:>14}" for k in kinds) + "  weighted")
    for m, per_kind in tally.items():
        cells = ["/".join(str(per_kind[k][c]) for c in cats) for k in kinds]
        scored = [per_kind[k][c] * weight[c] for k in kinds if k != "aeolian" for c in cats]
        total = sum(sum(per_kind[k].values()) for k in kinds if k != "aeolian")
        print(f"{m:22} " + "  ".join(f"{c:>14}" for c in cells) + f"  {sum(scored) / total:8.3f}")
    return {m: sum(t[k]["exact"] for k in t) for m, t in tally.items()}


def check_contract(result):
    """analysis.json fields and types as the player and transcribe.py expect them; returns a list of problems."""
    problems = []
    t, k, c, p = result["tempo"], result["key"], result["chords"], result["peaks"]
    segs = c["segments"]
    for name, ok in [("version", result["version"] == 2), ("duration", result["duration_s"] > 0),
                     ("bpm", isinstance(t["bpm"], float)), ("beats sorted", np.all(np.diff(t["beats"]) > 0)),
                     ("downbeats are beats", set(t["downbeats"]) <= set(t["beats"])),
                     ("key fields", all(f in k for f in ("tonic", "mode", "label_zh", "label_en", "confidence",
                                                         "alternatives", "method", "by_method"))),
                     ("key confidence", 0 <= k["confidence"] <= 1),
                     ("chords cover the song",
                      segs[0]["start"] == 0 and abs(segs[-1]["end"] - result["duration_s"]) < 0.05),
                     ("chords contiguous", all(a["end"] == b["start"] for a, b in zip(segs, segs[1:]))),
                     ("chord fields", all(set(s) == {"start", "end", "label", "root", "quality"} for s in segs)),
                     ("peaks", p["samples_per_second"] <= 100 and max(p["data"]) <= 1 and min(p["data"]) >= -1
                      and abs(len(p["data"]) / p["samples_per_second"] - result["duration_s"]) < 0.1)]:
        if not ok:
            problems.append(name)
    return problems


def check_chords(split, count=24, verbose=True):
    """mir_eval chord scores (mean over songs) for analyze.py as configured, and the contract of each result."""
    rows, problems = [], set()
    for i in range(count):
        folder = TEST_ROOT / f"chords_{split}" / f"{i:02d}"
        truth = json.loads((folder / "truth.json").read_text(encoding="utf-8"))
        result = analyze.analyze_song(folder, analyze.find_stems(folder))
        problems.update(check_contract(result))
        scores = chord_scores(truth["chords"], result["chords"]["segments"])
        rows.append(scores)
        if verbose:
            est = " ".join(s["label"] for s in result["chords"]["segments"][:12])
            print(f"  {split} {i:02d} {truth['style']:5} {truth['key']:9} found {result['key']['label_en']:9} "
                  + " ".join(f"{k} {v:.2f}" for k, v in scores.items()) + f" | {est}")
    mean = {k: float(np.mean([r[k] for r in rows])) for k in rows[0]}
    print(f"\nChords, {split} split ({count} songs), mean mir_eval scores: "
          + ", ".join(f"{k} {v:.3f}" for k, v in mean.items()))
    if problems:
        print("analysis.json contract problems:", sorted(problems))
    return mean, problems


def tracker_slips():
    """What Beat This! does on a noisy rock song, made up on a 140 BPM grid of 300 beats (5 ms jitter), with
    downbeats on every 4th beat from beat 2: (detected beats, detected downbeats, their confidence, true beats,
    true downbeat indices). Slips: extra beats 0.3 beat late, near-duplicates 75 ms apart (as at 121.5 s in
    Carsick Cars), a stretch counted in half time, one in double time and one in quadruple time, a 12-beat break
    with no beats, missing downbeats, single stray downbeats, and a noise stretch of 5 bars whose only downbeats
    are unsure ones on the wrong beat."""
    rng = rng_for("tracker_slips")
    true = 0.2 + 60 / 140 * np.arange(300) + rng.normal(0, 0.005, 300)
    double = (true[160:176] + true[161:177]) / 2
    quad = np.concatenate([true[k] + (true[k + 1] - true[k]) * np.array([0.25, 0.5, 0.75]) for k in range(236, 252)])
    extra = np.concatenate([true[[30, 77, 250]] + 0.13, true[[50, 283]] + 0.075, true[[150]] - 0.075])
    detected = np.concatenate([true[:100], true[100:140:2], true[140:200], true[212:], double, quad, extra])
    detected = np.sort(detected + rng.normal(0, 0.003, len(detected)))
    down = np.arange(2, 300, 4)
    noisy = range(178, 195, 4)
    said = [true[i] for i in down if i not in (18, 58, 270) and not 200 <= i < 212 and i not in noisy]
    said += [true[i + 1] for i in (42, 150, 230)] + [true[i + 2] for i in (90, 262)]  # sure but stray
    times = np.array(said + [true[i + 1] for i in noisy])
    confidence = np.array([1.0] * len(said) + [0.3] * len(noisy))
    order = np.argsort(times)
    return detected, times[order], confidence[order], true, down


def check_beat_grid():
    """regular_grid on made-up tracker output whose true beats and bars are known. Returns a list of problems."""
    problems = []
    # gaps of 3 and 4 beats and a missing downbeat
    beats = np.arange(0, 20, 0.5)
    kept = np.r_[0:10, 12:30, 33:40]
    filled, bars, _ = analyze.regular_grid(beats[kept], beats[::4][[0, 1, 2, 4, 5, 6, 7, 8, 9]])
    if not (np.allclose(filled, beats) and np.allclose(bars, beats[::4])):
        problems.append("gaps")

    detected, said, confidence, true, down = tracker_slips()
    found, bars, note = analyze.regular_grid(detected, said, confidence)
    off_ms = np.abs(found - true).max() * 1000 if len(found) == len(true) else np.inf
    gaps = np.diff(found)
    dup = int(np.sum(gaps < 0.5 * 60 / 140))
    lengths = np.diff(np.searchsorted(found, bars))
    on_grid = len(found) == len(true) and len(bars) == len(down) and np.allclose(bars, found[down])
    print(f"\nBeat grid, made-up tracker slips: {len(detected)} detected -> {len(found)} beats (true {len(true)}), "
          f"{off_ms:.1f} ms from the true beats at most, {dup} beat intervals under half a beat, bars of "
          f"{dict(zip(*[v.tolist() for v in np.unique(lengths, return_counts=True)]))} beats{note}")
    if len(found) != len(true) or off_ms > 20 or dup:
        problems.append(f"tracker slips: beats ({len(found)} for {len(true)}, {off_ms:.0f} ms off, {dup} duplicates)")
    if not on_grid:  # bar lines on every 4th beat from beat 2 (beats 0-1 are a pickup), and nowhere else
        problems.append(f"tracker slips: bars {[round(float(b), 2) for b in bars[:6]]}... lengths {lengths.tolist()}")

    # a real 2/4 bar: sure downbeats move by two beats for good after beat 40
    beats = 0.5 + 0.5 * np.arange(120)
    down = list(range(0, 41, 4)) + list(range(42, 120, 4))
    _, bars, _ = analyze.regular_grid(beats, beats[down], np.ones(len(down)))
    if not np.allclose(bars, beats[down]):
        problems.append("a 2/4 bar is not followed")

    # a real change from 100 to 150 BPM (not a double-time count) keeps every beat
    beats = np.concatenate([0.3 + 0.6 * np.arange(50), 0.3 + 0.6 * 50 + 0.4 * np.arange(75)])
    found, _, _ = analyze.regular_grid(beats, [])
    if len(found) != len(beats) or not np.allclose(found, beats):
        problems.append(f"100 -> 150 BPM: {len(found)} beats for {len(beats)}")
    print("Beat grid:", "; ".join(problems) or "ok")
    return problems


def check_helpers():
    """peaks and chord spelling on made-up input."""
    problems = []
    sine = np.sin(np.linspace(0, 200 * np.pi, analyze.SR * 2)).astype(np.float32)
    peaks = analyze.peaks(sine * 3)  # too loud: must still land in -1..1
    if len(peaks["data"]) != 2 * peaks["samples_per_second"] or max(peaks["data"]) > 1 or min(peaks["data"]) < -1:
        problems.append("peaks")
    # D/F# in G minor (key 19), F# in G# minor (20), Bb and Ab/C in C major (0)
    spelled = [analyze.chord_label(2, "maj", 6, 19), analyze.chord_label(6, "maj", None, 20),
               analyze.chord_label(10, "maj", None, 0), analyze.chord_label(8, "maj", 0, 0)]
    if spelled != ["D/F#", "F#", "Bb", "Ab/C"]:
        problems.append(f"chord spelling {spelled}")
    return problems


def make_edge_songs():
    """Odd songs cut from key/major_E: 2 s long, silence, drums only, no bass stem, and a bass stem that is only
    faint noise (-70 dBFS, like the bass stem of a song without bass)."""
    stems = {f.stem: load(f) for f in sorted((TEST_ROOT / "key" / "major_E").glob("*.flac"))}
    noise = 3e-4 * rng_for("leaky_bass").standard_normal(stems["bass"].shape)
    songs = {"short_2s": {k: v[:, :2 * SR] for k, v in stems.items()},
             "silent": {k: np.zeros_like(v[:, :10 * SR]) for k, v in stems.items()},
             "drums_only": {"drums": stems["drums"]},
             "no_bass": {k: v for k, v in stems.items() if k != "bass"},
             "leaky_bass": {**stems, "bass": noise}}
    for name, song in songs.items():
        for stem, y in song.items():
            save(TEST_ROOT / "edge" / name / f"{stem}.flac", y)


def check_edges():
    """The odd songs must not crash; silence and drums give no key and only "N"; bass leakage must not change
    the profiles' key or its confidence (without the -40 dB gate, 0.84 became 0.99). Returns a list of problems."""
    problems, results = [], {}
    for name in ("short_2s", "silent", "drums_only", "no_bass", "leaky_bass"):
        folder = TEST_ROOT / "edge" / name
        try:
            results[name] = r = analyze.analyze_song(folder, analyze.find_stems(folder))
        except Exception as e:  # noqa: BLE001 -- a crash is what this check looks for
            problems.append(f"{name} crashed: {type(e).__name__}: {e}")
            continue
        segs = r["chords"]["segments"]
        if segs[0]["start"] != 0 or abs(segs[-1]["end"] - r["duration_s"]) > 0.05:
            problems.append(f"{name}: chords do not cover the song")
        if name in ("silent", "drums_only") and (r["key"] or any(s["label"] != "N" for s in segs)):
            problems.append(f"{name}: a key or chords in a song without harmony")
    if "no_bass" in results and "leaky_bass" in results:  # the noise must leave the profiles' opinion alone
        keys = [(results[n]["key"]["by_method"]["profiles"]["label_en"],
                 results[n]["key"]["by_method"]["profiles"]["confidence"]) for n in ("no_bass", "leaky_bass")]
        if keys[0][0] != "E major" or keys[0] != keys[1]:
            problems.append(f"profiles' key without bass / with bass leakage: {keys} (should be equal, E major)")
    print("\nEdge songs:", "; ".join(problems) or "ok")
    return problems


def ensure_songs(remake):
    if remake or not (TEST_ROOT / "tempo" / "070bpm" / "drums.flac").exists():
        make_tempo_songs()
    if remake or not (TEST_ROOT / "key" / "major_C" / "piano.flac").exists():
        make_key_songs()
    for split in ("train", "test"):
        if remake or not (TEST_ROOT / f"chords_{split}" / "23" / "truth.json").exists():
            make_chord_songs(split)
    if remake or not (TEST_ROOT / "edge" / "leaky_bass" / "bass.flac").exists():
        make_edge_songs()


def quick():
    """A subset with thresholds a little below what the current settings reach, so a regression fails. Only
    train songs: the test songs are scored once, with --full, after the settings are fixed (AGENTS.md)."""
    failures = check_helpers() + check_beat_grid() + check_edges()
    ok, total = check_tempo()
    if ok < total:
        failures.append(f"tempo {ok}/{total}")
    exact = check_keys(tonics=(0, 4, 10))  # 9 songs, 3 of them aeolian (counted as relative major)
    if min(exact["S-KEY"], exact["profiles"], exact["mean (reported)"]) < 6:
        failures.append(f"key {exact}")
    mean, problems = check_chords("train", count=6, verbose=False)
    if mean["majmin"] < 0.85 or mean["root"] < 0.85:
        failures.append(f"chords {mean}")
    failures += [f"contract: {p}" for p in sorted(problems)]
    print("\nFAILED: " + "; ".join(failures) if failures else "\nall quick checks passed")
    return not failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="all songs and the per-setting tables")
    parser.add_argument("--remake", action="store_true", help="synthesize the test songs again")
    args = parser.parse_args()
    ensure_songs(args.remake)
    if not args.full:
        sys.exit(0 if quick() else 1)
    print("helpers:", check_helpers() or "ok")
    check_beat_grid()
    check_edges()
    check_tempo()
    check_keys(splits=("train",), variants=True)  # what the key settings were chosen on
    check_chords("train")                          # what the chord settings were chosen on
    check_keys(tonics=(), splits=("test",))        # scored once, after the settings were fixed
    check_chords("test")


if __name__ == "__main__":
    main()
