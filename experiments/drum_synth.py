"""Synthetic drum kits and grooves with known hits: test loops for experiments/tests/test_drums.py, and random
kits for part of the training data of experiments/train_drums.py (so the classifier does not only know the
sampled kits of E-GMD).

Pieces (drums.PIECES): kick (a sine with a falling pitch, a low thump and a beater click), snare (two damped modes
and band-passed noise), three toms (damped modes with a pitch drop), closed and open hi-hat (six detuned square
waves and noise, high-passed; an open hi-hat rings until the next hi-hat closes it), crash (noise and many
inharmonic partials) and ride (bell partials, a stick click and a quieter wash). The fixed kits' levels and
decays give about the band levels of real kicks, snares and hi-hats (the medians in E-GMD and MDB Drums): kick and
snare die down within an 8th, a snare's body is louder than its wires.
"""
import numpy as np
from scipy import signal

SR = 44100
PIECES = ("kick", "snare", "tom_hi", "tom_mid", "tom_lo", "hihat", "hihat_open", "crash", "ride")

KITS = {"a": dict(kick_hz=55, kick_decay=0.07, snare_hz=190, snare_decay=0.05, toms_hz=(220, 160, 110),
                  hat_decay=0.035, hat_cut=4000, open_decay=0.45, crash_decay=0.9, crash_cut=2500, ride_scale=1.0,
                  level=dict(kick=0.9, snare=0.7, tom_hi=0.6, tom_mid=0.6, tom_lo=0.65, hihat=0.45, hihat_open=0.35,
                             crash=0.35, ride=0.3)),
        "b": dict(kick_hz=48, kick_decay=0.1, snare_hz=230, snare_decay=0.07, toms_hz=(180, 135, 95),
                  hat_decay=0.05, hat_cut=4000, open_decay=0.45, crash_decay=1.3, crash_cut=2500, ride_scale=1.0,
                  level=dict(kick=0.9, snare=0.7, tom_hi=0.6, tom_mid=0.6, tom_lo=0.65, hihat=0.45, hihat_open=0.35,
                             crash=0.35, ride=0.3))}


def random_kit(rng):
    hi = rng.uniform(160, 260)
    return dict(kick_hz=rng.uniform(40, 70), kick_decay=rng.uniform(0.05, 0.3), snare_hz=rng.uniform(150, 280),
                snare_decay=rng.uniform(0.04, 0.15), toms_hz=(hi, hi * rng.uniform(0.68, 0.8), hi * rng.uniform(0.45, 0.6)),
                hat_decay=rng.uniform(0.02, 0.08), hat_cut=rng.uniform(2500, 7000), open_decay=rng.uniform(0.3, 0.7),
                crash_decay=rng.uniform(0.7, 2.0), crash_cut=rng.uniform(1500, 4000), ride_scale=rng.uniform(0.7, 1.3),
                level=dict(kick=rng.uniform(0.6, 1), snare=rng.uniform(0.5, 0.9), tom_hi=rng.uniform(0.4, 0.8),
                           tom_mid=rng.uniform(0.4, 0.8), tom_lo=rng.uniform(0.4, 0.8), hihat=rng.uniform(0.2, 0.6),
                           hihat_open=rng.uniform(0.2, 0.5), crash=rng.uniform(0.2, 0.5), ride=rng.uniform(0.2, 0.5)))


# ---------- sounds ----------

def env(n, decay, attack=0.001):
    t = np.arange(n) / SR
    return np.minimum(t / attack, 1) * np.exp(-t / decay)


def bandpass(x, lo, hi, order=2):
    return signal.sosfilt(signal.butter(order, [lo, hi], "bandpass", fs=SR, output="sos"), x)


def highpass(x, f, order=2):
    return signal.sosfilt(signal.butter(order, f, "highpass", fs=SR, output="sos"), x)


def kick(kit, rng):
    n = int(0.6 * SR)
    t = np.arange(n) / SR
    f = kit["kick_hz"] + 2.5 * kit["kick_hz"] * np.exp(-t / 0.025)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, kit["kick_decay"], 0.002)
    thump = bandpass(rng.standard_normal(n), 35, 120) * env(n, kit["kick_decay"] * 0.7, 0.002)
    beater = bandpass(rng.standard_normal(n), 300, 5000, order=1) * env(n, 0.012)  # beater and shell
    return body + 0.8 * thump + 0.3 * beater


def snare(kit, rng):
    n = int(0.4 * SR)
    t = np.arange(n) / SR
    body = (np.sin(2 * np.pi * kit["snare_hz"] * t) + 0.6 * np.sin(2 * np.pi * 1.65 * kit["snare_hz"] * t)) \
        * env(n, 0.06)
    wires = bandpass(rng.standard_normal(n), 300, 10000, order=1) * env(n, kit["snare_decay"])
    return body + 0.5 * wires


def tom(hz, rng):
    n = int(0.7 * SR)
    t = np.arange(n) / SR
    phase = 2 * np.pi * np.cumsum(hz * (1 + 0.15 * np.exp(-t / 0.05))) / SR
    body = (np.sin(phase) + 0.35 * np.sin(1.6 * phase)) * env(n, 0.35, 0.002)
    return body + 0.25 * bandpass(rng.standard_normal(n), 300, 5000, order=1) * env(n, 0.015)


def metal(seconds, decay, cutoff, rng, freqs=(205.3, 304.4, 369.6, 522.7, 540.0, 800.0)):
    """Six detuned square waves and noise, high-passed: the classic hi-hat and cymbal recipe."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    sq = sum(signal.square(2 * np.pi * 2.3 * f * t + rng.uniform(0, 6.3)) for f in freqs) / len(freqs)
    return highpass(0.6 * sq + 0.5 * rng.standard_normal(n), cutoff) * env(n, decay)


def crash(kit, rng):
    n = int(2.5 * SR)
    t = np.arange(n) / SR
    partials = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6.3)) for f in rng.uniform(400, 9000, 60)) / 8
    return highpass(partials + rng.standard_normal(n), kit["crash_cut"], order=4) * env(n, kit["crash_decay"], 0.003)


def ride(kit, rng):
    n = int(2.5 * SR)
    t = np.arange(n) / SR
    bell = sum(np.sin(2 * np.pi * f * kit["ride_scale"] * t) * np.exp(-t / 0.6)
               for f in (2650.0, 3720.0, 4980.0, 6110.0)) / 4
    wash = highpass(rng.standard_normal(n), 4000) * env(n, 0.4)
    tick = highpass(rng.standard_normal(n), 3000) * env(n, 0.012)
    return 0.3 * bell + 0.25 * wash + 1.2 * tick


def kit_sounds(kit, rng):
    return {"kick": kick(kit, rng), "snare": snare(kit, rng), "tom_hi": tom(kit["toms_hz"][0], rng),
            "tom_mid": tom(kit["toms_hz"][1], rng), "tom_lo": tom(kit["toms_hz"][2], rng),
            "hihat": metal(0.25, kit["hat_decay"], kit["hat_cut"], rng),
            "hihat_open": metal(1.0, kit["open_decay"], kit["hat_cut"] * 0.85, rng), "crash": crash(kit, rng),
            "ride": ride(kit, rng)}


# ---------- grooves ----------

def groove(rng, bpm, bars, start=1.0, style="mixed"):
    """[(time_s, piece, velocity 0-1)] of a drummer's groove, humanised (+-5 ms). style "mixed": phrases of 8 bars
    (2 rock, 2 funk, rock, a tom fill, ride, a bar with an open hi-hat on the last 8th) with a crash at the start
    and after the fill; "punk": 8th hi-hats with the kick on every beat; "random": each bar a random choice of
    hi-hat / ride figure, kick pattern, backbeat or half-time snare with ghost notes, fills and crashes."""
    beat = 60 / bpm
    hits = []

    def at(bar, pos, piece, vel=1.0):  # pos in 16ths within the bar
        hits.append((start + (bar * 16 + pos) * beat / 4, piece, vel))

    for bar in range(bars):
        if style == "random":
            random_bar(rng, lambda pos, piece, vel=1.0: at(bar, pos, piece, vel))
            continue
        part = "punk" if style == "punk" else ("rock", "rock", "funk", "funk", "rock", "fill", "ride", "open")[bar % 8]
        if bar % 8 in (0, 6):  # the start of a phrase, and after the fill
            at(bar, 0, "crash")
        if part in ("rock", "punk", "open"):
            for pos in range(0, 16, 2):
                if not (bar % 8 == 0 and pos == 0) and not (part == "open" and pos == 14):
                    at(bar, pos, "hihat", 0.9 if pos % 4 == 0 else 0.6)
            if part == "open":
                at(bar, 14, "hihat_open")
            for pos in (0, 8, 10) if part != "punk" else (0, 4, 8, 12):
                at(bar, pos, "kick")
            for pos in (4, 12):
                at(bar, pos, "snare")
        elif part == "funk":
            for pos in range(16):
                at(bar, pos, "hihat", 0.8 if pos % 2 == 0 else 0.5)
            for pos in (0, 3, 6, 10):
                at(bar, pos, "kick")
            for pos in (4, 12):
                at(bar, pos, "snare")
        elif part == "fill":
            for pos in range(16):
                at(bar, pos, ("snare", "tom_hi", "tom_mid", "tom_lo")[pos // 4], 0.9 if pos % 4 == 0 else 0.7)
            at(bar, 0, "kick")
        elif part == "ride":
            for pos in range(0, 16, 2):
                if pos or bar % 8 != 6:
                    at(bar, pos, "ride", 0.9 if pos % 4 == 0 else 0.6)
            for pos in (0, 8):
                at(bar, pos, "kick")
            for pos in (4, 12):
                at(bar, pos, "snare")
    at(bars, 0, "crash")  # and a crash on the downbeat after the last bar
    at(bars, 0, "kick")
    return sorted((t + rng.normal(0, 0.005), p, float(np.clip(v * rng.uniform(0.75, 1.0), 0.2, 1))) for t, p, v in hits)


def random_bar(rng, at):
    if rng.random() < 0.15:  # a fill: 8ths or 16ths over snare and toms
        step = rng.choice([1, 2])
        pieces = rng.choice(["snare", "tom_hi", "tom_mid", "tom_lo"], size=4, replace=True)
        for pos in range(0, 16, step):
            at(pos, pieces[pos // 4], rng.uniform(0.6, 1))
        at(0, "kick")
        return
    if rng.random() < 0.2:
        at(0, "crash")
    keeper = rng.choice(["hihat", "hihat", "ride"])
    step = rng.choice([1, 2, 2, 4])
    for pos in range(0, 16, step):
        if keeper == "hihat" and rng.random() < 0.1:
            at(pos, "hihat_open", rng.uniform(0.6, 1))
        elif not (pos == 0 and rng.random() < 0.2):
            at(pos, keeper, 0.9 if pos % 4 == 0 else rng.uniform(0.4, 0.8))
    for pos in sorted({0} | set(rng.choice(16, size=rng.integers(1, 5), replace=False))):
        if pos not in (4, 12) or rng.random() < 0.2:
            at(pos, "kick", rng.uniform(0.7, 1))
    for pos in (8,) if rng.random() < 0.2 else (4, 12):
        at(pos, "snare", rng.uniform(0.8, 1))
    for pos in rng.choice(16, size=rng.integers(0, 3), replace=False):  # ghost notes
        if pos not in (4, 8, 12):
            at(pos, "snare", rng.uniform(0.2, 0.35))


def render(hits, kit, seconds, rng, room=0.0, bleed_db=None):
    """Mono audio of the hits [(time_s, piece, velocity)]; room = reverb mix (0-1); bleed_db = quiet bass and
    guitar notes this far below the drums (what is left in a separated drum stem)."""
    variants = [kit_sounds(kit, rng) for _ in range(3)]  # a few takes of each piece, picked at random per hit
    y = np.zeros(int(seconds * SR))
    hat_times = np.array([t for t, p, _ in hits if p in ("hihat", "hihat_open")])
    for t, piece, vel in hits:
        s = variants[rng.integers(len(variants))][piece] * kit["level"][piece] * vel
        if piece == "hihat_open":  # rings until the next hi-hat closes it
            later = hat_times[hat_times > t + 0.01]
            if len(later):
                n = int((later[0] - t) * SR)
                s = s.copy()
                s[n:] *= np.exp(-np.arange(len(s) - n) / (0.01 * SR))
        i = int(t * SR)
        if 0 <= i < len(y):
            y[i:i + len(s)] += s[:len(y) - i]
    if room:
        n = int(0.6 * SR)
        ir = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.12 * SR))
        wet = signal.fftconvolve(y, ir)[:len(y)]
        y = y + room * wet * np.std(y) / np.std(wet)
    if bleed_db is not None:
        bleed = np.zeros(len(y))
        for t0 in np.arange(0.3, seconds - 1, 0.45):
            n = int(0.4 * SR)
            tt = np.arange(n) / SR
            f0 = 440 * 2 ** ((rng.integers(36, 70) - 69) / 12)
            bleed[int(t0 * SR):int(t0 * SR) + n] += sum(np.sin(2 * np.pi * f0 * k * tt) / k for k in range(1, 8)) \
                * np.exp(-tt * 3)
        y = y + 10 ** (-bleed_db / 20) * np.std(y) / np.std(bleed) * bleed
    return (0.8 * y / np.max(np.abs(y))).astype(np.float32)


def random_loop(rng):
    """(audio, hits [(time_s, piece)]) of a random kit playing random bars, 20-40 s, for training."""
    bpm = rng.uniform(70, 200)
    bars = max(4, int(rng.uniform(20, 40) / (4 * 60 / bpm)))
    hits = groove(rng, bpm, bars, start=rng.uniform(0.2, 1.0), style="random")
    seconds = hits[-1][0] + 2.5
    room = rng.uniform(0, 0.4) if rng.random() < 0.6 else 0.0
    bleed = rng.uniform(20, 35) if rng.random() < 0.5 else None
    return render(hits, random_kit(rng), seconds, rng, room, bleed), [(t, p) for t, p, _ in hits]
