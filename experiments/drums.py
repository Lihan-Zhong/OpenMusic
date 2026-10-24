"""Drum stem -> drum hits as General MIDI percussion notes, for transcribe.py (扒鼓谱).

    hits = drums.transcribe(y)     # mono 44.1 kHz drum stem -> [(start_s, end_s, GM note, velocity 1-127)]

How:
1. Onset candidates: peaks of the summed rise of a log-frequency magnitude spectrogram (12 bands per octave,
   30 Hz - 17 kHz, 10 ms frames), at least 30 ms apart. There are a few more candidates than hits; the
   classifier drops the rest (bleed from other instruments, ringing, noise).
2. Which kit pieces were hit at each candidate (any number at once: kick + hi-hat, snare + crash, ...): a small
   neural network (an MLP, 1040 -> 256 -> 64 -> 9 sigmoid outputs) looks at the coarse spectrum (3 bands per
   octave) from 30 ms before the candidate to 150 ms after it, plus its mean level 150-250 ms and 250-450 ms
   after (how long it rings: closed vs open hi-hat, hi-hat vs crash and ride), once relative to the stem's loud
   level and once relative to each band's own loud level (so a kit EQ'd differently looks the same).
3. Pieces sharing a limb or a sound are exclusive: of closed and open hi-hat, of the three toms, and of crash and
   ride only the likeliest one is kept per candidate. Velocity is how loud the hit is in that piece's band,
   relative to the piece's loudest hit in the song.

The weights (experiments/models/drums.npz) were trained by experiments/train_drums.py on part of E-GMD
(Callender, Hawthorne & Engel 2020, CC BY 4.0, https://magenta.tensorflow.org/datasets/e-gmd): human
performances on a Roland e-drum kit, each recorded through up to 43 of its kits, with augmentation (EQ,
compression, room reverb, quiet pitched bleed, low-pass) so that it also works on drum stems separated from a
mix. Scores on held-out E-GMD performances and on real recordings (MDB Drums) are in docs/drums.md.

Optional plugin: ADTOF-pytorch (Xavier Riley's PyTorch port of ADTOF by Zehren, Alunno & Bientinesi,
https://github.com/xavriley/ADTOF-pytorch). Its weights are CC BY-NC-SA 4.0 (non-commercial), so it is not part
of OpenMusic: the user installs it in its own venv (README), and transcribe_adtof runs its `adtof` command as a
separate process and reads the MIDI file it writes. It knows 5 classes (kick, snare, toms, hi-hat, cymbals); our
classifier then picks the piece within the class at each of its hits (which tom, closed or open hi-hat, crash or
ride), and velocities are measured as for our own hits.
"""
import os
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import librosa
import numpy as np

SR = 44100
N_FFT = 2048
HOP = 441                       # 10 ms frames
FPS = SR / HOP
FMIN, FMAX, PER_OCTAVE = 30.0, 17000.0, 12
COARSE = 4                      # the classifier sees PER_OCTAVE / COARSE = 3 bands per octave
FLOOR = -4.0                    # log10 magnitude floor, relative to the stem's loud level (-80 dB)
WAIT = 3                        # candidates at least this many frames apart (30 ms)
DELTA = 0.02                    # a candidate's rise must be this far above its local mean (log10 units)
PRE, POST = 3, 15               # classifier frames before / after the candidate frame
TAILS = ((15, 25), (25, 45))    # then the mean of these frame ranges after it
OFFSET_S = 0.01                 # a hit's spectral rise peaks about 10 ms before the hit (2048-sample frames)
GATE_DB = 40.0                  # candidates this far below the stem's loud frames are not hits

MODEL = Path(__file__).resolve().parent / "models" / "drums.npz"
ADTOF_ENV = "OPENMUSIC_ADTOF"   # path of the plugin's `adtof` command, if not in the default place below
ADTOF_DEFAULT = Path(__file__).resolve().parent.parent / "plugins" / "ADTOF-pytorch" / ".venv" / \
    ("Scripts/adtof.exe" if os.name == "nt" else "bin/adtof")
ADTOF_CLASS = {35: ("kick",), 36: ("kick",), 38: ("snare",), 47: ("tom_hi", "tom_mid", "tom_lo"),
               42: ("hihat", "hihat_open"), 49: ("crash", "ride")}   # its MIDI notes -> the pieces they cover

# kit pieces in the classifier's output order, their General MIDI percussion notes and names
PIECES = ("kick", "snare", "tom_hi", "tom_mid", "tom_lo", "hihat", "hihat_open", "crash", "ride")
GM_NOTE = {"kick": 36, "snare": 38, "tom_hi": 50, "tom_mid": 47, "tom_lo": 43, "hihat": 42, "hihat_open": 46,
           "crash": 49, "ride": 51}
NAMES = {"kick": "Kick", "snare": "Snare", "tom_hi": "High tom", "tom_mid": "Mid tom", "tom_lo": "Floor tom",
         "hihat": "Hi-hat", "hihat_open": "Open hi-hat", "crash": "Crash", "ride": "Ride"}
THRESHOLD = {"tom_hi": 0.7, "tom_mid": 0.7, "tom_lo": 0.7}   # others 0.5; toms are rare in songs, common in E-GMD
EXCLUSIVE = (("hihat", "hihat_open"), ("tom_hi", "tom_mid", "tom_lo"), ("crash", "ride"))
VELOCITY_HZ = {"kick": (30, 150), "snare": (150, 6000), "tom_hi": (60, 1000), "tom_mid": (60, 1000),
               "tom_lo": (60, 1000), "hihat": (6000, 17000), "hihat_open": (6000, 17000), "crash": (3000, 17000),
               "ride": (3000, 17000)}
HIT_S = 0.1                     # length of a hit in the MIDI file and notes.json


def filterbank():
    """Triangular log-frequency filters (bands x FFT bins), PER_OCTAVE per octave, each at least one bin wide;
    and the bands' centre frequencies."""
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR)
    n = int(np.floor(PER_OCTAVE * np.log2(FMAX / FMIN)))
    bins = np.unique(np.round(FMIN * 2 ** (np.arange(n + 2) / PER_OCTAVE) / (SR / N_FFT)).astype(int))
    fb = np.zeros((len(bins) - 2, len(freqs)))
    for i, (a, c, b) in enumerate(zip(bins, bins[1:], bins[2:])):
        fb[i, a:c + 1] = np.linspace(0, 1, c - a + 1) if c > a else 1
        fb[i, c:b + 1] = np.linspace(1, 0, b - c + 1) if b > c else 1
    return fb, freqs[bins[1:-1]]


FB, BAND_HZ = filterbank()
GROUP = np.floor(np.log2(BAND_HZ / FMIN) * PER_OCTAVE / COARSE).astype(int)   # coarse band of each band


def log_spectrogram(y):
    """log10 magnitude (bands x frames, frame t centred at t * HOP) relative to the stem's loud level (the
    99.5th percentile of all band values), floored at FLOOR."""
    S = FB @ np.abs(librosa.stft(np.asarray(y, np.float32), n_fft=N_FFT, hop_length=HOP))
    return np.log10(np.maximum(S / (np.percentile(S, 99.5) + 1e-12), 10 ** FLOOR))


def candidates(L):
    """Frames where the spectrum rises: local maxima (+-WAIT frames) of the mean rise of each band over the
    larger of its two previous frames, DELTA above the mean of the 100 ms around them."""
    prev = np.maximum(np.pad(L, ((0, 0), (1, 0)), mode="edge")[:, :-1],
                      np.pad(L, ((0, 0), (2, 0)), mode="edge")[:, :-2])
    rise = np.maximum(L - prev, 0).mean(axis=0)
    return librosa.util.peak_pick(rise, pre_max=WAIT, post_max=WAIT, pre_avg=10, post_avg=10, delta=DELTA,
                                  wait=WAIT)


def coarse(L):
    return np.stack([L[GROUP == g].mean(axis=0) for g in np.unique(GROUP)])


def features(L, frames):
    """Classifier input per candidate frame (rows): the coarse spectrum from PRE frames before to POST after,
    then its mean over each of TAILS; first relative to the stem's loud level, then to each band's own (99th
    percentile)."""
    pad = max(PRE, TAILS[-1][1])
    Lp = np.pad(L, ((0, 0), (pad, pad)), constant_values=FLOOR)
    out = []
    for C in (coarse(Lp), coarse(Lp - np.percentile(L, 99, axis=1, keepdims=True))):
        rows = []
        for f in np.asarray(frames, int) + pad:
            rows.append(np.concatenate([C[:, f - PRE:f + POST].ravel()] + [C[:, f + a:f + b].mean(axis=1)
                                                                           for a, b in TAILS]))
        out.append(np.array(rows, np.float32).reshape(len(frames), -1))
    return np.concatenate(out, axis=1)


@lru_cache(maxsize=1)
def model():
    if not MODEL.is_file():
        raise SystemExit(f"{MODEL} is missing; it comes with the repository (or run experiments/train_drums.py)")
    with np.load(MODEL) as m:
        layers = [(m[f"W{i}"].astype(np.float32), m[f"b{i}"].astype(np.float32)) for i in range(int(m["layers"]))]
        if tuple(m["pieces"]) != PIECES:
            raise SystemExit(f"{MODEL} was trained for other kit pieces: {list(m['pieces'])}")
        return m["mean"].astype(np.float32), m["scale"].astype(np.float32), layers


def probabilities(X):
    """The classifier's output (candidates x PIECES): how likely each piece was hit there."""
    mean, scale, layers = model()
    h = (X - mean) / scale
    for i, (W, b) in enumerate(layers):
        h = h @ W + b
        h = np.maximum(h, 0) if i < len(layers) - 1 else 0.5 * (1 + np.tanh(h / 2))  # ReLU, then sigmoid
    return h


def decide(P):
    """Hits (candidates x PIECES, bool) from the probabilities: above THRESHOLD, and one piece per EXCLUSIVE
    group."""
    hit = P > np.array([THRESHOLD.get(p, 0.5) for p in PIECES])
    for group in EXCLUSIVE:
        idx = [PIECES.index(p) for p in group]
        best = np.array(idx)[np.argmax(P[:, idx], axis=1)]
        for k in idx:
            hit[:, k] &= best == k
    return hit


def velocities(L, frames, hit):
    """MIDI velocity per hit: the peak level in the piece's band (VELOCITY_HZ) over the 30 ms after the candidate,
    in dB below the piece's loudest hit in the song; 0 dB -> 120, -30 dB -> 30, at least 20."""
    vel = np.zeros(hit.shape, int)
    for k, piece in enumerate(PIECES):
        rows = np.flatnonzero(hit[:, k])
        if not len(rows):
            continue
        lo, hi = VELOCITY_HZ[piece]
        band = L[(BAND_HZ >= lo) & (BAND_HZ < hi)].max(axis=0)
        level = np.array([band[f:f + 3].max() for f in frames[rows]]) * 20
        vel[rows, k] = np.clip(np.round(120 + 3 * (level - level.max())), 20, 127)
    return vel


def as_hits(L, times, pieces):
    """[(start_s, end_s, GM note, velocity)] of hits at times (s) of pieces, velocity as in velocities()."""
    if not len(times):
        return []
    frames = np.clip(np.round((np.asarray(times) - OFFSET_S) * FPS).astype(int), 0, L.shape[1] - 1)
    hit = np.zeros((len(times), len(PIECES)), bool)
    hit[np.arange(len(times)), [PIECES.index(p) for p in pieces]] = True
    vel = velocities(L, frames, hit).max(axis=1)
    return sorted((round(float(t), 3), round(float(t) + HIT_S, 3), GM_NOTE[p], int(v))
                  for t, p, v in zip(times, pieces, vel))


def adtof_command():
    """The ADTOF-pytorch plugin's `adtof` command: $OPENMUSIC_ADTOF, else ADTOF_DEFAULT; None if neither exists."""
    path = Path(os.environ[ADTOF_ENV]).expanduser() if os.environ.get(ADTOF_ENV) else ADTOF_DEFAULT
    return path if path.is_file() else None


def transcribe_adtof(path, y, command):
    """Drum hits like transcribe(), from the ADTOF-pytorch plugin run on the stem file `path` (its own process,
    its own venv); y is the same stem, mono 44.1 kHz, for the velocities. Raises RuntimeError if it fails."""
    import mido
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "adtof.mid"
        run = subprocess.run([str(command), "--audio", str(path), "--out", str(out), "--device", "cuda"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             env=dict(os.environ, PYTHONIOENCODING="utf-8"))  # not the locale's code page (GBK)
        if run.returncode or not out.is_file():
            raise RuntimeError(f"{command} failed: {(run.stderr or run.stdout).strip()[-500:]}")
        times, classes, t = [], [], 0.0
        for msg in mido.MidiFile(str(out)):
            t += msg.time
            if msg.type == "note_on" and msg.velocity > 0 and msg.note in ADTOF_CLASS:
                times.append(t)
                classes.append(ADTOF_CLASS[msg.note])
    L = log_spectrogram(y)
    if not times:
        return []
    frames = np.clip(np.round((np.asarray(times) - OFFSET_S) * FPS).astype(int), 0, L.shape[1] - 1)
    P = probabilities(features(L, frames))
    pieces = [max(group, key=lambda p: P[i, PIECES.index(p)]) for i, group in enumerate(classes)]
    return as_hits(L, times, pieces)


def transcribe(y):
    """Drum hits [(start_s, end_s, GM note, velocity)] of a mono 44.1 kHz drum stem, sorted by time."""
    L = log_spectrogram(y)
    frames = candidates(L)
    if not len(frames):
        return []
    loud = 20 * np.log10(np.sum(10 ** L, axis=0))
    frames = frames[np.array([loud[f:f + 3].max() for f in frames]) > np.percentile(loud, 99) - GATE_DB]
    if not len(frames):
        return []
    hit = decide(probabilities(features(L, frames)))
    vel = velocities(L, frames, hit)
    hits = []
    for i, f in enumerate(frames):
        t = round(f / FPS + OFFSET_S, 3)
        hits += [(t, round(t + HIT_S, 3), GM_NOTE[p], int(vel[i, k])) for k, p in enumerate(PIECES) if hit[i, k]]
    return sorted(hits)
