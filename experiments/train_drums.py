"""Train and score the drum-hit classifier of experiments/drums.py (its weights: experiments/models/drums.npz).

    python experiments/train_drums.py                     # download the E-GMD subset, train, write the model
    python experiments/train_drums.py --eval              # score the model: E-GMD test, MDB Drums if present
    python experiments/train_drums.py --separate demucs6  # split the MDB Drums mixes, to score on separated drums
    python experiments/train_drums.py --eval --adtof      # the same scores for the optional ADTOF-pytorch plugin

Training data: part of E-GMD v1.0.0 (Callender, Hawthorne & Engel, "Improving Perceptual Quality of Drum
Transcription with the Expanded Groove MIDI Dataset", 2020; CC BY 4.0): human performances on a Roland TD-17
e-drum kit, each rendered through up to 43 of its kits, with the MIDI of what was played. Of its train split
we take, per kit, PER_KIT beats and fills (distinct performances where possible); single members are read out
of the 96 GB zip with HTTP range requests, about 0.8 GB in all, into data/e-gmd/ (git-ignored). Each file is
used as it is and AUGMENT more times through random EQ, compression, room reverb, quiet pitched bleed and a
low-pass, so that the classifier also fits drum stems separated from a mix. On top come SYNTH_LOOPS loops of
random synthetic kits playing random bars (drum_synth.random_loop, half of them augmented too): with E-GMD alone
the classifier knew only E-GMD's sampled kits and missed most synthesised hi-hats; with them it finds those and
does as well or slightly better on real recordings (docs/drums.md).

Labels: at every onset candidate of drums.candidates, which kit pieces (drums.PIECES) E-GMD has a hit of within
MATCH_S. The classifier is scikit-learn's MLPClassifier (two hidden ReLU layers, multi-label logistic output,
early stopping on 10 % of the candidates); its weights are stored as float16 in an .npz and run with numpy.

Scores (mir_eval's onset matching, 50 ms): per piece and for the usual groups (hi-hat = closed + open, toms,
cymbals = crash + ride), summed over files. --eval uses E-GMD's test split (other performances, same kits) and,
if it is in data/MDBDrums (git clone https://github.com/CarlSouthall/MDBDrums data/MDBDrums; CC BY-NC-SA 4.0,
so only for local evaluation, never for training), MDB Drums: 23 real studio recordings from MedleyDB with
hand-made annotations, as the drums-only mix and as the drum stem separated from the full mix (--separate).
Its MIREX 2017 halves are reported apart: "dev" was looked at while choosing the design, "test" only at the end.
"""
import argparse
import csv
import io
import random
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

import mido
import numpy as np
from mir_eval.util import match_events
from scipy import signal

import drum_synth
import drums
from common import DATA, load

EGMD_URL = "https://storage.googleapis.com/magentadata/datasets/e-gmd/v1.0.0/e-gmd-v1.0.0.zip"
EGMD_CSV = "https://storage.googleapis.com/magentadata/datasets/e-gmd/v1.0.0/e-gmd-v1.0.0.csv"
EGMD = DATA / "e-gmd"
MDB = DATA / "MDBDrums" / "MDB Drums"
MDB_SEPARATED = DATA / "MDBDrums" / "separated"

PER_KIT = {"train": {"beat": 5, "fill": 3}, "validation": {"beat": 1}, "test": {"beat": 1}}
SECONDS = {"beat": (8, 60), "fill": (1.5, 45)}
AUGMENT = 2
SYNTH_LOOPS = 150
HIDDEN = (256, 64)
MATCH_S = 0.05
SEED = 0

# E-GMD's MIDI notes (Roland TD-17, see the GMD paper) -> kit piece; 54 (tambourine) and others are left out
EGMD_PIECE = {36: "kick", 37: "snare", 38: "snare", 40: "snare", 48: "tom_hi", 50: "tom_hi", 45: "tom_mid",
              47: "tom_mid", 43: "tom_lo", 58: "tom_lo", 42: "hihat", 22: "hihat", 44: "hihat", 46: "hihat_open",
              26: "hihat_open", 49: "crash", 55: "crash", 57: "crash", 52: "crash", 51: "ride", 59: "ride",
              53: "ride"}
# MDB Drums subclasses -> kit piece (TMB, tambourine, is left out)
MDB_PIECE = {"KD": "kick", "SD": "snare", "SDB": "snare", "SDD": "snare", "SDF": "snare", "SDG": "snare",
             "SDNS": "snare", "SST": "snare", "CHH": "hihat", "PHH": "hihat", "OHH": "hihat_open", "HIT": "tom_hi",
             "MHT": "tom_mid", "HFT": "tom_lo", "LFT": "tom_lo", "CRC": "crash", "CHC": "crash", "SPC": "crash",
             "RDC": "ride", "RDB": "ride"}
MDB_DEV = ("80sRock", "BebopJazz", "Britpop", "CoolJazz", "Disco", "FunkJazz", "FusionJazz", "Reggae", "Rock",
           "Rockabilly", "Shadows", "Zeppelin")          # MIREX 2017 training half; the other 11 are "test"
GROUPS = {"hi-hat": ("hihat", "hihat_open"), "toms": ("tom_hi", "tom_mid", "tom_lo"), "cymbals": ("crash", "ride")}


# ---------- E-GMD ----------

class HttpFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests, so zipfile can read single members of a remote zip."""

    def __init__(self, url):
        self.url, self.pos = url, 0
        self.size = int(urllib.request.urlopen(urllib.request.Request(url, method="HEAD")).headers["Content-Length"])

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def readinto(self, buffer):
        n = min(len(buffer), self.size - self.pos)
        if n <= 0:
            return 0
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{self.pos + n - 1}"})
        data = urllib.request.urlopen(req).read()
        buffer[:len(data)] = data
        self.pos += len(data)
        return len(data)


def egmd_rows():
    path = EGMD / "e-gmd-v1.0.0.csv"
    if not path.is_file():
        EGMD.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(EGMD_CSV, path)
    return list(csv.DictReader(path.open(encoding="utf-8")))


def egmd_subset(split):
    """The files we use from one split: per kit PER_KIT[split] beats / fills of SECONDS, from performances not
    used for another kit where possible (seeded, so always the same files)."""
    rows = [r for r in egmd_rows() if r["split"] == split]
    random.Random(SEED).shuffle(rows)
    chosen, used, count = {}, set(), Counter()
    for distinct in (True, False):
        for r in rows:
            kind, key = r["beat_type"], (r["kit_name"], r["beat_type"])
            lo, hi = SECONDS.get(kind, (0, 0))
            if (count[key] >= PER_KIT[split].get(kind, 0) or not lo < float(r["duration"]) < hi
                    or r["audio_filename"] in chosen or distinct and r["id"] in used):
                continue
            chosen[r["audio_filename"]] = r
            used.add(r["id"])
            count[key] += 1
    return [chosen[k] for k in sorted(chosen)]


def egmd_files(split):
    """[(wav, midi)] of the subset, downloading what is missing."""
    rows = egmd_subset(split)
    missing = [r for r in rows if not (EGMD / split / r["audio_filename"]).is_file()]
    if missing:
        print(f"E-GMD {split}: reading {len(missing)} of {len(rows)} files out of {EGMD_URL}")
        archive = zipfile.ZipFile(io.BufferedReader(HttpFile(EGMD_URL), buffer_size=1 << 20))
        for i, r in enumerate(missing):
            for key in ("midi_filename", "audio_filename"):
                dst = EGMD / split / r[key]
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_suffix(dst.suffix + ".part")
                tmp.write_bytes(archive.read("e-gmd-v1.0.0/" + r[key]))
                tmp.replace(dst)
            if (i + 1) % 50 == 0:
                print(f"  {i + 1} / {len(missing)}", flush=True)
    return [(EGMD / split / r["audio_filename"], EGMD / split / r["midi_filename"]) for r in rows]


def egmd_hits(path):
    """[(time_s, piece)] of an E-GMD MIDI file."""
    hits, t = [], 0.0
    for msg in mido.MidiFile(str(path)):
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and msg.note in EGMD_PIECE:
            hits.append((t, EGMD_PIECE[msg.note]))
    return hits


# ---------- MDB Drums ----------

def mdb_tracks(half):
    names = sorted(p.name.removesuffix("_subclass.txt") for p in (MDB / "annotations" / "subclass").glob("*.txt"))
    return [n for n in names if (n.removeprefix("MusicDelta_") in MDB_DEV) == (half == "dev")]


def mdb_hits(track):
    hits = []
    for line in (MDB / "annotations" / "subclass" / f"{track}_subclass.txt").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in MDB_PIECE:
            hits.append((float(parts[0]), MDB_PIECE[parts[1]]))
    return hits


def mdb_audio(track, version):
    if version == "drums only":
        return MDB / "audio" / "drum_only" / f"{track}_Drum.wav"
    return MDB_SEPARATED / version / f"{track}_drums.flac"


def separate_mdb(method):
    import evaluate_multi
    separate = evaluate_multi.load_method(method)
    for mix in sorted((MDB / "audio" / "full_mix").glob("*_MIX.wav")):
        dst = MDB_SEPARATED / method / mix.name.replace("_MIX.wav", "_drums.flac")
        if not dst.exists():
            start = time.perf_counter()
            evaluate_multi.save(dst, evaluate_multi.run(separate, load(mix))["drums"])
            print(f"  {dst.name}: {time.perf_counter() - start:.0f} s", flush=True)


# ---------- augmentation ----------

def augment(y, rng):
    """Random EQ (low / high shelf and a peak, +-6 dB), sometimes compression, room reverb, pitched bleed 20-35 dB
    down (bass and guitar left in a separated drum stem) and a 11-17 kHz low-pass (MP3)."""
    sr, n = drums.SR, len(y)
    f = np.fft.rfftfreq(n, 1 / sr) + 1.0
    low, high, peak = rng.uniform(80, 300), rng.uniform(3000, 9000), np.exp(rng.uniform(np.log(200), np.log(5000)))
    db = (rng.uniform(-6, 6) / (1 + (f / low) ** 2) + rng.uniform(-8, 5) / (1 + (high / f) ** 2)
          + rng.uniform(-6, 6) * np.exp(-(np.log2(f / peak) * rng.uniform(0.5, 2)) ** 2))
    y = np.fft.irfft(np.fft.rfft(y) * 10 ** (db / 20), n)
    if rng.random() < 0.6:  # 4:1 above a -24..-10 dB threshold, 5 ms attack, 80 ms release
        a, r = np.exp(-1 / (0.005 * sr)), np.exp(-1 / (0.08 * sr))
        env = signal.lfilter([1 - a], [1, -a], np.abs(y))
        over = 20 * np.log10(env / (env.max() + 1e-9) + 1e-9) - rng.uniform(-24, -10)
        y = y * 10 ** (signal.lfilter([1 - r], [1, -r], np.minimum(0, -0.75 * over)) / 20)
    if rng.random() < 0.6:
        rt60 = rng.uniform(0.2, 1.2)
        t = np.arange(int(rt60 * sr)) / sr
        ir = signal.lfilter([1], [1, -0.6], rng.standard_normal(len(t)) * np.exp(-6.9 * t / rt60))
        ir[:int(0.005 * sr)] *= np.linspace(0, 1, int(0.005 * sr))
        wet = signal.fftconvolve(y, ir)[:n]
        y = y + rng.uniform(0.05, 0.4) * wet * np.std(y) / (np.std(wet) + 1e-9)
    if rng.random() < 0.7:
        bleed, t0 = np.zeros(n), 0.0
        while t0 < n / sr - 0.1:
            m = int(min(rng.uniform(0.15, 0.8), n / sr - t0) * sr)
            t = np.arange(m) / sr
            f0 = 440 * 2 ** ((rng.integers(28, 76) - 69) / 12)
            tone = sum(np.sin(2 * np.pi * f0 * k * t) / k ** 1.3 for k in range(1, 12) if f0 * k < 16000)
            bleed[int(t0 * sr):int(t0 * sr) + m] += tone * np.exp(-t * rng.uniform(1, 6)) * np.minimum(t / 0.004, 1)
            t0 += m / sr
        y = y + 10 ** (-rng.uniform(20, 35) / 20) * np.std(y) / (np.std(bleed) + 1e-9) * bleed
    if rng.random() < 0.5:
        y = signal.lfilter(*signal.butter(6, rng.uniform(11000, 17000) / (sr / 2)), y)
    return (0.8 * y / (np.max(np.abs(y)) + 1e-9)).astype(np.float32)


# ---------- data ----------

def labelled(y, hits):
    """(features, labels candidates x PIECES) of one drum recording with known hits [(time_s, piece)]."""
    L = drums.log_spectrogram(y)
    frames = drums.candidates(L)
    t = frames / drums.FPS + drums.OFFSET_S
    Y = np.zeros((len(frames), len(drums.PIECES)), np.uint8)
    for when, piece in hits:
        if len(t):
            i = int(np.argmin(np.abs(t - when)))
            if abs(t[i] - when) <= MATCH_S:
                Y[i, drums.PIECES.index(piece)] = 1
    return drums.features(L, frames), Y


def mono(path):
    return load(path, sr=drums.SR).mean(axis=0)


def training_set():
    X, Y, rng = [], [], np.random.default_rng(SEED)
    files = egmd_files("train")
    start = time.perf_counter()
    for i, (wav, mid) in enumerate(files):
        y, hits = mono(wav), egmd_hits(mid)
        for version in range(1 + AUGMENT):
            x, lab = labelled(augment(y, rng) if version else y, hits)
            X.append(x)
            Y.append(lab)
        if (i + 1) % 50 == 0:
            print(f"  features: {i + 1} / {len(files)} files, {time.perf_counter() - start:.0f} s", flush=True)
    for _ in range(SYNTH_LOOPS):
        y, hits = drum_synth.random_loop(rng)
        x, lab = labelled(augment(y, rng) if rng.random() < 0.5 else y, hits)
        X.append(x)
        Y.append(lab)
    print(f"  features: {SYNTH_LOOPS} synthetic loops, {time.perf_counter() - start:.0f} s", flush=True)
    return np.concatenate(X), np.concatenate(Y)


def train():
    from sklearn.neural_network import MLPClassifier
    from sklearn.preprocessing import StandardScaler
    X, Y = training_set()
    print(f"training on {len(X)} candidates ({', '.join(f'{p} {n}' for p, n in zip(drums.PIECES, Y.sum(0)))})")
    scaler = StandardScaler().fit(X)
    start = time.perf_counter()
    clf = MLPClassifier(hidden_layer_sizes=HIDDEN, alpha=1e-3, max_iter=300, early_stopping=True,
                        random_state=SEED).fit(scaler.transform(X), Y)
    print(f"trained in {time.perf_counter() - start:.0f} s, {clf.n_iter_} epochs")
    layers = {}
    for i, (W, b) in enumerate(zip(clf.coefs_, clf.intercepts_)):
        layers[f"W{i}"], layers[f"b{i}"] = W.astype(np.float16), b.astype(np.float32)
    drums.MODEL.parent.mkdir(exist_ok=True)
    np.savez_compressed(drums.MODEL, layers=len(clf.coefs_), pieces=np.array(drums.PIECES),
                        mean=scaler.mean_.astype(np.float32), scale=scaler.scale_.astype(np.float32), **layers,
                        info=np.array("trained by experiments/train_drums.py on E-GMD v1.0.0 (CC BY 4.0) and "
                                      "drum_synth.py loops"))
    drums.model.cache_clear()
    gap = np.max(np.abs(drums.probabilities(X[:2000]) - clf.predict_proba(scaler.transform(X[:2000]))))
    print(f"wrote {drums.MODEL} ({drums.MODEL.stat().st_size // 1024} KB; float16 weights change the "
          f"probabilities by {gap:.4f} at most)")


# ---------- scoring ----------

def builtin(path):
    return drums.transcribe(mono(path))


def adtof(path):
    return drums.transcribe_adtof(path, mono(path), drums.adtof_command())


def score(recordings, title, transcribe=builtin):
    """recordings: [(audio path, [(time_s, piece)])]; transcribe(path) -> hits. Prints and returns {piece or
    group: F}."""
    found = Counter()
    for path, hits in recordings:
        est = transcribe(path)
        by_note = {n: p for p, n in drums.GM_NOTE.items()}
        est_t = {p: np.array([h[0] for h in est if by_note[h[2]] == p]) for p in drums.PIECES}
        ref_t = {p: np.array(sorted(t for t, q in hits if q == p)) for p in drums.PIECES}
        for name, pieces in [(p, (p,)) for p in drums.PIECES] + list(GROUPS.items()):
            r = np.sort(np.concatenate([ref_t[p] for p in pieces]))
            e = np.sort(np.concatenate([est_t[p] for p in pieces]))
            found[name, "ref"] += len(r)
            found[name, "est"] += len(e)
            found[name, "hit"] += len(match_events(r, e, MATCH_S)) if len(r) and len(e) else 0
    print(f"\n{title} ({len(recordings)} files)\n  {'piece':12} {'ref':>5} {'est':>5} {'P':>5} {'R':>5} {'F':>5}")
    out = {}
    for name in list(drums.PIECES) + list(GROUPS):
        ref, est, hit = found[name, "ref"], found[name, "est"], found[name, "hit"]
        p, r = hit / est if est else 0.0, hit / ref if ref else 0.0
        out[name] = 2 * p * r / (p + r) if hit else 0.0
        print(f"  {name:12} {ref:5d} {est:5d} {p:5.2f} {r:5.2f} {out[name]:5.2f}" if ref or est else f"  {name:12}  -")
    return out


def evaluate(transcribe=builtin):
    if transcribe is adtof and drums.adtof_command() is None:
        raise SystemExit("the ADTOF-pytorch plugin is not installed (see the README)")
    score([(w, egmd_hits(m)) for w, m in egmd_files("validation")], "E-GMD validation (other performances)",
          transcribe)
    score([(w, egmd_hits(m)) for w, m in egmd_files("test")], "E-GMD test (other performances)", transcribe)
    if not MDB.is_dir():
        print(f"\nno MDB Drums in {MDB} (git clone https://github.com/CarlSouthall/MDBDrums data/MDBDrums)")
        return
    versions = ["drums only"] + sorted(p.name for p in MDB_SEPARATED.iterdir() if p.is_dir()) \
        if MDB_SEPARATED.is_dir() else ["drums only"]
    for half in ("dev", "test"):
        for version in versions:
            tracks = [t for t in mdb_tracks(half) if mdb_audio(t, version).is_file()]
            score([(mdb_audio(t, version), mdb_hits(t)) for t in tracks], f"MDB Drums {half}, {version}",
                  transcribe)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--eval", action="store_true", help="score the model instead of training it")
    parser.add_argument("--separate", metavar="MODEL", help="split the MDB Drums mixes with methods_multi/MODEL.py")
    parser.add_argument("--adtof", action="store_true", help="with --eval: score the optional ADTOF-pytorch plugin")
    args = parser.parse_args()
    if args.separate:
        separate_mdb(args.separate)
    elif args.eval:
        evaluate(adtof if args.adtof else builtin)
    else:
        train()
        score([(w, egmd_hits(m)) for w, m in egmd_files("validation")], "E-GMD validation (other performances)")


if __name__ == "__main__":
    sys.exit(main())
