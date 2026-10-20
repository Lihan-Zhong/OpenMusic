"""Find the tempo, key and chords of each separated song, to help practise and transcribe it by ear.

    python experiments/analyze.py outputs/mp3
    python experiments/analyze.py outputs/mp3 --song "Carsick Cars - 中南海" --force

Every <stems_root>/<song>/ folder with stems ({vocals,drums,bass,guitar,piano,other} as .flac, .wav or .mp3)
gets <stems_root>/<song>/analysis.json (UTF-8, "version": 2): duration, tempo (BPM, beats, bar starts, beats
per bar, a tempo curve), key (with runners-up and one entry per method), chords (one segment per chord, on
the beat grid) and waveform peaks of the mix for the player. Songs whose analysis.json is version 2 and newer
than their stems are skipped unless --force. One summary line is printed per song.

Tempo: the "Beat This!" beat tracker (CPJKU, MIT licence for code and weights; its 8 MB "small0" checkpoint
  goes to .cache/torch) on the full mix, i.e. the sum of all stems. Its beats are then cleaned up to one beat
  per beat of the song's main pulse (regular_beats): near-duplicates and stretches it counted in double time
  are thinned out, missed beats, half-time stretches and breaks are filled in. Its downbeats then set the bar
  lines, which keep a steady bar length unless several sure downbeats in a row say otherwise (bar_lines). These
  beats and bars are final: transcribe.py writes them into the MIDI as they are, so bar N is the same bar in
  the player and in the MIDI. BPM = 60 / median beat interval; half and double time are listed as
  alternatives. If beat_this is missing or its checkpoint cannot be loaded, librosa's beat tracker is used (no
  downbeats then).
Key: two opinions. (1) S-KEY (Deezer, MIT, vendored in experiments/vendor/skey; needs nnAudio), a small
  self-supervised network, on the full mix. (2) Key profiles: chroma of the harmonic stems (no drums, vocals
  at half weight, as StemDeck does) plus the bass stem on its own, correlated with Shaath's major/minor
  profiles. The reported key averages the two probability lists (KEY_DEFAULT); key.by_method keeps each
  one. experiments/tests/test_analyze.py has the evidence for this choice. Without nnAudio, or for clips under
  SKEY_MIN_S: profiles only. A bass stem 40 dB below the mix (leakage, e.g. in a song without bass) is ignored.
Chords: the classic template + HMM recipe (as in librosa's viterbi_discriminative example), on beat-length
  frames: log-compressed CQT chroma of the harmonic stems, the bass stem for the root, 24 major/minor triads
  + "no chord" scored by cosine similarity, a key-aware prior, and Viterbi smoothing so chords change on
  beats. Each chord is then refined to 7 / maj7 / m7 / sus4 / sus2 / dim / 5 (power chord) when that fits
  clearly better, and written as a slash chord (G/B) when the bass clearly plays another chord tone. These
  extras are the least reliable part: guitar overtones can mimic them. No trained chord model is
  used: the ones that exist are non-commercial (madmom), have no weight licence (BTC) or need GPL Vamp
  binaries (Chordino).
Peaks: min/max pairs of the mix, PEAK_BINS_PER_S pairs per second, for wavesurfer.js's "peaks" option
  ("data" alternates min, max; "samples_per_second" counts entries of "data", i.e. 2 per pair).

Borrowed with thanks: the profile tables come from Essentia's src/algorithms/tonal/key.cpp (AGPL-3.0) and,
for Albrecht-Shanahan, StemDeck's app/pipeline/analyze.py (Apache-2.0, which also gave the stem weights);
the S-KEY wrapper follows deezer/skey's key_detection.py (MIT).
"""
import argparse
import functools
import json
import os
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import librosa
import numpy as np

from common import load

SR = 22050                     # analysis rate; beat_this and S-KEY both work at 22.05 kHz
HOP = 512                      # chroma frames of 23 ms
STEM_NAMES = ("vocals", "drums", "bass", "guitar", "piano", "other")
EXT_PREFERENCE = (".flac", ".wav", ".mp3")
ACCOMP_STEMS = ("guitar", "piano", "other")  # the harmonic stems besides vocals
VOCAL_WEIGHT = 0.5             # vocals count half in the harmonic chroma (StemDeck's stem weights)
VERSION = 2
VENDOR = Path(__file__).resolve().parent / "vendor"

BEAT_MODEL = "small0"           # 8 MB; on the user's songs it skipped fewer beats than final0 (80 MB), at half
                               # the time and memory; the paper's GTZAN beat F1 is 88.8 vs 89.1
# Beat clean-up (regular_beats): the cheapest chain through the detected beats, where each step of k beats costs
# k * (log(step / (k * local period)))^2 / (2 * BEAT_SPREAD^2) + FILL_COST * (k - 1), and each detected beat left
# out SKIP_COST. So a beat 10 % early costs 0.5 on each side, and one about 13 % off is replaced by a filled beat.
# A step longer than its k beats costs at most STEP_CAP: where the beat phase jumps (the grid moves by part of a
# beat, e.g. where the drums come in), one beat is made longer, and the detected beats around it are kept.
BEAT_SPREAD = 0.1
STEP_CAP = 4.0
SKIP_COST = 1.0                # an extra beat, a near-duplicate, or the off-beat of a stretch counted in double time
FILL_COST = 0.8                # a beat the tracker missed, or the in-between beat of a stretch counted in half time;
                               # a little below SKIP_COST, so a first or last beat that fits the grid is kept
MAX_FILL = 8                   # steps of up to 8 beats are weighed; a longer break gets evenly spaced beats
LOCAL_S = 10.0                 # local beat period: median of the beat intervals within +-10 s ...
OCTAVE_TOLERANCE = 0.2         # ... after scaling intervals within 2^0.2 (15 %) of 1/4, 1/2 or k song beats to one
DOWNBEAT_TOLERANCE = 0.3       # a detected downbeat marks the beat it is within 0.3 beat periods of
# Bar lines (bar_lines): the cheapest count through the beats; costs per detected downbeat off a bar line and per
# bar line without one (both scaled by the tracker's confidence), and per odd bar. One stray downbeat is ignored;
# two sure ones in a row that agree on a new place move the bar lines for good, unsure ones (as in a noise
# passage) need three or more; a stretch that moves and later moves back needs about twice as many.
BAR_MISS, BAR_EMPTY, BAR_SHIFT = 1.0, 0.5, 2.5
CURVE_STEP_S = 5.0             # one tempo-curve point every 5 s, from beats within +-5 s
STABLE_TOLERANCE = 0.04        # stable: >= 90 % of local tempos within +-4 % of the song's BPM

NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
FLATS = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
# How musicians usually spell each key (major / minor); the "tonic" and "root" fields keep the sharp names.
SPELL_MAJOR = ("C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
SPELL_MINOR = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "G#", "A", "Bb", "B")
FLAT_KEYS = {(5, "major"), (10, "major"), (3, "major"), (8, "major"), (1, "major"),
             (2, "minor"), (7, "minor"), (0, "minor"), (5, "minor"), (10, "minor"), (3, "minor")}

# Key profiles: how strongly each scale degree (from the tonic up, in semitones) is heard in a key, major
# row then minor row. Krumhansl, Temperley (MIREX 2005), Shaath and edma are copied from Essentia's key.cpp;
# Albrecht & Shanahan (2013, a pop corpus) from StemDeck, rescaled there (correlation ignores the scale).
PROFILES = {
    "krumhansl": ([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
                  [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]),
    "temperley2005": ([0.748, 0.060, 0.488, 0.082, 0.670, 0.460, 0.096, 0.715, 0.104, 0.366, 0.057, 0.400],
                      [0.712, 0.084, 0.474, 0.618, 0.049, 0.460, 0.105, 0.747, 0.404, 0.067, 0.133, 0.330]),
    "shaath": ([6.6, 2.0, 3.5, 2.3, 4.6, 4.0, 2.5, 5.2, 2.4, 3.7, 2.3, 3.4],
               [6.5, 2.7, 3.5, 5.4, 2.6, 3.5, 2.5, 5.2, 4.0, 2.7, 4.3, 3.2]),
    "edma": ([1.00, 0.29, 0.50, 0.40, 0.60, 0.56, 0.32, 0.80, 0.31, 0.45, 0.42, 0.39],
             [1.00, 0.31, 0.44, 0.58, 0.33, 0.49, 0.29, 0.78, 0.43, 0.29, 0.53, 0.32]),
    "albrecht": ([5.47, 0.14, 2.55, 0.14, 3.15, 2.16, 0.37, 4.92, 0.21, 1.84, 0.18, 1.86],
                 [5.06, 0.14, 2.42, 2.42, 0.35, 1.96, 0.35, 4.16, 2.53, 0.28, 2.67, 0.62]),
}
KEY_PROFILE = "shaath"         # with BASS_WEIGHT 1.0: among the best on the synthetic train songs, see the test
BASS_WEIGHT = 1.0              # score = match(harmonic chroma) + BASS_WEIGHT * match(bass chroma)
KEY_TEMPERATURE = 0.05         # profile score gap -> probability (a gap of 0.05 is a factor e); at 0.05 the mean
                               # confidence on the synthetic train songs (0.92) matches the accuracy (0.92)
KEY_DEFAULT = "ensemble"       # "ensemble" (mean of both opinions' probabilities), "skey" or "profiles"
SKEY_CHUNK_S = 30              # S-KEY's front end runs on 30 s of audio at a time to stay under ~1 GB
SKEY_MIN_S = 4                 # S-KEY's network halves the time axis 7 times and fails on clips under ~3 s
STEM_MIN_DB = -40.0            # a bass stem (or all harmonic stems together) this far below the mix in RMS is
                               # separation leakage, e.g. the bass stem of a song without bass: left out

# Chords: qualities as intervals above the root, and the label suffix musicians write.
CHORD_TYPES = {"maj": ((0, 4, 7), ""), "min": ((0, 3, 7), "m"), "7": ((0, 4, 7, 10), "7"),
               "maj7": ((0, 4, 7, 11), "maj7"), "min7": ((0, 3, 7, 10), "m7"), "sus4": ((0, 5, 7), "sus4"),
               "sus2": ((0, 2, 7), "sus2"), "dim": ((0, 3, 6), "dim"), "5": ((0, 7), "5")}
REFINE = {"maj": ("7", "maj7", "sus4", "sus2", "5"),  # what each triad may become; "5" = power chord, no third
          "min": ("min7", "dim", "sus4", "sus2", "5")}
CHORD_LOG_GAIN = 10.0          # log(1 + g * CQT) before folding to chroma: quiet chord tones still count
TEMPLATE_HARMONICS = 6         # when choosing 7th / sus / dim / 5, templates include each note's first 6 partials,
REFINE_DECAY = 0.6             # the k-th one weighted 0.6 ** (k - 1) (as in Gomez 2006); plain triads score
                               # better without them
CHORD_BASS_WEIGHT = 0.3        # bass on the root adds this much to a chord's cosine score (0.6x on other tones)
CHORD_N_SCORE = 0.55           # "no chord" wins when no triad matches better than this
CHORD_SILENCE_DB = -35.0       # beats this far below the song's loud beats are "no chord"
CHORD_TEMPERATURE = 0.05       # score -> probability, as KEY_TEMPERATURE
CHORD_SELF = 0.8               # chance that the chord on one beat carries on to the next
KEY_PRIOR = {"diatonic": 0.0, "borrowed": -0.05, "other": -0.12}  # added to the score of each triad
EXTENSION_MARGIN = 0.08        # a 7th / sus / dim / 5 must beat the plain triad's cosine by this much. Cautious:
                               # 0.04 scores better on the synthetic songs, but on real guitars the overtones
                               # alone then turn many plain chords into maj7 or 5 chords
SLASH_RATIO = 1.5              # slash chord when the bass note is this much stronger than the root
PEAK_BINS_PER_S = 50


def find_stems(song_dir):
    """{stem name: path}, taking .flac over .wav over .mp3 when a stem exists in several formats."""
    found = {}
    for ext in reversed(EXT_PREFERENCE):
        for name in STEM_NAMES:
            if (song_dir / f"{name}{ext}").is_file():
                found[name] = song_dir / f"{name}{ext}"
    return found


def load_mono(path):
    return load(path, sr=SR).mean(axis=0)


def package_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return "?"


# ---------- tempo ----------

@functools.cache
def beat_model():
    """The Beat This! model (downloaded once into .cache/torch), or None if it is not installed or not loadable."""
    try:
        from beat_this.inference import Audio2Frames
        return Audio2Frames(BEAT_MODEL, device="cpu")
    except ImportError:
        return None
    except Exception as e:  # e.g. offline on the first run: the checkpoint cannot be downloaded
        print(f"beat_this could not load its {BEAT_MODEL} checkpoint ({e}); using librosa instead", file=sys.stderr)
        return None


def track_beats(y):
    """Beat and downbeat times in seconds, the tracker's confidence in each downbeat (0-1: its probability, from
    0.5 at its detection threshold up to 1, rescaled) and a short method note. Beat This! if installed, else
    librosa."""
    model = beat_model()
    if model is None:
        return track_beats_librosa(y)
    from beat_this.model.postprocessor import Postprocessor
    beat_logits, downbeat_logits = model(y, SR)
    beats, downbeats = Postprocessor("minimal")(beat_logits, downbeat_logits)
    beats = refine_peaks(beats, beat_logits.numpy(), fps=50)
    prob = 1 / (1 + np.exp(-downbeat_logits.numpy()))
    frames = np.clip(np.round(np.asarray(downbeats) * 50).astype(int), 0, len(prob) - 1)
    confidence = np.clip(2 * prob[frames] - 1, 0, 1)
    return beats, np.asarray(downbeats), confidence, \
        f"beat_this {package_version('beat-this')} {BEAT_MODEL} on the full mix"


def refine_peaks(times, logits, fps):
    """Move each peak between frames with a parabola through its 3 frames (frames are 20 ms apart)."""
    out = []
    for t in times:
        f = int(round(t * fps))
        if 0 < f < len(logits) - 1:
            a, b, c = logits[f - 1], logits[f], logits[f + 1]
            denom = a - 2 * b + c
            offset = 0.5 * (a - c) / denom if denom < 0 else 0.0
            t = (f + float(np.clip(offset, -0.5, 0.5))) / fps
        out.append(t)
    return np.asarray(out)


def track_beats_librosa(y):
    _, frames = librosa.beat.beat_track(y=y, sr=SR, hop_length=512)
    return librosa.frames_to_time(frames, sr=SR, hop_length=512), np.array([]), np.array([]), \
        f"librosa {librosa.__version__} beat_track on the full mix (no downbeats)"


def local_periods(beats, span=8):
    """Beat period from a straight-line fit over each run of span+1 beats, and the run's centre time.

    A fit over 8 intervals averages out the 20 ms frame grid. Runs whose intervals differ by more than 30 %
    (a missed or an extra beat, or a break) are left out, so they cannot put nonsense in the tempo curve.
    """
    if len(beats) < span + 1:
        span = len(beats) - 1
    if span < 2:
        return np.array([]), np.array([])
    runs = np.lib.stride_tricks.sliding_window_view(beats, span + 1)
    ibi = np.diff(runs, axis=1)
    regular = ibi.max(axis=1) <= 1.3 * ibi.min(axis=1)
    if regular.any():
        runs = runs[regular]
    idx = np.arange(span + 1) - span / 2
    periods = (runs - runs.mean(axis=1, keepdims=True)) @ idx / np.sum(idx ** 2)
    return periods, runs.mean(axis=1)


def tempo_summary(beats, duration):
    """Song BPM, a coarse tempo curve and whether the tempo holds steady."""
    periods, centres = local_periods(beats)
    if len(periods) == 0:
        return None, [], False
    bpm = 60.0 / np.median(periods)
    curve = []
    for t in np.arange(CURVE_STEP_S / 2, duration, CURVE_STEP_S):
        near = periods[np.abs(centres - t) <= CURVE_STEP_S]
        if len(near):
            curve.append([round(float(t), 1), round(60.0 / float(np.median(near)), 1)])
    within = np.abs(60.0 / periods / bpm - 1) <= STABLE_TOLERANCE
    return float(bpm), curve, bool(np.mean(within) >= 0.9)


def beats_per_bar(beats, downbeats):
    """The most common number of beats between consecutive downbeats (None if unclear)."""
    if len(downbeats) < 3:
        return None
    counts = np.diff(np.searchsorted(beats, downbeats))
    values, freq = np.unique(counts[counts > 0], return_counts=True)
    if len(values) == 0:
        return None
    best = int(values[np.argmax(freq)])
    return best if freq.max() >= 0.5 * len(counts) and 2 <= best <= 12 else None


def bpm_alternatives(bpm):
    """Half and double time, the usual ways two musicians count the same song differently."""
    return [round(b, 1) for b in (bpm / 2, bpm * 2) if 40 <= b <= 250]


def weighted_median(values, weights):
    order = np.argsort(values)
    cum = np.cumsum(weights[order])
    return float(values[order][np.searchsorted(cum, cum[-1] / 2)])


def pulse_period(beats, times):
    """Beat period at the given times, counted at the song's main pulse.

    The song's pulse is the beat interval that covers the most time (a median weighted by duration, so a stretch
    the tracker counted in double time cannot outvote a longer one counted normally). Intervals within
    OCTAVE_TOLERANCE of 1/4 or 1/2 of it (double time), or of k = 2..MAX_FILL times it (missed beats, half time),
    count as one beat; longer breaks are left out. Each time then gets the duration-weighted median of the
    intervals within LOCAL_S: a change of tempo is followed, a switch to double or half time is not.
    """
    ibi = np.diff(beats)
    song = weighted_median(ibi, ibi)
    scaled, weight = ibi.copy(), ibi.copy()
    for n in (1 / 4, 1 / 2) + tuple(range(2, MAX_FILL + 1)):
        near = np.abs(np.log2(ibi / (n * song))) <= OCTAVE_TOLERANCE
        scaled[near] = ibi[near] / n
    weight[ibi > (MAX_FILL + 0.5) * song] = 0
    centres = (beats[:-1] + beats[1:]) / 2
    out = np.full(len(times), song)
    for k, t in enumerate(times):
        near = (np.abs(centres - t) <= LOCAL_S) & (weight > 0)
        if near.any():
            out[k] = weighted_median(scaled[near], weight[near])
    return out


def regular_beats(beats):
    """The detected beats, cleaned up to one beat per beat of the song's main pulse.

    Dynamic programming finds the cheapest chain through the detected beats (costs: see BEAT_SPREAD), each step
    measured against the pulse_period at its middle: it leaves out near-duplicates and extra beats, and the
    off-beats of a stretch the tracker counted in double (or quadruple) time, keeping the ones in step with the
    beats around the stretch; it fills in missed beats and the half-time stretches with evenly spaced beats. A
    break longer than MAX_FILL beats gets evenly spaced beats at the local period, so the bar count carries on
    through it. Returns the beats and how many were left out and filled in.
    """
    n = len(beats)
    if n < 8:
        return beats, 0, 0
    step = 0.1  # the pulse period is looked up on a 0.1 s grid
    grid = pulse_period(beats, np.arange(0, beats[-1] + step, step))

    def period(t):
        return grid[np.clip(np.round(np.asarray(t) / step).astype(int), 0, len(grid) - 1)]

    cost = SKIP_COST * np.arange(n, dtype=float)  # starting the chain at beat j leaves out the j before it
    prev = np.full(n, -1)
    for j in range(1, n):
        reach = (MAX_FILL + 0.5) * period(beats[j])
        if beats[j] - beats[j - 1] > reach:  # a break: carry on from the last beat before it (or a duplicate of it)
            i = np.arange(np.searchsorted(beats, beats[j - 1] - 0.5 * period(beats[j - 1])), j)
            c = cost[i] + SKIP_COST * (j - 1 - i)
        else:
            i = np.arange(np.searchsorted(beats, beats[j] - reach), j)
            span, p = beats[j] - beats[i], period((beats[i] + beats[j]) / 2)
            k = np.maximum(np.floor(span / p + 0.5), 1)
            dev = np.log(span / (k * p))
            fit = k * dev ** 2 / (2 * BEAT_SPREAD ** 2)
            c = cost[i] + np.where(dev > 0, np.minimum(fit, STEP_CAP), fit) + FILL_COST * (k - 1) \
                + SKIP_COST * (j - 1 - i)
        best = int(np.argmin(c))
        if c[best] < cost[j]:
            cost[j], prev[j] = c[best], i[best]
    chain = [int(np.argmin(cost + SKIP_COST * (n - 1 - np.arange(n))))]  # ending early leaves out the rest
    while prev[chain[-1]] >= 0:
        chain.append(int(prev[chain[-1]]))
    chain = chain[::-1]
    out = [beats[chain[:1]]]
    for a, b in zip(chain, chain[1:]):
        k = max(1, int(np.floor((beats[b] - beats[a]) / period((beats[a] + beats[b]) / 2) + 0.5)))
        out.append(np.linspace(beats[a], beats[b], k + 1)[1:])
    out = np.concatenate(out)
    return out, n - len(chain), len(out) - len(chain)


def bar_lines(beats, downbeats, confidence=None):
    """Bar starts: the detected downbeats, made into bars of a steady length where the music allows.

    Each detected downbeat marks the beat within DOWNBEAT_TOLERANCE of it with its confidence w (0-1; 1 if not
    given). With a clear meter (beats_per_bar), a Viterbi pass over the position in the bar counts through the
    beats: it pays BAR_MISS * w for a marked beat off a bar line, BAR_EMPTY * (1 - w) for a bar line and BAR_SHIFT
    for an odd bar. So stray or missing downbeats leave the bars alone, a lasting change (a 2/4 bar in a 4/4 song)
    moves them, and unsure downbeats (in a noise passage) need more bars in a row to do so. Bar lines also carry
    on before the first and after the last detected downbeat, so only the first bar can be short (a pickup).
    Without a clear meter the marked beats are returned as they are.
    """
    if len(downbeats) == 0 or len(beats) < 2:
        return np.array([])
    idx = np.clip(np.searchsorted(beats, downbeats), 1, len(beats) - 1)
    idx = np.where(np.abs(beats[idx - 1] - downbeats) < np.abs(beats[idx] - downbeats), idx - 1, idx)
    near = np.abs(beats[idx] - downbeats) <= DOWNBEAT_TOLERANCE * float(np.median(np.diff(beats)))
    weight = np.zeros(len(beats))
    np.maximum.at(weight, idx[near], np.ones(len(downbeats))[near] if confidence is None else confidence[near])
    per_bar = beats_per_bar(beats, beats[weight > 0])
    if per_bar is None:
        return beats[weight > 0]
    pos = np.arange(per_bar)
    total = np.zeros(per_bar)
    back = np.zeros((len(beats), per_bar), int)
    for m in range(len(beats)):
        if m:
            steady, shifted = np.roll(total, 1), total.min() + BAR_SHIFT  # roll: position q - 1 -> q, last -> 0
            back[m] = np.where(steady <= shifted, (pos - 1) % per_bar, int(np.argmin(total)))
            total = np.minimum(steady, shifted)
        total = total + np.where(pos == 0, BAR_EMPTY * (1 - weight[m]), BAR_MISS * weight[m])
    path = [int(np.argmin(total))]
    for m in range(len(beats) - 1, 0, -1):
        path.append(int(back[m][path[-1]]))
    return beats[np.array(path[::-1]) == 0]


def regular_grid(beats, downbeats, confidence=None):
    """regular_beats, then bar_lines; and a note for the tempo method saying what changed."""
    beats = np.unique(np.asarray(beats, float))
    found = len(beats)
    beats, dropped, filled = regular_beats(beats)
    downbeats = bar_lines(beats, np.asarray(downbeats, float), confidence)
    note = f", {dropped} of {found} detected beats left out and {filled} filled in" if dropped or filled else ""
    per_bar = beats_per_bar(beats, downbeats)
    if per_bar:
        lengths = np.diff(np.searchsorted(beats, downbeats))
        note += f"; bar lines from its downbeats, {int(np.sum(lengths != per_bar))} bars not {per_bar} beats long"
    return beats, downbeats, note


def analyze_tempo(mix, duration):
    beats, downbeats, confidence, method = track_beats(mix)
    beats, downbeats, note = regular_grid(beats, downbeats, confidence)
    method += note
    bpm, curve, stable = tempo_summary(beats, duration)
    tempo = {"bpm": round(bpm, 1) if bpm else None, "bpm_alternatives": bpm_alternatives(bpm) if bpm else [],
             "stable": stable, "tempo_curve": curve, "beats": [round(float(t), 3) for t in beats],
             "downbeats": [round(float(t), 3) for t in downbeats],
             "beats_per_bar": beats_per_bar(beats, downbeats), "method": method}
    return tempo, beats


# ---------- chroma ----------

def cqt(y, fmin, n_octaves, tuning):
    """CQT magnitude, 36 bins per octave from fmin (Hz) up."""
    return np.abs(librosa.cqt(y, sr=SR, hop_length=HOP, fmin=fmin, n_bins=36 * n_octaves,
                              bins_per_octave=36, tuning=tuning))


def fold(C, fmin):
    """Pitch-class energy per frame (12 x frames) from a 36-bin-per-octave CQT."""
    return librosa.feature.chroma_cqt(C=C, sr=SR, fmin=fmin, bins_per_octave=36, norm=None)


def estimate_tuning(y):
    """Deviation from A440 in bins (of 36 per octave), from three 20 s excerpts (a whole song takes ~0.6 GB)."""
    if len(y) > 60 * SR:
        y = np.concatenate([y[int(f * len(y)) - 10 * SR:int(f * len(y)) + 10 * SR] for f in (0.25, 0.5, 0.75)])
    return librosa.estimate_tuning(y=y, sr=SR, bins_per_octave=36)


def song_chroma(harmonic, bass):
    """Chroma (12 x frames) of the harmonic stems, linear and log-compressed ("harmonic_log"), and of the bass
    stem ("bass", None without one)."""
    tuning = estimate_tuning(harmonic) if np.any(harmonic) else 0.0
    fmin_h, fmin_b = librosa.note_to_hz("C2"), librosa.note_to_hz("C1")
    C = cqt(harmonic, fmin_h, 6, tuning)
    out = {"tuning": float(tuning), "harmonic": fold(C, fmin_h),
           "harmonic_log": fold(np.log1p(CHORD_LOG_GAIN * C / (C.max() + 1e-12)), fmin_h)}
    out["bass"] = fold(cqt(bass, fmin_b, 3, tuning), fmin_b) if bass is not None and np.any(bass) else None
    return out


def mean_profile(chroma):
    """Average pitch-class profile (12 values, sums to 1) over the frames that are not near silence."""
    energy = chroma.sum(axis=0)
    keep = energy > 1e-3 * energy.max() if energy.max() > 0 else np.ones_like(energy, bool)
    frames = chroma[:, keep] / chroma[:, keep].max(axis=0, keepdims=True)
    profile = frames.mean(axis=1)
    return profile / profile.sum()


# ---------- key ----------

def key_scores(chroma, profile_name=KEY_PROFILE):
    """Correlation of the chroma with each of the 24 keys: index 0-11 = C..B major, 12-23 = C..B minor."""
    scores = []
    for mode_profile in PROFILES[profile_name]:
        for tonic in range(12):
            scores.append(np.corrcoef(chroma, np.roll(mode_profile, tonic))[0, 1])
    return np.array(scores)


def profile_key_probs(harmonic_profile, bass_profile=None, profile_name=KEY_PROFILE, bass_weight=BASS_WEIGHT):
    scores = key_scores(harmonic_profile, profile_name)
    if bass_profile is not None:
        scores = scores + bass_weight * key_scores(bass_profile, profile_name)
    prob = np.exp((scores - scores.max()) / KEY_TEMPERATURE)
    return prob / prob.sum()


@functools.cache
def skey_model():
    """S-KEY's VQT front end and ChromaNet (vendored), or None if nnAudio or the vendored files are missing."""
    try:
        import torch
        sys.path.insert(0, str(VENDOR))
        from skey.chromanet import ChromaNet
        from skey.hcqt import VQT, CropCQT
    except ImportError:
        return None
    finally:
        if str(VENDOR) in sys.path:
            sys.path.remove(str(VENDOR))
    try:
        # The checkpoint also holds optimizer state with numpy scalars; allow just those, never full unpickling.
        core = getattr(np, "_core", None) or np.core
        allowed = [(core.multiarray.scalar, "numpy.core.multiarray.scalar"), np.dtype, type(np.dtype("float64"))]
        with torch.serialization.safe_globals(allowed):
            ckpt = torch.load(VENDOR / "skey" / "skey.pt", map_location="cpu", weights_only=True)
        # same settings as load_model_components() in deezer/skey's key_detection.py
        hcqt = VQT(harmonics=[1], fmin=27.5, n_bins=99, verbose=False)
        net = ChromaNet(n_bins=84, n_harmonics=1, out_channels=[2, 3, 40, 40, 30, 10, 3],
                        kernels=[7, 7, 7, 7, 7, 5, 5], temperature=1)
        hcqt.load_state_dict({k.replace("hcqt.", ""): v for k, v in ckpt["stone"].items() if "hcqt" in k})
        net.load_state_dict({k.replace("chromanet.", ""): v for k, v in ckpt["stone"].items() if "chromanet" in k})
    except Exception as e:  # e.g. a missing or damaged skey.pt, or an older torch
        print(f"S-KEY could not be loaded ({e}); key from the profiles only", file=sys.stderr)
        return None
    return hcqt.eval(), net.eval(), CropCQT(84)


def skey_vqt(hcqt, y):
    """S-KEY's VQT of the whole signal, computed SKEY_CHUNK_S at a time (all at once, nnAudio needs ~10 MB per
    second of audio: 4.7 GB for a 7-minute song). Each chunk gets a 2 s margin on both sides, which is dropped,
    so the frames match a single pass."""
    import torch
    hop, margin = 512, 88 * 512
    chunk = SKEY_CHUNK_S * SR // hop * hop
    parts = []
    for s in range(0, len(y), chunk):
        a, b = max(0, s - margin), min(len(y), s + chunk + margin)
        v = hcqt(torch.tensor(y[a:b], dtype=torch.float32)[None, None])
        last = len(y) // hop + 1 if s + chunk >= len(y) else (s + chunk) // hop
        parts.append(v[..., s // hop - a // hop: last - a // hop])
    return torch.cat(parts, dim=-1)


def skey_key_probs(y):
    """S-KEY's 24 key probabilities for a mono 22.05 kHz signal, in this file's order (C..B major, C..B minor)."""
    import torch
    hcqt, net, crop = skey_model()
    with torch.inference_mode():
        vqt = skey_vqt(hcqt, y / (np.abs(y).max() + 1e-9))
        out = net(crop(vqt, torch.zeros(1))).mean(dim=0).numpy().astype(float)
    # S-KEY's order starts at A major (0) and B minor (12), a semitone apart each
    ours = np.zeros(24)
    for i in range(12):
        ours[(9 + i) % 12] = out[i]
        ours[12 + (11 + i) % 12] = out[12 + i]
    return ours / ours.sum()


def key_label(index):
    tonic, minor = index % 12, index >= 12
    spelled = (SPELL_MINOR if minor else SPELL_MAJOR)[tonic]
    return {"tonic": NAMES[tonic], "mode": "minor" if minor else "major",
            "label_zh": f"{spelled} {'小调' if minor else '大调'}",
            "label_en": f"{spelled} {'minor' if minor else 'major'}"}


def relative_key(index):
    return (index + 9) % 12 + 12 if index < 12 else (index + 3) % 12


def key_result(prob, method):
    """Best key, its probability and the runners-up (always including the relative key)."""
    order = list(np.argsort(-prob))
    best = order[0]
    shown = order[1:4]
    if relative_key(best) not in shown:
        shown.append(relative_key(best))
    alternatives = [{"label_zh": key_label(i)["label_zh"], "label_en": key_label(i)["label_en"],
                     "score": round(float(prob[i]), 3)} for i in shown]
    return {**key_label(best), "confidence": round(float(prob[best]), 3), "alternatives": alternatives,
            "method": method}


def analyze_key(mix, chroma):
    """Key from S-KEY and from the profiles, and KEY_DEFAULT's pick of the two. None if all is silent.

    The ensemble averages the two 24-key probability lists, so its confidence drops when they disagree.
    by_method is keyed by the short names "profiles" and "skey"; each entry says its method in full.
    """
    if not np.any(chroma["harmonic"]):
        return None
    bass = mean_profile(chroma["bass"]) if chroma["bass"] is not None else None
    opinions = {"profiles": (profile_key_probs(mean_profile(chroma["harmonic"]), bass),
                             f"{KEY_PROFILE} key profiles on CQT chroma of the stems without drums (vocals x0.5)"
                             + (f" + {BASS_WEIGHT:g} x bass stem" if bass is not None else ""))}
    if len(mix) >= SKEY_MIN_S * SR and skey_model() is not None:
        opinions["skey"] = (skey_key_probs(mix), f"S-KEY (Deezer ICASSP 2025, vendored; nnAudio "
                                                 f"{package_version('nnAudio')}) on the full mix")
    if KEY_DEFAULT == "ensemble" and len(opinions) == 2:
        prob = (opinions["skey"][0] + opinions["profiles"][0]) / 2
        default = key_result(prob, "mean of the probabilities of: " + "; ".join(m for _, m in opinions.values()))
    else:
        default = key_result(*opinions.get(KEY_DEFAULT, opinions["profiles"]))
    by_method = {}
    for name, (prob, method) in opinions.items():
        r = key_result(prob, method)
        by_method[name] = {k: r[k] for k in ("label_en", "label_zh", "confidence", "method")}
    return {**default, "by_method": by_method}


def key_index(key):
    return NAMES.index(key["tonic"]) + (12 if key["mode"] == "minor" else 0)


# ---------- chords ----------

@functools.cache
def template(root, quality, decay=0.0):
    """Unit-length chroma template: the chord tones, plus their overtones when decay > 0 (the k-th partial
    weighted decay ** (k - 1)). With overtones, the 3rd partial of a major chord's third (its major seventh)
    is expected, so a plain major chord no longer looks like a maj7."""
    t = np.zeros(12)
    for i in CHORD_TYPES[quality][0]:
        for h in range(1, TEMPLATE_HARMONICS + 1):
            t[(root + i + int(round(12 * np.log2(h)))) % 12] += decay ** (h - 1)
    return t / np.linalg.norm(t)


def key_fit(root, quality, key_idx):
    """'diatonic', 'borrowed' (common outside the key: in major bIII, iv, bVI, bVII and the major II and III;
    in minor the major V, the major IV and the major I) or 'other'."""
    if key_idx is None:
        return "diatonic"
    tonic, minor = key_idx % 12, key_idx >= 12
    degree = (root - tonic) % 12
    if minor:
        diatonic = {(0, "min"), (3, "maj"), (5, "min"), (7, "min"), (8, "maj"), (10, "maj")}
        borrowed = {(7, "maj"), (5, "maj"), (0, "maj")}
    else:
        diatonic = {(0, "maj"), (2, "min"), (4, "min"), (5, "maj"), (7, "maj"), (9, "min")}
        borrowed = {(10, "maj"), (5, "min"), (8, "maj"), (3, "maj"), (2, "maj"), (4, "maj")}
    return "diatonic" if (degree, quality) in diatonic else "borrowed" if (degree, quality) in borrowed else "other"


def beat_grid(beats, n_frames, duration):
    """Chroma frame indices and times of the beat-length chord frames: [0, beat 1, beat 2, ..., end]."""
    if len(beats) < 2:  # no beats found: fall back to half-second frames
        beats = np.arange(0.5, duration, 0.5)
    t = np.concatenate([[0.0], beats[(beats > 0) & (beats < duration)], [duration]])
    f = np.minimum(librosa.time_to_frames(t, sr=SR, hop_length=HOP), n_frames)
    keep = np.concatenate([[True], np.diff(f) > 0])
    f, t = f[keep], t[keep]
    t[-1] = duration  # a last beat in the song's last frame gives way to the end, so the chords reach it
    return f, t


def unit_columns(c):
    c = c - c.min(axis=0, keepdims=True)  # the noise floor folds into every pitch class
    return c / (np.linalg.norm(c, axis=0, keepdims=True) + 1e-12)


def beat_features(chroma, beats, duration):
    """Per beat: harmonic chroma, log-compressed and linear (unit length), bass chroma (0..1, scaled by how
    much bass sounds), loudness in dB below the song's loud beats, and the beat-frame boundaries in seconds."""
    frames, times = beat_grid(beats, chroma["harmonic"].shape[1], duration)
    sync = functools.partial(librosa.util.sync, idx=frames, pad=False)
    energy = sync(chroma["harmonic"].sum(axis=0, keepdims=True), aggregate=np.mean)[0]
    loud = np.percentile(energy, 90) if np.any(energy) else 1.0
    out = {"log": unit_columns(sync(chroma["harmonic_log"], aggregate=np.median)),
           "linear": unit_columns(sync(chroma["harmonic"], aggregate=np.median)),
           "level_db": 10 * np.log10(energy / (loud + 1e-12) + 1e-12), "times": times}
    if chroma["bass"] is not None:
        bb = sync(chroma["bass"], aggregate=np.mean)
        presence = np.clip(bb.sum(axis=0) / (np.percentile(bb.sum(axis=0), 75) + 1e-12), 0, 1)
        out["bass"] = bb / (bb.max(axis=0, keepdims=True) + 1e-12) * presence
    else:
        out["bass"] = np.zeros_like(out["log"])
    return out


def triad_scores(hb, bb, level_db, key_idx):
    """Score of each of the 25 states (0-11 C..B major, 12-23 C..B minor, 24 no chord) on every beat."""
    scores = np.zeros((25, hb.shape[1]))
    for state in range(24):
        root, quality = state % 12, ("maj", "min")[state // 12]
        tones = [(root + i) % 12 for i in CHORD_TYPES[quality][0]]
        bass = np.maximum(bb[root], 0.6 * bb[tones[1:]].max(axis=0))
        scores[state] = template(root, quality) @ hb + CHORD_BASS_WEIGHT * bass \
            + KEY_PRIOR[key_fit(root, quality, key_idx)]
    scores[24] = np.where(level_db < CHORD_SILENCE_DB, 10.0, CHORD_N_SCORE)
    return scores


def refine(root, quality, h, b):
    """Quality (the triad, or a 7th / sus / dim / 5 that fits clearly better) and slash bass (or None) for one
    segment, from its unit-length chroma h and bass chroma b."""
    best, best_fit = quality, template(root, quality, REFINE_DECAY) @ h + EXTENSION_MARGIN
    for q in REFINE[quality]:
        fit = template(root, q, REFINE_DECAY) @ h
        if fit > best_fit:
            best, best_fit = q, fit
    tones = [(root + i) % 12 for i in CHORD_TYPES[best][0]]
    low = int(np.argmax(b))
    slash = low if best != "5" and low != root and low in tones and b[low] > SLASH_RATIO * b[root] \
        and b[low] > 0.3 else None
    return best, slash


def note_name(pc, key_idx):
    """Spell a note for a chord label: flats in flat keys, and for the b2, b3, b6 and b7 of a major key (the
    borrowed chords: Bb and Ab in C major, not A# and G#) or the b2 of a minor key, whose b3, b6 and b7 are
    its own notes (F# in G# minor, not Gb); sharps otherwise, and for a minor key's leading tone (C# in D minor)."""
    if key_idx is None:
        return NAMES[pc]
    minor, degree = key_idx >= 12, (pc - key_idx) % 12
    flat_key = (key_idx % 12, "minor" if minor else "major") in FLAT_KEYS and not (minor and degree == 11)
    return (FLATS if flat_key or degree in ((1,) if minor else (1, 3, 8, 10)) else NAMES)[pc]


def bass_name(root_name, interval, key_idx):
    """The bass note of a slash chord, spelled as the chord tone it is: the third of D is F# (D/F#) even in G
    minor, the fifth of Bb is F. note_name's spelling when that would need E#, B#, Cb, Fb or a double sharp/flat."""
    letters = "CDEFGAB"
    letter = letters[(letters.index(root_name[0]) + {2: 1, 3: 2, 4: 2, 5: 3, 6: 4, 7: 4, 10: 6, 11: 6}[interval]) % 7]
    pc = (NAMES.index(root_name[0]) + {"#": 1, "b": -1}.get(root_name[1:], 0) + interval) % 12
    name = letter + {0: "", 1: "#", 11: "b"}.get((pc - NAMES.index(letter)) % 12, "?")
    return name if name in NAMES or name in FLATS else note_name(pc, key_idx)


def chord_label(root, quality, slash, key_idx):
    name = note_name(root, key_idx)
    bass = f"/{bass_name(name, (slash - root) % 12, key_idx)}" if slash is not None else ""
    return name + CHORD_TYPES[quality][1] + bass


def analyze_chords(chroma, beats, duration, key_idx):
    f = beat_features(chroma, beats, duration)
    scores = triad_scores(f["log"], f["bass"], f["level_db"], key_idx)
    post = np.exp((scores - scores.max(axis=0)) / CHORD_TEMPERATURE)
    post /= post.sum(axis=0, keepdims=True)
    states = librosa.sequence.viterbi_discriminative(post, librosa.sequence.transition_loop(25, CHORD_SELF))
    segments = []
    starts = np.flatnonzero(np.diff(states, prepend=-1))
    for a, b in zip(starts, list(starts[1:]) + [len(states)]):
        state = int(states[a])
        seg = {"start": round(float(f["times"][a]), 3), "end": round(float(f["times"][b]), 3)}
        if state == 24:
            seg.update(label="N", root="N", quality="N")
        else:
            root, quality = state % 12, ("maj", "min")[state // 12]
            h = f["linear"][:, a:b] @ np.maximum(10 ** (f["level_db"][a:b] / 10), 1e-6)
            quality, slash = refine(root, quality, h / (np.linalg.norm(h) + 1e-12), f["bass"][:, a:b].mean(axis=1))
            seg.update(label=chord_label(root, quality, slash, key_idx), root=NAMES[root], quality=quality)
        if segments and segments[-1]["label"] == seg["label"]:
            segments[-1]["end"] = seg["end"]
        else:
            segments.append(seg)
    method = (f"beat-synchronous log-CQT chroma of the stems without drums, bass stem for roots; 24 triads + N, "
              f"key-aware prior, Viterbi (librosa {librosa.__version__}); then 7/maj7/m7/sus/dim/5 and slash chords")
    return {"segments": segments, "method": method}


# ---------- peaks ----------

def peaks(mix):
    """Min/max pairs of the mix, PEAK_BINS_PER_S pairs per second, scaled into -1..1."""
    hop = SR // PEAK_BINS_PER_S
    n = len(mix) // hop
    frames = mix[: n * hop].reshape(n, hop)
    scale = max(1.0, float(np.abs(mix).max()))
    data = np.empty(2 * n)
    data[0::2], data[1::2] = frames.min(axis=1) / scale, frames.max(axis=1) / scale
    return {"samples_per_second": 2 * PEAK_BINS_PER_S, "data": [round(float(v), 3) for v in data]}


# ---------- song ----------

def stem_audio(stems):
    audio = {name: load_mono(path) for name, path in stems.items()}
    n = min(len(v) for v in audio.values())
    return {name: v[:n] for name, v in audio.items()}, n


def song_signals(audio, n):
    """The mix, the harmonic stems (no drums or bass; vocals at VOCAL_WEIGHT) and the bass stem (or None).
    Harmonic stems or a bass stem more than STEM_MIN_DB below the mix count as absent (silence / None)."""
    mix = sum(audio.values())
    harmonic = sum(audio[k] for k in ACCOMP_STEMS if k in audio) + VOCAL_WEIGHT * audio.get("vocals", 0)
    if not isinstance(harmonic, np.ndarray) or not audible(harmonic, mix):  # no harmonic stem at all
        harmonic = np.zeros(n, np.float32)
    bass = audio.get("bass")
    return mix, harmonic.astype(np.float32), bass if bass is not None and audible(bass, mix) else None


def audible(y, mix):
    """Whether y is louder (RMS) than STEM_MIN_DB below the mix."""
    def power(v):
        return float(np.mean(np.square(v, dtype=np.float64)))
    return power(y) > 0 and power(y) >= power(mix) * 10 ** (STEM_MIN_DB / 10)


def analyze_song(song_dir, stems):
    audio, n = stem_audio(stems)
    duration = n / SR
    mix, harmonic, bass = song_signals(audio, n)
    del audio  # only these sums are needed from here on
    chroma = song_chroma(harmonic, bass)
    del harmonic, bass

    tempo, beats = analyze_tempo(mix, duration)
    key = analyze_key(mix, chroma)
    chords = analyze_chords(chroma, beats, duration, key_index(key) if key else None)
    return {"version": VERSION, "song": song_dir.name, "duration_s": round(duration, 2), "tempo": tempo,
            "key": key, "chords": chords, "peaks": peaks(mix)}


def is_up_to_date(out, stems):
    if not out.exists() or out.stat().st_mtime <= max(p.stat().st_mtime for p in stems.values()):
        return False
    try:
        return json.loads(out.read_text(encoding="utf-8")).get("version") == VERSION
    except (OSError, ValueError):
        return False


def summary(result, seconds):
    t, k, c = result["tempo"], result["key"], result["chords"]["segments"]
    key_text = f"{k['label_en']} ({k['confidence']:.2f})" if k else "no key"
    others = [f"{m['label_en']}" for m in k["by_method"].values() if m["label_en"] != k["label_en"]] if k else []
    labels = [s["label"] for s in c if s["label"] != "N"]
    common = sorted(set(labels), key=lambda x: -sum(s["end"] - s["start"] for s in c if s["label"] == x))[:6]
    return (f"{result['song']}: {t['bpm']} BPM, {t['beats_per_bar']} beats/bar, key {key_text}"
            f"{' (other method: ' + ', '.join(others) + ')' if others else ''}, {len(c)} chord segments, "
            f"{'mostly ' + ' '.join(common) if common else 'no chords'}, {seconds:.1f} s")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stems_root", type=Path)
    parser.add_argument("--song", help="only this song folder")
    parser.add_argument("--force", action="store_true", help="redo songs that already have analysis.json")
    args = parser.parse_args()

    if not args.stems_root.is_dir():
        raise SystemExit(f"{args.stems_root} is not a folder")
    song_dirs = sorted(p for p in args.stems_root.iterdir() if p.is_dir())
    if args.song:
        song_dirs = [p for p in song_dirs if p.name == args.song]
    if not song_dirs:
        raise SystemExit(f"no song folder matched under {args.stems_root}")
    failed = []
    for song_dir in song_dirs:
        stems = find_stems(song_dir)
        if not stems:
            continue
        out = song_dir / "analysis.json"
        if not args.force and is_up_to_date(out, stems):
            print(f"{song_dir.name}: analysis.json is up to date (use --force to redo)")
            continue
        start = time.perf_counter()
        try:
            result = analyze_song(song_dir, stems)
        except Exception as e:  # e.g. an unreadable stem file: report it and go on with the other songs
            print(f"{song_dir.name}: FAILED, {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            failed.append(song_dir.name)
            continue
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, out)
        print(summary(result, time.perf_counter() - start), flush=True)
    if failed:
        raise SystemExit(f"analysis failed for: {', '.join(failed)}")


if __name__ == "__main__":
    main()
