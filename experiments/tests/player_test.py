"""Measure the practice player (experiments/build_player.py) in headless Chromium, in real time, with numbers.

    python experiments/tests/player_test.py                  # sync, timing, metronome, limiter, UI checks (~1 min)
    python experiments/tests/player_test.py --engine stems   # the same with one stretcher per stem
    python experiments/tests/player_test.py --browser webkit # the same in Safari's engine (see below)
    python experiments/tests/player_test.py --cpu            # + render cost and per-stem fidelity of both engines
    python experiments/tests/player_test.py --memory         # + memory, CPU and limiter with the real 6:45 song
    python experiments/tests/player_test.py --screens        # + screenshots (zh/en, 1280 and 390 px wide)

Needs the dev requirements (requirements-dev.txt) and Playwright's Chromium inside the project:
    PLAYWRIGHT_BROWSERS_PATH=.cache/ms-playwright .venv/bin/python -m playwright install chromium --only-shell
    (and `... playwright install webkit`, 78 MB download, for --browser webkit)
Writes test songs, their pages and the screenshots to outputs/_player_test/ (git-ignored); --cpu, --memory and
--screens also use the real songs in outputs/mp3. Audio is muted; an AudioWorklet in the page records every stem
after its fader (plus the metronome and the output), and the recordings are compared with the transport's
committed time map:
  sync   6 stems with the same clicks (MP3 and FLAC mixed): inter-stem offsets and a null test through seeks, a
         count-in, a speed change, speed-region edges, pitch changes, loop wraps with the speed trainer and a
         slider drag; each click's output time against the plan; metronome clicks against the beats
  loud   bass at -1 dBFS boosted +12 dB: output peak behind the limiter and the soft clipper
  bare   two stems, no analysis.json, no transcription: the page loads and plays; file:// shows the hint
  ui     mouse drags on the waveform (loop, speed region, snapped to bars; over a switched-off loop; a speed region
         inside a loop), removing the loop (✕ on the waveform, Esc, button), keys (also pressed quickly), solo,
         reload keeps settings, a count-in started between two beats stays in time
  ux     a shaky click seeks, drags end at the mouse, A / B keys, the loop readout, double-click clears the loop,
         focus (slider arrows, Enter in a number box), the trainer gives the speed back, 原调, speed-region hint,
         the language link keeps the song, the restored-settings notice and 本曲恢复默认
  --cpu  6 real stems rendered offline: CPU share, and magnitude-spectrogram fidelity of each stem (alone,
         soloed, and summed) against the time-warped original, which is fair to a phase vocoder
Exit code 1 if a check fails.
"""
import argparse
import contextlib
import functools
import http.server
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_player  # noqa: E402
from common import OUTPUTS, ROOT, save  # noqa: E402

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".cache" / "ms-playwright"))
from playwright.sync_api import sync_playwright  # noqa: E402

SR = 44100
TEST_ROOT = OUTPUTS / "_player_test"
SONGS = TEST_ROOT / "songs"
SCREENS = TEST_ROOT / "screens"
REAL = OUTPUTS / "mp3"
PORT = 8779
SYNC_SECONDS, BEAT = 40.0, 0.5           # clicks every 0.5 s (120 BPM), a downbeat every 4
CLICKS = np.arange(0.5, SYNC_SECONDS - 0.5, BEAT)
LIMITS = {"offset_ms": 1.0, "plan_ms": 5.0, "metro_ms": 5.0, "peak_dbfs": 0.0}


# ---------- test songs ---------------------------------------------------------------------------------------
def burst(n_ms=4.0, freq=2000.0):
    n = int(SR * n_ms / 1000)
    return np.hanning(n) * np.sin(2 * np.pi * freq * np.arange(n) / SR)


def click_track(seconds, times, amps):
    y = np.zeros(int(seconds * SR), np.float32)
    b = burst()
    for t, a in zip(times, amps):
        i = int(round(t * SR)) - len(b) // 2  # the burst is centred on t
        y[i:i + len(b)] += a * b
    return np.stack([y, y])


def midi_file(notes, bpm=120):
    """A tiny one-track standard MIDI file (only for the download link)."""
    tpq, ev = 480, []
    for s, e, p, v in notes:
        ev += [(int(s * bpm / 60 * tpq), 0x90, p, v), (int(e * bpm / 60 * tpq), 0x80, p, 0)]
    data, last = b"", 0
    for tick, st, p, v in sorted(ev):
        d, var = tick - last, b""
        var = bytes([d & 0x7F])
        d >>= 7
        while d:
            var = bytes([(d & 0x7F) | 0x80]) + var
            d >>= 7
        data += var + bytes([st, p, v])
        last = tick
    data += b"\x00\xff\x2f\x00"
    return b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") + (1).to_bytes(2, "big") + tpq.to_bytes(2, "big") \
        + b"MTrk" + len(data).to_bytes(4, "big") + data


def make_songs():
    sync = SONGS / "sync"
    amps = [0.8 if i % 4 == 0 else 0.5 for i in range(len(CLICKS))]
    y = click_track(SYNC_SECONDS, CLICKS, amps)
    for stem in ("vocals", "drums", "bass"):           # MP3 and FLAC mixed: both must decode to the same samples
        save(sync / f"{stem}.mp3", y)
    for stem in ("guitar", "piano", "other"):
        save(sync / f"{stem}.flac", y)
    chords = ["Em", "C", "G", "D/F#"]
    analysis = {
        "version": 2, "song": "sync", "duration_s": SYNC_SECONDS,
        "tempo": {"bpm": 120.0, "bpm_alternatives": [60.0, 240.0], "stable": True,
                  "tempo_curve": [[t, 120.0] for t in range(0, 40, 5)], "beats": CLICKS.tolist(),
                  "downbeats": CLICKS[::4].tolist(), "beats_per_bar": 4, "method": "synthetic"},
        "key": {"tonic": "E", "mode": "minor", "label_zh": "E 小调", "label_en": "E minor", "confidence": 0.42,
                "alternatives": [{"label_zh": "G 大调", "label_en": "G major", "score": 0.3}], "method": "synthetic",
                "by_method": {}},
        "chords": {"segments": [{"start": float(t), "end": float(t + 2), "label": chords[i % 4],
                                 "root": chords[i % 4][0], "quality": "min" if "m" in chords[i % 4] else "maj"}
                                for i, t in enumerate(CLICKS[::4])], "method": "synthetic"},
    }
    (sync / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    rng = np.random.default_rng(0)
    notes = [[float(t), float(t + 0.4), int(p), 90] for t, p in zip(CLICKS, rng.integers(40, 76, len(CLICKS)))]
    (sync / "transcription").mkdir(exist_ok=True)
    (sync / "transcription" / "guitar.notes.json").write_text(json.dumps(
        {"version": 1, "stem": "guitar", "notes": notes, "pitch_range": [40, 76], "method": "synthetic"}), encoding="utf-8")
    (sync / "transcription" / "guitar.mid").write_bytes(midi_file(notes))
    phrase = [[float(t), float(t + 0.4), int(p), 90] for t, p in zip(np.arange(2.0, 5.0, 0.5), (60, 62, 64, 65, 67, 69))]
    (sync / "transcription" / "vocals.notes.json").write_text(json.dumps(   # one phrase, then 35 s without notes
        {"version": 1, "stem": "vocals", "notes": phrase, "pitch_range": [60, 69], "method": "synthetic"}), encoding="utf-8")
    hits = [[float(t), float(t + 0.1), 42, 80] for t in np.arange(0.5, SYNC_SECONDS - 0.5, BEAT / 2)]
    hits += [[float(t), float(t + 0.1), 36 if i % 2 == 0 else 38, 110] for i, t in enumerate(CLICKS)]
    (sync / "transcription" / "drums.notes.json").write_text(json.dumps(
        {"version": 1, "stem": "drums", "notes": sorted(hits), "pitch_range": [36, 42], "method": "synthetic",
         "kit": {"36": "Kick", "38": "Snare", "42": "Hi-hat"}}), encoding="utf-8")

    loud = SONGS / "loud"
    t = np.arange(int(12 * SR)) / SR
    save(loud / "bass.flac", np.stack([0.891 * np.sin(2 * np.pi * 55 * t)] * 2))
    save(loud / "drums.flac", click_track(12, np.arange(0.25, 11.5, 0.5), [1.0] * 23) / 0.95 * 0.99)

    bare = SONGS / "bare"
    rng = np.random.default_rng(1)
    save(bare / "vocals.flac", 0.2 * np.stack([np.sin(2 * np.pi * 220 * t[:8 * SR])] * 2))
    save(bare / "other.flac", 0.05 * rng.standard_normal((2, 8 * SR)))


# ---------- server and browser -----------------------------------------------------------------------------
class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the test songs at / and the real outputs/mp3 library at /real/."""
    def translate_path(self, path):
        p = super().translate_path(path)
        rel = Path(p).relative_to(Path(self.directory)).as_posix()
        if rel == "real" or rel.startswith("real/"):
            return str(REAL / rel[5:])
        return p

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def serve(root, port=PORT):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        srv.shutdown()
        srv.server_close()


def open_page(browser, url, problems, viewport=(1280, 900)):
    page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.goto(url)
    page.wait_for_function("document.documentElement.dataset.ready === '1' || document.getElementById('status').classList.contains('err')",
                           timeout=180000)
    return page


# ---------- in-page recorder and analysis --------------------------------------------------------------------
RECORDER_JS = r"""
async (taps) => {
  const {ctx, engine} = window.player;
  const code = `class Rec extends AudioWorkletProcessor {
    constructor(o) { super(); this.n = o.processorOptions.n; this.on = false; this.buf = []; this.sent = 0;
      this.port.onmessage = e => { if (e.data === 'start') { this.on = true; this.first = currentFrame; }
        else { this.on = false; this.flush(); this.port.postMessage({done: true}); } }; }
    process(inputs) {
      if (!this.on) return true;
      const inp = inputs[0], block = [];
      for (let c = 0; c < this.n; c++) block.push(inp[c] ? inp[c].slice() : new Float32Array(128));
      this.buf.push(block);
      if (this.buf.length >= 32) this.flush();
      return true;
    }
    flush() {
      if (!this.buf.length) return;
      const len = this.buf.length * 128, out = [];
      for (let c = 0; c < this.n; c++) { const a = new Float32Array(len); this.buf.forEach((b, i) => a.set(b[c], i * 128)); out.push(a); }
      this.port.postMessage({first: this.first, data: out}, out.map(a => a.buffer)); this.buf = [];
    }
  }
  registerProcessor('rec', Rec);`;
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([code], {type: 'text/javascript'})));
  const n = taps.length;
  const node = new AudioWorkletNode(ctx, 'rec', {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
    channelCount: n, channelCountMode: 'explicit', channelInterpretation: 'discrete', processorOptions: {n}});
  const merger = new ChannelMergerNode(ctx, {numberOfInputs: n});
  taps.forEach((name, i) => {
    const src = name.startsWith('stem') ? engine.gains[+name.slice(4)] : engine[name];
    src.connect(merger, 0, i);
  });
  merger.connect(node); node.connect(new GainNode(ctx, {gain: 0})).connect(ctx.destination);
  const rec = window.rec = {chunks: [], first: null, done: null, node};
  node.port.onmessage = e => {
    if (e.data.done) { rec.done?.(); return; }
    if (rec.first === null) rec.first = e.data.first;
    rec.chunks.push(e.data.data);
  };
  rec.start = () => node.port.postMessage('start');
  rec.stop = () => new Promise(r => { rec.done = r; node.port.postMessage('stop'); });
  rec.join = () => {
    const len = rec.chunks.reduce((s, c) => s + c[0].length, 0);
    rec.ch = Array.from({length: n}, (_, k) => { const a = new Float32Array(len); let o = 0; for (const c of rec.chunks) { a.set(c[k], o); o += c[k].length; } return a; });
    rec.chunks = [];
    return len;
  };
}
"""

# The sync scenario runs inside the page so that browser timers, not Python round trips, set the timing.
SYNC_SCENARIO_JS = r"""
async () => {
  const {tr, ui, ctx} = window.player;
  const wait = s => new Promise(r => setTimeout(r, s * 1000));
  const marks = [];
  const mark = what => marks.push({what, t: ctx.currentTime, x: tr.position()});
  ui.setSpeedRegion(9, 12, 0.75); ui.setSpeedRegion(12, 15, 1.25); ui.setSpeedRegion(25.9, 28, 0.85); ui.afterRegionEdit();
  tr.metroOn = true; tr.countInOn = true;
  const clickTimes = []; tr.onclick = c => clickTimes.push(c.t);
  window.rec.start();
  tr.seek(2); mark('play'); await tr.play();
  const start = tr.log[tr.log.length - 1].output, countIn = clickTimes.filter(t => t < start - 1e-6).map(t => start - t);
  await wait(4.5);
  ui.setSpeed(0.9); mark('speed 0.9'); await wait(2.0);
  tr.seek(8.25); mark('seek 8.25'); await wait(3.0);
  document.getElementById('pitch-n').value = 3; ui.setPitch(); mark('pitch +3'); await wait(5.5);
  Object.assign(tr.trainer, {start: 0.8, step: 0.1, every: 1, target: 1.0});
  tr.loop = {on: false, a: 24, b: 26}; ui.setLoop(true); mark('loop 24-26');
  document.getElementById('trainer').checked = true; document.getElementById('trainer').onchange(); mark('trainer');
  await wait(9.0);
  document.getElementById('pitch-n').value = 0; ui.setPitch(); mark('pitch 0'); await wait(1.0);
  for (let k = 0; k < 12; k++) { ui.setSpeed(0.8 + 0.02 * k); await wait(0.06); }   // like dragging the slider
  mark('drag speed'); await wait(1.5);
  tr.pause(); mark('pause'); await wait(0.6);
  await window.rec.stop();
  return {marks, countIn, lead: tr.lead, latency: window.player.engine.latency, frames: window.rec.join(), first: window.rec.first,
          sampleRate: ctx.sampleRate, baseLatency: ctx.baseLatency, outputLatency: ctx.outputLatency};
}
"""

ANALYZE_JS = r"""
(args) => {
  const {tr, ctx} = window.player, R = window.rec, sr = ctx.sampleRate, ch = R.ch, n = ch[0].length;
  const log = tr.log.filter(s => !s.dropped).sort((a, b) => a.output - b.output);
  const toIdx = t => Math.round(t * sr) - R.first;
  const peakNear = (a, i, w) => { let best = -1, v = 0; for (let j = Math.max(0, i - w); j < Math.min(n, i + w); j++) { const x = Math.abs(a[j]); if (x > v) { v = x; best = j; } } return [best, v]; };
  // expected output time of every source click under the committed map; skip clicks inside seek/pause fades
  const guards = log.filter(s => s.jump || !s.active).map(s => [s.output - tr.lead - 0.02, s.output + 0.03]);
  const expected = [];
  log.forEach((s, k) => {
    if (!s.active) return;
    const end = log[k + 1]?.output ?? Infinity;
    for (const c of args.clicks) {
      const t = s.output + (c - s.input) / s.rate;
      if (t < s.output - 1e-9 || t >= end || guards.some(([a, b]) => t >= a && t < b)) continue;
      const i = toIdx(t); if (i < 2000 || i > n - 2000) continue;
      expected.push({t, i, c, rate: s.rate, semis: s.semis, kind: s.kind || (s.jump ? 'jump' : 'edit'), since: t - s.output});
    }
  });
  const W = Math.round(0.025 * sr), music = [], metro = [], offsets = [];
  let missing = 0;
  for (const e of expected) {
    const [pi, pv] = peakNear(ch[0], e.i, W);
    if (pv < 0.05) { missing++; continue; }
    music.push({...e, err: (pi - e.i) / sr * 1000});
    for (let k = 1; k < args.nStems; k++) { const [qi] = peakNear(ch[k], pi, Math.round(0.003 * sr)); offsets.push((qi - pi) / sr * 1000); }
    if (args.metro >= 0) { const [mi, mv] = peakNear(ch[args.metro], e.i, W); if (mv > 0.02) metro.push({err: (mi - e.i) / sr * 1000, vsMusic: (mi - pi) / sr * 1000, kind: e.kind, rate: e.rate}); }
  }
  // null test: stems of the same file type must come out identical (MP3 stems 0-2, FLAC stems 3-5)
  let peak0 = 0, resid = 0, cross = 0;
  for (let j = 0; j < n; j++) {
    peak0 = Math.max(peak0, Math.abs(ch[0][j]));
    for (const [k, ref] of [[1, 0], [2, 0], [4, 3], [5, 3]]) resid = Math.max(resid, Math.abs(ch[k][j] - ch[ref][j]));
    cross = Math.max(cross, Math.abs(ch[3][j] - ch[0][j]));
  }
  const dbr = v => v > 0 ? 20 * Math.log10(v / peak0) : -Infinity;
  const events = log.filter(s => s.kind === 'rate' || s.kind === 'wrap').map(s => ({kind: s.kind, output: s.output, input: s.input, rate: s.rate, late: s.late}));
  return {music, metro, offsets, missing, expected: expected.length, residualDb: dbr(resid), mp3VsFlacDb: dbr(cross),
          dropped: tr.log.filter(s => s.dropped).length,
          events, log: log.map(s => ({output: s.output, input: s.input, rate: s.rate, active: s.active, kind: s.kind || (s.jump ? 'jump' : ''), semis: s.semis}))};
}
"""

LOUD_SCENARIO_JS = r"""
async () => {
  const {tr, ui, ctx, engine} = window.player;
  const wait = s => new Promise(r => setTimeout(r, s * 1000));
  const bass = ui.stems.findIndex(s => s.name === 'bass');
  ui.stems[bass].db = 12; ui.applyGains();
  let minRed = 0;
  const poll = setInterval(() => { minRed = Math.min(minRed, engine.limiter.reduction); }, 20);
  window.rec.start(); await tr.play(); await wait(6); tr.pause(); await wait(0.4); await window.rec.stop();
  clearInterval(poll);
  window.rec.join();
  const peak = a => a.reduce((m, x) => Math.max(m, Math.abs(x)), 0);
  const [pre, lim, out] = window.rec.ch.map(peak);
  const p99 = a => { const s = Float32Array.from(a, Math.abs).sort(); return s[Math.floor(0.99 * (s.length - 1))]; };
  const knee = window.rec.ch[1].reduce((s, x) => s + (Math.abs(x) > 0.94 ? 1 : 0), 0) / window.rec.ch[1].length;
  const db = x => 20 * Math.log10(x);
  return {preLimiterDb: db(pre), afterLimiterDb: db(lim), afterLimiterP99Db: db(p99(window.rec.ch[1])), outputDb: db(out),
          clipperActiveShare: knee, minReductionDb: minRed};
}
"""

BARE_JS = r"""
async () => {
  const {tr, ctx} = window.player;
  const hidden = id => document.getElementById(id).hidden;
  const before = tr.position();
  await tr.play(); await new Promise(r => setTimeout(r, 2000)); const during = tr.position(); tr.pause();
  await new Promise(r => setTimeout(r, 400));
  return {advanced: during - before, tempoHidden: hidden('fact-tempo'), keyHidden: hidden('fact-key'),
          metroHidden: hidden('metro-box'), detailHidden: hidden('detail'), hintShown: !hidden('no-analysis'),
          stems: document.querySelectorAll('#stems .stem').length, midiLinks: document.querySelectorAll('#stems .midi a').length};
}
"""


def stats(values):
    a = np.abs(np.asarray(values, float))
    if not len(a):
        return {"n": 0}
    return {"n": int(len(a)), "median": round(float(np.median(a)), 3), "p95": round(float(np.percentile(a, 95)), 3),
            "max": round(float(a.max()), 3)}


def run_sync(browser, base, engine, problems):
    page = open_page(browser, f"{base}player.html?song=sync&engine={engine}", problems)
    page.evaluate(RECORDER_JS, ["stem0", "stem1", "stem2", "stem3", "stem4", "stem5", "metro", "clip"])
    info = page.evaluate(SYNC_SCENARIO_JS)
    res = page.evaluate(ANALYZE_JS, {"clicks": CLICKS.tolist(), "nStems": 6, "metro": 6})
    page.close()
    music = res["music"]
    by_kind = {}
    for m in music:
        by_kind.setdefault(m["kind"], []).append(m["err"])
    near_edges = [m["err"] for m in music if m["kind"] in ("rate", "wrap") and m["since"] < 1.0]
    rates = {round(m["rate"], 3) for m in music}
    trainer_rates = [round(e["rate"], 2) for e in res["events"] if e["kind"] == "wrap"]
    out = {
        "engine": engine, "stretch_latency_s": info["latency"], "schedule_lead_s": round(info["lead"], 3),
        "recorded_s": round(info["frames"] / SR, 1), "clicks_expected": res["expected"], "clicks_missing": res["missing"],
        "rates_heard": sorted(rates),
        "inter_stem_offset_ms": stats(res["offsets"]), "null_residual_same_format_db": round(res["residualDb"], 1),
        "mp3_vs_flac_difference_db": round(res["mp3VsFlacDb"], 1), "replaced_pending_changes": res["dropped"],
        "late_commits_ms": [round(e["late"] * 1000, 1) for e in res["events"] if e.get("late")],
        "music_vs_plan_ms": stats([m["err"] for m in music]),
        "music_vs_plan_signed_median_ms": round(float(np.median([m["err"] for m in music])), 3) if music else None,
        "music_vs_plan_by_event_ms": {k: stats(v) for k, v in by_kind.items()},
        "first_second_after_region_edge_or_wrap_ms": stats(near_edges),
        "metro_vs_plan_ms": stats([m["err"] for m in res["metro"]]),
        "metro_vs_music_ms": stats([m["vsMusic"] for m in res["metro"]]),
        "region_and_loop_events": len(res["events"]), "trainer_rates_at_wraps": trainer_rates,
        "pitch_values": sorted({round(s["semis"], 2) for s in res["log"]}),
        "count_in_clicks_before_start_s": [round(x, 3) for x in sorted(info["countIn"])],
    }
    checks = {
        "inter-stem offsets <= 1 ms": out["inter_stem_offset_ms"].get("max", 99) <= LIMITS["offset_ms"],
        "clicks heard where planned (<= 5 ms)": out["music_vs_plan_ms"].get("p95", 99) <= LIMITS["plan_ms"],
        "metronome on the beats (<= 5 ms)": out["metro_vs_plan_ms"].get("p95", 99) <= LIMITS["metro_ms"],
        "no click missing": res["missing"] == 0,
        "speed trainer climbs 0.9 -> 1.0": trainer_rates[:2] == [0.9, 1.0],
        "count-in: 4 clicks one beat apart": out["count_in_clicks_before_start_s"] == [0.5, 1.0, 1.5, 2.0],
        "regions heard at 0.75 and 1.25": {0.75, 1.25} <= rates,
        "null test: same-file stems identical": res["residualDb"] < -90,
    }
    return out, checks


def run_loud(browser, base, problems):
    page = open_page(browser, f"{base}player.html?song=loud", problems)
    page.evaluate(RECORDER_JS, ["out", "trim", "clip"])
    res = page.evaluate(LOUD_SCENARIO_JS)
    page.close()
    res = {k: round(v, 3) for k, v in res.items()}
    return res, {"output peak <= 0 dBFS with +12 dB boost": res["outputDb"] <= LIMITS["peak_dbfs"]}


def run_bare(browser, base, problems):
    page = open_page(browser, f"{base}player.en.html?song=bare", problems)
    res = page.evaluate(BARE_JS)
    page.close()
    ok = res["advanced"] > 1.5 and res["tempoHidden"] and res["metroHidden"] and res["detailHidden"] and res["hintShown"]
    page = browser.new_page()
    page.goto((SONGS / "player.html").resolve().as_uri())
    file_msg = page.is_visible("#filewarn")
    page.close()
    res["file_url_message"] = file_msg
    return res, {"page works without analysis/transcription": ok, "file:// shows the server hint": file_msg}


def run_ui(browser, base, problems):
    """Drive the page like a user: drag on the waveform, edit a region, keys, mute/solo, silence while paused."""
    page = open_page(browser, f"{base}player.html?song=sync", problems)
    box = page.locator("#wave").bounding_box()
    dur = page.evaluate("() => window.player.tr.duration")
    x_of = lambda t: box["x"] + t / dur * box["width"]  # noqa: E731
    y = box["y"] + box["height"] * 0.4

    def drag(a, b):
        page.mouse.move(x_of(a), y)
        page.mouse.down()
        for k in range(1, 11):
            page.mouse.move(x_of(a + (b - a) * k / 10), y)
        page.mouse.up()
        page.wait_for_timeout(300)
    state = "() => { const tr = window.player.tr; return {loop: tr.loop, regions: tr.regions, speed: tr.speed, playing: tr.playing, x: tr.position()}; }"
    res = {}
    drag(5.2, 9.3)                                    # default drag mode: A-B loop, snapped to bars
    res["loop_after_drag"] = page.evaluate(state)["loop"]
    res["x_after_loop_drag"] = round(page.evaluate(state)["x"], 2)  # the playhead goes to A
    page.check("input[name=drag][value=speed]")
    drag(12.4, 16.7)                                  # a speed region, snapped to bars, 80 % by default
    res["regions_after_drag"] = page.evaluate(state)["regions"]
    page.fill("#regionlist input[type=number]", "60")
    page.dispatch_event("#regionlist input[type=number]", "change")
    res["region_speed_after_edit"] = page.evaluate(state)["regions"][0]["speed"]
    page.evaluate("() => window.scrollTo(0, 0)")      # fill() scrolled the list into view
    box.update(page.locator("#wave").bounding_box())
    page.mouse.click(x_of(3.0), y)                    # click on the waveform = seek
    res["after_click"] = round(page.evaluate(state)["x"], 2)
    page.keyboard.press("ArrowRight")                 # next bar
    res["after_arrow_right"] = round(page.evaluate(state)["x"], 2)
    page.keyboard.press("]")
    res["speed_after_bracket"] = page.evaluate(state)["speed"]
    page.keyboard.press("[")
    page.keyboard.press("l")
    res["loop_on_after_l"] = page.evaluate(state)["loop"]["on"]
    page.keyboard.press(" ")
    page.wait_for_timeout(1200)
    res["playing_after_space"] = page.evaluate(state)["playing"]
    x0 = page.evaluate(state)["x"]
    for _ in range(3):                                # quick presses: each one counts, also while a seek waits
        page.keyboard.press("ArrowRight")
    res["bars_after_3_quick_arrows"] = round(page.evaluate(state)["x"] - (4.5 + 2 * ((x0 - 4.5) // 2)), 2) / 2
    page.click("#stems .stem:nth-child(4) .s")        # solo guitar
    res["silent_rows_with_solo"] = page.evaluate("() => document.querySelectorAll('#stems .stem.silent').length")
    page.click("#stems .stem:nth-child(4) .s")
    page.keyboard.press(" ")
    page.wait_for_timeout(800)
    res["playing_after_second_space"] = page.evaluate(state)["playing"]
    page.evaluate(RECORDER_JS, ["clip"])
    res["paused_output_peak"] = page.evaluate("""async () => { window.rec.start(); await new Promise(r => setTimeout(r, 1000));
        await window.rec.stop(); window.rec.join(); return window.rec.ch[0].reduce((m, x) => Math.max(m, Math.abs(x)), 0); }""")
    res["limiter_badge_when_paused"] = page.evaluate("() => document.getElementById('limiter').classList.contains('active')")
    page.evaluate("() => { const r = document.getElementById('roll'); r.value = 'drums'; r.onchange(); }")
    page.wait_for_timeout(300)
    res["drum_lane_rows"] = page.evaluate("() => document.getElementById('detail').dataset.lane")
    page.evaluate("""() => { const z = document.getElementById('zoom'); z.value = 8; z.oninput();
        const r = document.getElementById('roll'); r.value = ''; r.onchange(); r.value = 'guitar'; r.onchange(); }""")
    page.wait_for_timeout(700)                          # settings are saved 300 ms after a change
    page.reload()
    page.wait_for_function("document.documentElement.dataset.ready === '1'")
    res["after_reload"] = page.evaluate(state)
    # the loop can be removed, and a loop that is off (or one in the other drag mode) does not catch the mouse
    page.evaluate("() => window.scrollTo(0, 0)")
    box.update(page.locator("#wave").bounding_box())
    drag(5.2, 7.3)                                    # over the switched-off loop 4.5-8.5: a new loop, not a move
    res["loop_over_old_one"] = page.evaluate(state)["loop"]
    page.check("input[name=drag][value=speed]")
    drag(4.8, 6.2)                                    # inside the loop: a speed region, the loop stays
    res["speed_inside_loop"] = page.evaluate(state)
    page.check("input[name=drag][value=loop]")
    page.locator("#wave [part~=loop] button").click()  # the ✕ on the waveform
    res["loop_after_wave_x"] = page.evaluate(state)["loop"]
    res["clear_button_disabled"] = page.evaluate("() => document.getElementById('loop-clear').disabled")
    page.keyboard.press("l")                          # a new two-bar loop from here, then Esc removes it
    res["loop_after_l"] = page.evaluate(state)["loop"]
    page.keyboard.press("Escape")
    res["loop_after_esc"] = page.evaluate(state)["loop"]
    page.keyboard.press("l")
    page.click("#loop-clear")
    res["loop_after_clear_button"] = page.evaluate(state)["loop"]
    page.locator("#wave [part~=speed-0] button").click()  # a speed region's ✕ works in either drag mode
    res["regions_after_wave_x"] = page.evaluate(state)["regions"]
    # a count-in from between two beats (3.25 s) must stay in time: the music's next beat (3.5 s) comes one beat
    # (0.5 s) after the last count-in click
    res["count_in_offbeat_gap_s"] = page.evaluate("""async () => { const tr = window.player.tr, clicks = [];
        tr.onclick = c => clicks.push(c.t); tr.countInOn = true; tr.seek(3.25); await tr.play();
        const s = tr.log[tr.log.length - 1]; tr.pause(); tr.countInOn = false; tr.onclick = null;
        await new Promise(r => setTimeout(r, 400));
        return Math.round((s.output + (3.5 - s.input) / s.rate - Math.max(...clicks)) * 1e4) / 1e4; }""")
    page.close()
    regions = res["regions_after_drag"]
    checks = {
        "drag sets a bar-snapped A-B loop": res["loop_after_drag"] == {"on": True, "a": 4.5, "b": 8.5}
        and res["x_after_loop_drag"] == 4.5,
        "3 quick arrow presses jump 3 bars": res["bars_after_3_quick_arrows"] == 3,
        "count-in from between beats stays in time": abs(res["count_in_offbeat_gap_s"] - 0.5) < 1e-3,
        "drag adds a bar-snapped speed region": len(regions) == 1 and (regions[0]["start"], regions[0]["end"]) == (12.5, 16.5),
        "region speed can be edited": res["region_speed_after_edit"] == 0.6,
        "click seeks, arrow jumps a bar": abs(res["after_click"] - 3.0) < 0.1 and res["after_arrow_right"] == 4.5,
        "keys: ] speed, L loop, space play/pause": res["speed_after_bracket"] == 1.05 and res["loop_on_after_l"] is False
        and res["playing_after_space"] and not res["playing_after_second_space"],
        "solo silences the other 5 rows": res["silent_rows_with_solo"] == 5,
        "drum lane: hi-hat, snare, kick rows, top to bottom": res["drum_lane_rows"] == "42 38 36",
        "silent while paused": res["paused_output_peak"] < 1e-4,
        "loop and speed regions come back after a reload": res["after_reload"]["loop"]["a"] == 4.5
        and res["after_reload"]["regions"] == [{"start": 12.5, "end": 16.5, "speed": 0.6}],
        "a drag over a switched-off loop draws a new loop": res["loop_over_old_one"] == {"on": True, "a": 4.5, "b": 6.5},
        "speed mode: a drag inside the loop adds a speed region": res["speed_inside_loop"]["loop"] == {"on": True, "a": 4.5, "b": 6.5}
        and {"start": 4.5, "end": 6.5, "speed": 0.8} in res["speed_inside_loop"]["regions"],
        "the loop's ✕, Esc and the clear button remove the loop": all(
            res[k] == {"on": False, "a": 0, "b": 0} for k in ("loop_after_wave_x", "loop_after_esc", "loop_after_clear_button"))
        and res["clear_button_disabled"] and res["loop_after_l"]["on"],
        "a speed region's ✕ removes it": res["regions_after_wave_x"] == [{"start": 12.5, "end": 16.5, "speed": 0.6}],
    }
    return res, checks


# ---------- CPU and cross-stem effects (offline rendering of real stems) -----------------------------------------
CPU_JS = r"""
async ({urls, start, seconds, rate}) => {
  const lib = window.playerLib, sr = 44100, len = Math.round(seconds * sr);
  const tmp = new AudioContext({sampleRate: sr}), stems = [];
  for (const u of urls) {          // decode, keep only the excerpt
    const b = await tmp.decodeAudioData(await (await fetch(u)).arrayBuffer());
    const s0 = Math.round(start * sr), n = len + sr;
    stems.push([0, 1].map(c => b.getChannelData(Math.min(c, b.numberOfChannels - 1)).slice(s0, s0 + n)));
  }
  await tmp.close();
  const b64 = f => { const u = new Uint8Array(f.buffer, f.byteOffset, f.byteLength); let s = ''; for (let i = 0; i < u.length; i += 32768) s += String.fromCharCode.apply(null, u.subarray(i, i + 32768)); return btoa(s); };
  async function render(mode, mix, solo = -1) {   // mix: the sum of the stems on one stereo node
    const N = mix ? 1 : stems.length;
    const octx = new OfflineAudioContext({numberOfChannels: 2 * N, length: len, sampleRate: sr});
    const eng = await lib.createEngine(octx, N, mode);
    eng.clip.disconnect();
    const merger = new ChannelMergerNode(octx, {numberOfInputs: 2 * N});
    eng.gains.forEach((g, i) => { const sp = new ChannelSplitterNode(octx, {numberOfOutputs: 2}); g.connect(sp); sp.connect(merger, 0, 2 * i); sp.connect(merger, 1, 2 * i + 1); });
    merger.connect(octx.destination);
    const data = mix ? [[0, 1].map(c => { const a = new Float32Array(stems[0][0].length); for (const s of stems) for (let j = 0; j < a.length; j++) a[j] += s[c][j]; return a; })]
                     : stems.map(s => s.map(a => a.slice()));
    if (mode === 'multi') await eng.nodes[0].addBuffers(data.flat());
    else for (let i = 0; i < N; i++) await eng.nodes[i].addBuffers(data[i]);
    if (solo >= 0) eng.setAudible(stems.map((_, i) => i === solo));
    for (const node of eng.nodes) await node.schedule({output: 0, input: 0, rate, active: true});
    const t0 = performance.now(), out = await octx.startRendering(), ms = performance.now() - t0;
    const which = solo >= 0 ? [solo] : Array.from({length: N}, (_, i) => i);
    return {share: ms / 1000 / seconds, left: which.map(i => b64(out.getChannelData(2 * i)))};
  }
  const stemsR = await render('stems', false), multiR = await render('multi', false), mixR = await render('stems', true);
  const soloR = [];
  for (let i = 0; i < stems.length; i++) soloR.push((await render('multi', false, i)).left[0]);
  return {stems: stemsR, multi: multiR, mix: mixR, multiSolo: soloR};
}
"""


def magnitude_snr(out, ref_audio, rate, skip_s=1.0, n_fft=2048, hop=512):
    """Phase-insensitive fidelity of a time-stretch: compare |STFT| of the output at output time t with |STFT| of
    the original at input time rate * t. dB, higher is better (a phase vocoder cannot be judged sample by sample)."""
    win = np.hanning(n_fft)
    centres = np.arange(int(skip_s * SR), len(out) - n_fft, hop)
    def frames(x, idx):
        idx = np.clip(idx, n_fft // 2, len(x) - n_fft // 2 - 1).astype(int)
        return np.abs(np.fft.rfft(np.stack([x[i - n_fft // 2:i + n_fft // 2] for i in idx]) * win, axis=1))
    a = frames(out, centres)
    b = frames(ref_audio, np.round(centres * rate))
    return float(10 * np.log10(np.sum(b ** 2) / np.sum((a - b) ** 2)))


def run_cpu(browser, base, problems, song="Carsick Cars - 中南海", start=120, seconds=12):
    """Offline render cost of 6 real stems, and how faithfully each stem is stretched by either engine."""
    import base64
    import soundfile as sf
    names = build_player.STEM_ORDER
    urls = [f"/real/{quote(song)}/{s}.mp3" for s in names]
    originals = {}
    for s in names:
        y, _ = sf.read(str(REAL / song / f"{s}.mp3"), dtype="float32", start=start * SR, frames=(seconds + 1) * SR, always_2d=True)
        originals[s] = y[:, 0]
    mix = sum(originals.values())
    page = open_page(browser, f"{base}player.en.html?song=bare", problems)
    res = {}
    for rate in (1.0, 0.75):
        r = page.evaluate(CPU_JS, {"urls": urls, "start": start, "seconds": seconds, "rate": rate})
        dec = lambda b: np.frombuffer(base64.b64decode(b), np.float32)  # noqa: E731
        row = {"cpu_share_of_one_core": {k: round(r[k]["share"], 3) for k in ("stems", "multi", "mix")}}
        for mode in ("stems", "multi"):
            outs = [dec(b) for b in r[mode]["left"]]
            row[f"{mode}_stem_fidelity_db"] = {s: round(magnitude_snr(o, originals[s], rate), 1) for s, o in zip(names, outs)}
            row[f"{mode}_sum_fidelity_db"] = round(magnitude_snr(sum(outs), mix, rate), 1)
        row["multi_masked_solo_fidelity_db"] = {s: round(magnitude_snr(dec(b), originals[s], rate), 1)
                                                for s, b in zip(names, r["multiSolo"])}
        row["mix_on_one_node_fidelity_db"] = round(magnitude_snr(dec(r["mix"]["left"][0]), mix, rate), 1)
        res[f"rate_{rate}"] = row
    page.close()
    return res


# ---------- memory and real-time CPU with the real 6:45 song -------------------------------------------------
def browser_processes():
    """(pid, rss MB, cpu seconds, command) for Playwright's Chromium processes; macOS/Linux only."""
    out = subprocess.run(["ps", "-axo", "pid=,rss=,time=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        if "ms-playwright" not in line:
            continue
        pid, rss, cpu, cmd = line.split(None, 3)
        parts = [float(p) for p in cpu.replace("-", ":").split(":")]
        secs = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
        rows.append((int(pid), int(rss) / 1024, secs, cmd))
    return rows


REAL_LIMITER_JS = r"""
async ({seconds, bassDb}) => {
  const {tr, ui, engine} = window.player;
  const bass = ui.stems.findIndex(s => s.name === 'bass');
  ui.stems[bass].db = bassDb; ui.applyGains();
  let minRed = 0;
  const poll = setInterval(() => { minRed = Math.min(minRed, engine.limiter.reduction); }, 20);
  window.rec.chunks = []; window.rec.first = null;
  window.rec.start(); await new Promise(r => setTimeout(r, seconds * 1000)); await window.rec.stop();
  clearInterval(poll);
  window.rec.join();
  const [lim, out] = window.rec.ch;
  const peak = a => a.reduce((m, x) => Math.max(m, Math.abs(x)), 0), db = x => 20 * Math.log10(x);
  const over = lim.reduce((n, x) => n + (Math.abs(x) > 0.94 ? 1 : 0), 0) / lim.length;
  return {bassDb, afterLimiterPeakDb: db(peak(lim)), outputPeakDb: db(peak(out)), clipperActiveShare: over, minReductionDb: minRed};
}
"""


def run_memory(browser, base, problems):
    """Load the 6:45 song (6 stems), play it, and watch the browser's memory and CPU (ps: macOS/Linux)."""
    if platform.system() == "Windows":
        return {"skipped": "uses ps (macOS/Linux)"}
    song = "Carsick Cars - 中南海"
    samples, stop = [], threading.Event()

    def poll():
        while not stop.is_set():
            rows = browser_processes()
            samples.append((sum(r[1] for r in rows), max((r[1] for r in rows), default=0)))
            stop.wait(0.2)
    idle = browser_processes()
    th = threading.Thread(target=poll, daemon=True)
    th.start()
    t0 = time.time()
    page = open_page(browser, f"{base}real/player.html?song={quote(song)}", problems)
    load_s = time.time() - t0
    after_load = browser_processes()
    page.evaluate(RECORDER_JS, ["trim", "clip"])
    page.evaluate("async () => { await window.player.tr.seek(60); await window.player.tr.play(); }")
    cpu0 = {r[0]: r[2] for r in browser_processes()}
    w0 = time.time()
    unity = page.evaluate(REAL_LIMITER_JS, {"seconds": 10, "bassDb": 0})
    page.evaluate("() => window.player.ui.setSpeed(0.8)")
    boosted = page.evaluate(REAL_LIMITER_JS, {"seconds": 10, "bassDb": 12})
    cpu1 = browser_processes()
    wall = time.time() - w0
    pos = page.evaluate("() => window.player.tr.position()")
    page.evaluate("() => window.player.tr.pause()")
    stop.set()
    th.join()
    page.close()
    busiest = max(cpu1, key=lambda r: r[2] - cpu0.get(r[0], r[2]))
    rnd = lambda d: {k: round(v, 3) if isinstance(v, float) else v for k, v in d.items()}  # noqa: E731
    return {"song": song, "load_and_decode_s": round(load_s, 1),
            "browser_rss_idle_mb": round(sum(r[1] for r in idle)),
            "browser_rss_after_load_mb": round(sum(r[1] for r in after_load)),
            "peak_browser_rss_mb": round(max(s[0] for s in samples)),
            "peak_single_process_rss_mb": round(max(s[1] for s in samples)),
            "playback_cpu_of_busiest_process": round((busiest[2] - cpu0.get(busiest[0], busiest[2])) / wall, 3),
            "song_position_after_play": round(pos, 2), "limiter_unity": rnd(unity), "limiter_bass_plus_12db": rnd(boosted)}


def run_ux(browser, base, problems):
    """The usability fixes: clicks vs drags, A/B keys, focus, trainer, resets, readouts (fresh page, bars every 2 s)."""
    page = open_page(browser, f"{base}player.html?song=sync", problems)
    page.on("dialog", lambda d: d.accept())
    box = page.locator("#wave").bounding_box()
    dur = page.evaluate("() => window.player.tr.duration")
    x_of = lambda t: box["x"] + t / dur * box["width"]  # noqa: E731
    y = box["y"] + box["height"] * 0.4
    state = "() => { const tr = window.player.tr; return {loop: tr.loop, regions: tr.regions, speed: tr.speed, semis: tr.semis, x: tr.position()}; }"
    js = page.evaluate
    r2 = lambda v: round(v, 2)  # noqa: E731
    no_loop = {"on": False, "a": 0, "b": 0}
    res = {}

    def press_move(t, dx, steps=4):
        page.mouse.move(x_of(t), y)
        page.mouse.down()
        for k in range(1, steps + 1):
            page.mouse.move(x_of(t) + dx * k / steps, y)
        page.mouse.up()
        page.wait_for_timeout(300)

    def blur():                                          # a select changed without the mouse keeps the focus
        js("() => document.activeElement.blur()")
    heights = {"fresh": js("() => document.getElementById('transport').offsetHeight")}
    press_move(20, 8)                                   # below the 10 px drag threshold: a click
    st = js(state)
    res["shaky_click"] = {"loop": st["loop"], "x": r2(st["x"])}
    press_move(30, 11)                                  # a drag, but under 12 px once corrected: still a click
    st = js(state)
    res["shaky_drag"] = {"loop": st["loop"], "x": r2(st["x"])}
    page.select_option("#snap", "off")
    blur()
    press_move(10, x_of(20) - x_of(10), steps=12)       # unsnapped drag 10 -> 20 s: the loop ends where the mouse did
    lp = js(state)["loop"]
    res["unsnapped_drag"] = [r2(lp["a"]), r2(lp["b"])]
    press_move(36, x_of(30) - x_of(36), steps=12)       # and a leftward one, 36 -> 30 s
    lp = js(state)["loop"]
    res["leftward_drag"] = [r2(lp["a"]), r2(lp["b"])]
    page.mouse.move(x_of(20), y)                         # one that turns back past its start: 20 -> 23 -> 17 s
    page.mouse.down()
    for t in (20.5, 21.5, 23, 21.5, 20, 18.5, 17):
        page.mouse.move(x_of(t), y)
    page.mouse.up()
    page.wait_for_timeout(300)
    lp = js(state)["loop"]
    res["turned_drag"] = [r2(lp["a"]), r2(lp["b"])]
    page.keyboard.press("Escape")
    page.select_option("#snap", "bar")
    blur()
    js("() => window.player.tr.seek(10.2)")
    page.keyboard.press("a")                             # A at 10.2 s -> bar line 10.5, B two bars later
    res["after_a"] = js(state)["loop"]
    js("() => window.player.tr.seek(13.1)")
    page.keyboard.press("b")                             # B at 13.1 s -> 12.5, and the playhead goes back to A
    page.wait_for_timeout(200)                           # the readouts follow on the next frame
    st = js(state)
    res["after_b"] = {"loop": st["loop"], "x": r2(st["x"])}
    res["loop_info"] = page.inner_text("#loop-info")
    heights["loop"] = js("() => document.getElementById('transport').offsetHeight")
    js("() => window.player.tr.seek(30)")
    page.wait_for_timeout(200)
    res["loop_info_outside"] = page.inner_text("#loop-info")
    heights["outside"] = js("() => document.getElementById('transport').offsetHeight")
    # a shaky click on the live loop seeks and leaves the loop where it is
    press_move(11.5, 5)
    st = js(state)
    res["shaky_click_on_loop"] = {"loop": st["loop"], "x": r2(st["x"])}
    # Esc on a focused control only leaves it; the next Esc clears the loop
    page.focus("#speed")
    page.keyboard.press("Escape")
    res["esc_leaves_slider"] = {"focus": js("() => document.activeElement.tagName"), "loop": js(state)["loop"]}
    page.locator("#wave [part~=loop]").dblclick()         # double-click on the loop removes it
    res["after_dblclick"] = js(state)["loop"]
    js("() => window.player.tr.seek(39.8)")
    page.keyboard.press("a")                             # A at the very end still makes a loop
    res["a_at_end"] = js(state)["loop"]
    page.keyboard.press("Escape")
    # keyboard: a slider reached with Tab keeps its arrow keys; Enter in a number box gives the keys back
    x0 = js(state)["x"]
    page.focus("#speed")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    st = js(state)
    res["slider_keys"] = {"speed": st["speed"], "moved": r2(st["x"] - x0)}
    page.keyboard.press("Escape")
    page.click("#speed-n", click_count=3)
    page.keyboard.type("85")
    page.keyboard.press("Enter")
    res["focus_after_enter"] = js("() => document.activeElement.tagName")
    page.keyboard.press(" ")
    page.wait_for_timeout(400)
    res["space_after_enter"] = js("() => window.player.tr.playing")
    page.keyboard.press(" ")
    page.wait_for_timeout(300)
    page.click("#speed-n", click_count=3)
    page.keyboard.type("55")
    page.keyboard.press("Escape")                        # Esc cancels what was typed
    res["esc_cancels_typing"] = js(state)["speed"]
    page.dispatch_event("#speed", "dblclick")            # double-click: back to 100 %
    res["speed_after_dblclick"] = js(state)["speed"]
    page.focus("#snap")
    page.keyboard.press("Escape")                        # Esc leaves a select
    res["esc_leaves_select"] = js("() => document.activeElement.tagName")
    # Space after clicking a download link plays and does not scroll the page
    js("""() => document.querySelectorAll('a[download]').forEach(a => a.addEventListener('click', e => e.preventDefault()))""")
    page.click("#roll-midi a")
    js("() => window.scrollTo(0, 0)")
    page.keyboard.press(" ")
    page.wait_for_timeout(300)
    res["space_after_link"] = {"playing": js("() => window.player.tr.playing"), "scroll": js("() => scrollY")}
    page.keyboard.press(" ")
    page.wait_for_timeout(300)
    # speed trainer: it gives the speed back when switched off (box, L, Esc), refuses start >= target, counts passes
    js("() => { const tr = window.player.tr; window.player.ui.setSpeed(0.9); tr.trainer.start = 0.6; tr.trainer.target = 0.8; }")
    page.check("#trainer")
    res["trainer_on"] = {"speed": js(state)["speed"], "status": page.inner_text("#trainer-status")}
    page.uncheck("#trainer")
    res["speed_after_trainer"] = js(state)["speed"]
    for key in ("l", "Escape"):
        page.check("#trainer")
        page.keyboard.press(key)
        res[f"trainer_after_{key}"] = {"speed": js(state)["speed"], "checked": page.is_checked("#trainer")}
    for start, target in ((0.9, 0.7), (0.8, 0.8)):
        js(f"() => {{ const tr = window.player.tr; tr.trainer.start = {start}; tr.trainer.target = {target}; }}")
        page.click("#trainer")
        res[f"trainer_bad_{start}_{target}"] = {"checked": page.is_checked("#trainer"), "status": page.inner_text("#trainer-status")}
    js("""() => { const tr = window.player.tr; tr.trainer.start = 0.6; tr.trainer.target = 0.8; tr.trainer.step = 0.05;
        tr.trainer.every = 1; tr.loop = {on: true, a: 10.5, b: 12.5}; window.player.ui.drawRegions(); }""")
    page.check("#trainer")
    js("() => { const tr = window.player.tr; tr.seek(10.5); tr.play(); }")
    page.wait_for_timeout(4200)                          # one pass of a 2 s bar at 60 % takes 3.3 s
    res["trainer_pass_2"] = {"speed": js(state)["speed"], "status": page.inner_text("#trainer-status")}
    js("() => window.player.tr.pause()")
    page.uncheck("#trainer")
    page.keyboard.press("Escape")
    # pitch: 原调 resets it; the transport chips say what is changed
    js("() => { document.getElementById('pitch-n').value = -2; window.player.ui.setPitch(); }")
    page.wait_for_timeout(200)
    res["chips"] = page.inner_text("#chips")
    heights["chips"] = js("() => document.getElementById('transport').offsetHeight")
    page.click("#pitch-reset")
    res["semis_after_reset"] = js(state)["semis"]
    # a speed region under the playhead is explained, and its row is lit
    js("""() => { const u = window.player.ui; u.setSpeedRegion(16.5, 18.5, 0.8); u.setSpeedRegion(20.5, 24.5, 0.7);
        u.afterRegionEdit(); window.player.tr.seek(22); }""")
    page.wait_for_timeout(300)
    res["speed_note"] = js("() => document.getElementById('speed-note').textContent")
    res["region_row_lit"] = js("() => [...document.querySelectorAll('#regionlist .item')].findIndex(r => r.classList.contains('now'))")
    res["lang_link"] = js("() => document.querySelector('header .lang a').getAttribute('href')")
    # before the first beat the position card shows no beat
    js("() => window.player.tr.seek(0.1)")
    page.wait_for_timeout(200)
    res["bar_before_first_beat"] = page.inner_text("#f-bar")
    # a seek during the count-in counts in again from the new spot
    js("() => { const tr = window.player.tr; tr.countInOn = true; tr.seek(10.5); tr.play(); }")
    page.wait_for_timeout(400)
    first = js("() => window.player.tr.countInfo && window.player.tr.countInfo.from")
    js("() => window.player.tr.seek(14.5)")
    page.wait_for_timeout(400)
    res["count_in_again"] = {"first": first, "second": js("() => window.player.tr.countInfo && window.player.tr.countInfo.from")}
    js("() => { const tr = window.player.tr; tr.pause(); tr.countInOn = false; }")
    # the piano roll's pitch axis stays put while it plays through a passage with no notes
    js("() => { const r = document.getElementById('roll'); r.value = 'vocals'; r.onchange(); window.player.tr.seek(3); }")
    page.wait_for_timeout(400)
    js("() => { const tr = window.player.tr; tr.seek(15); tr.play(); }")
    page.wait_for_timeout(300)
    range_a = js("() => document.getElementById('detail').dataset.range")
    page.wait_for_timeout(1500)
    res["roll_range_in_gap"] = [range_a, js("() => document.getElementById('detail').dataset.range")]
    js("() => window.player.tr.pause()")
    js("() => window.player.ui.setSpeed(0.9)")
    page.reload()                                        # at once: the change is saved when the page goes away
    page.wait_for_function("document.documentElement.dataset.ready === '1'")
    res["speed_after_quick_reload"] = js(state)["speed"]
    res["restored_notice"] = js("() => !document.getElementById('restored').hidden && document.getElementById('restored-text').textContent")
    page.keyboard.press("]")                             # any change: the notice is no longer true, so it goes
    page.wait_for_timeout(200)
    res["notice_after_change"] = js("() => document.getElementById('restored').hidden")
    page.click("#reset-song-2")
    page.wait_for_timeout(200)
    st = js(state)
    res["after_reset_song"] = {"loop": st["loop"], "regions": st["regions"], "speed": st["speed"]}
    page.close()
    # an unknown ?song= says so and loads nothing (no silent fallback to the first, maybe longest, song)
    page = open_page(browser, f"{base}player.html?song=no-such-song", problems)
    res["unknown_song"] = {"error": page.evaluate("() => document.getElementById('status').classList.contains('err')"),
                           "text": page.inner_text("#status"), "loaded": page.evaluate("() => !!window.player")}
    page.close()
    ok_click = lambda r, t: r["loop"] == no_loop and abs(r["x"] - t) < 0.6  # noqa: E731
    checks = {
        "a shaky click (8 px, or an 11 px drag) seeks and makes no loop": ok_click(res["shaky_click"], 20)
        and ok_click(res["shaky_drag"], 30),
        "unsnapped drags run from the press to the release, both ways and turned back": res["unsnapped_drag"] == [10, 20]
        and res["leftward_drag"] == [30, 36] and res["turned_drag"] == [17, 20],
        "A and B set the loop at the playhead, snapped to bars": res["after_a"] == {"on": True, "a": 10.5, "b": 14.5}
        and res["after_b"]["loop"] == {"on": True, "a": 10.5, "b": 12.5} and res["after_b"]["x"] == 10.5,
        "A at the very end of the song still makes a loop": res["a_at_end"]["on"] and res["a_at_end"]["b"] > res["a_at_end"]["a"],
        "the loop readout shows times and bars, and says when the playhead is outside":
            res["loop_info"] == "0:10.5–0:12.5 · 第 6 小节" and res["loop_info_outside"].endswith("（播放头在循环外）"),
        "the transport keeps its height whatever its readouts say": len(set(heights.values())) == 1,
        "a shaky click on the live loop seeks and leaves the loop": res["shaky_click_on_loop"]["loop"] == {"on": True, "a": 10.5, "b": 12.5}
        and abs(res["shaky_click_on_loop"]["x"] - 11.5) < 0.6,
        "Esc on a focused slider only leaves it": res["esc_leaves_slider"]["focus"] == "BODY" and res["esc_leaves_slider"]["loop"]["on"],
        "a double-click on the loop removes it": res["after_dblclick"] == no_loop,
        "a focused slider keeps its arrow keys": res["slider_keys"] == {"speed": 1.02, "moved": 0},
        "Enter leaves a number box, then Space plays; Esc cancels typing": res["focus_after_enter"] == "BODY"
        and res["space_after_enter"] and res["esc_cancels_typing"] == 0.85,
        "Esc leaves a select": res["esc_leaves_select"] == "BODY",
        "Space after clicking a download link plays and does not scroll": res["space_after_link"] == {"playing": True, "scroll": 0},
        "double-click on the speed slider resets it": res["speed_after_dblclick"] == 1,
        "the trainer starts at its start speed and gives the speed back (box, L, Esc)":
            res["trainer_on"]["speed"] == 0.6 and "60%" in res["trainer_on"]["status"] and res["speed_after_trainer"] == 0.9
            and res["trainer_after_l"] == res["trainer_after_Escape"] == {"speed": 0.9, "checked": False},
        "the trainer refuses a start at or above the target": all(
            not res[k]["checked"] and res[k]["status"] for k in ("trainer_bad_0.9_0.7", "trainer_bad_0.8_0.8")),
        "the trainer counts its passes and steps the speed": res["trainer_pass_2"]["speed"] == 0.65
        and "第 2 遍" in res["trainer_pass_2"]["status"],
        "the transport shows what is changed; 原调 resets the pitch": "−2" in res["chips"] and res["semis_after_reset"] == 0,
        "a speed region under the playhead is explained and its row lit": "70%" in res["speed_note"] and res["region_row_lit"] == 1,
        "the language switch keeps the song": res["lang_link"].endswith("player.en.html?song=sync"),
        "no beat is shown before the first beat": res["bar_before_first_beat"] == "",
        "a seek during the count-in counts in again": bool(res["count_in_again"]["first"])
        and (res["count_in_again"]["second"] or 0) > res["count_in_again"]["first"],
        "the piano roll's pitch axis does not grow in a passage without notes":
            res["roll_range_in_gap"][0] == res["roll_range_in_gap"][1]
            and 14 <= int(res["roll_range_in_gap"][1].split("-")[1]) - int(res["roll_range_in_gap"][1].split("-")[0]) <= 20,
        "a change right before a reload is kept": res["speed_after_quick_reload"] == 0.9,
        "restored settings are announced in full, and the notice goes after a change": bool(res["restored_notice"])
        and all(w in res["restored_notice"] for w in ("速度 90%", "2 个变速段")) and res["notice_after_change"],
        "本曲恢复默认 clears the song's settings": res["after_reset_song"] == {"loop": no_loop, "regions": [], "speed": 1},
        "an unknown song is reported and nothing is loaded": res["unknown_song"]["error"] and "no-such-song" in res["unknown_song"]["text"]
        and not res["unknown_song"]["loaded"],
    }
    return res, checks


def run_gp_dialog(browser, base, problems):
    """Opened from the workbench, the player offers a Guitar Pro download with a chosen audio track (POST /api/gp,
    answered here by a stand-in); the default is vocals + guitar + other."""
    page = open_page(browser, f"{base}player.html?song=sync&from=workbench", problems)
    seen = []

    def fake_gp(route):
        seen.append(json.loads(route.request.post_data or "{}") | {"header": route.request.headers.get("x-openmusic")})
        route.fulfill(status=200, body=b"PK fake gp", headers={"Content-Type": "application/octet-stream"})
    page.route("**/api/gp", fake_gp)
    res = {"button": page.inner_text("#roll-gp")}
    page.click("#roll-gp button")
    res["defaults"] = page.evaluate("""() => ({audio: document.querySelector('#gp-dialog input[name=gp-audio]:checked').value,
        stems: [...document.querySelectorAll('#gp-stems input:checked')].map(c => c.value)})""")
    page.keyboard.press(" ")                              # the player's keys are off while the dialog is open
    res["playing_in_dialog"] = page.evaluate("() => window.player.tr.playing")
    with page.expect_download() as dl:
        page.click("#gp-go")
    res["download_name"] = dl.value.suggested_filename
    res["request"] = seen[-1] if seen else None
    page.wait_for_timeout(1500)
    res["closed"] = page.evaluate("() => !document.getElementById('gp-dialog').open")
    page.click("#roll-gp button")                         # bass only: the choice is remembered for next time
    page.uncheck("#gp-stems input[value=vocals]"); page.uncheck("#gp-stems input[value=guitar]")
    page.uncheck("#gp-stems input[value=other]"); page.check("#gp-stems input[value=bass]")
    with page.expect_download() as dl:
        page.click("#gp-go")
    res["bass_name"] = dl.value.suggested_filename
    page.wait_for_timeout(1500)
    page.click("#roll-gp button")
    res["remembered"] = page.evaluate("() => [...document.querySelectorAll('#gp-stems input:checked')].map(c => c.value)")
    page.check("#gp-dialog input[value=none]")
    with page.expect_download() as dl:
        page.click("#gp-go")
    res["none"] = {"name": dl.value.suggested_filename, "audio": seen[-1]["audio"]}
    page.close()
    page = open_page(browser, f"{base}player.html?song=sync", problems)   # not from the workbench: no dialog
    res["plain_page_button"] = page.evaluate("() => !!document.querySelector('#roll-gp button')")
    page.close()
    checks = {
        "Guitar Pro dialog: default audio is vocals + guitar + other": res["defaults"] == {"audio": "stems", "stems": ["vocals", "guitar", "other"]},
        "Guitar Pro dialog: the request names the song, the audio and the stems": res["request"] is not None
        and res["request"]["song"] == "sync" and res["request"]["audio"] == "stems"
        and res["request"]["stems"] == ["vocals", "guitar", "other"] and res["request"]["header"] == "1",
        "Guitar Pro dialog: the file is named after the choice, and the dialog closes": res["download_name"] == "sync (人声 吉他 其他).gp"
        and res["bass_name"] == "sync (贝斯).gp" and res["closed"],
        "Guitar Pro dialog: the last choice is remembered; no audio works": res["remembered"] == ["bass"]
        and res["none"] == {"name": "sync (无音频).gp", "audio": "none"},
        "Guitar Pro dialog: player keys are off while it is open": not res["playing_in_dialog"],
        "Guitar Pro dialog: only offered when opened from the workbench": not res["plain_page_button"],
    }
    return res, checks


def run_screens(browser, base, problems):
    SCREENS.mkdir(parents=True, exist_ok=True)
    shots = []
    real = sorted(p.name for p in REAL.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
    short = next((s for s in real if "intro" in s), real[0] if real else None)
    targets = [("sync", "player.html", "sync"), ("sync", "player.en.html", "sync")]
    if short:
        targets += [(short, "real/player.html", "real"), (short, "real/player.en.html", "real")]
    for song, page_name, tag in targets:
        for w, h in ((1280, 900), (390, 844)):
            page = open_page(browser, f"{base}{page_name}?song={quote(song)}", problems, viewport=(w, h))
            page.evaluate("() => { const p = window.player; if (p) { p.tr.seek(Math.min(9, p.tr.duration / 3)); } }")
            page.wait_for_timeout(500)
            lang = "en" if page_name.endswith(".en.html") else "zh"
            path = SCREENS / f"{tag}_{lang}_{w}.png"
            page.screenshot(path=str(path), full_page=True)
            width = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
            shots.append({"file": str(path), "scrollWidth_vs_viewport": width})
            page.close()
    return shots


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default=build_player.ENGINE, choices=("stems", "multi"))
    parser.add_argument("--browser", default="chromium", choices=("chromium", "webkit"))
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--memory", action="store_true")
    parser.add_argument("--screens", action="store_true")
    parser.add_argument("--skip-sync", action="store_true", help="only run the optional parts")
    args = parser.parse_args()

    make_songs()
    build_player.build(SONGS)
    if args.memory or args.screens:
        build_player.build(REAL)
    report, checks, problems = {}, {}, []
    with serve(SONGS) as base, sync_playwright() as p:
        browser = (p.webkit.launch() if args.browser == "webkit" else
                   p.chromium.launch(args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"]))
        try:
            if not args.skip_sync:
                report["sync"], c = run_sync(browser, base, args.engine, problems)
                checks.update(c)
                report["loud"], c = run_loud(browser, base, problems)
                checks.update(c)
                report["bare"], c = run_bare(browser, base, problems)
                checks.update(c)
                report["ui"], c = run_ui(browser, base, problems)
                checks.update(c)
                report["ux"], c = run_ux(browser, base, problems)
                checks.update(c)
                report["gp_dialog"], c = run_gp_dialog(browser, base, problems)
                checks.update(c)
            if args.cpu:
                report["cpu"] = run_cpu(browser, base, problems)
                report["cpu_roformer_song"] = run_cpu(browser, base, problems, "想想XiangXiang - 偷看你的日记", 60)
            if args.memory:
                report["memory"] = run_memory(browser, base, problems)
            if args.screens:
                report["screens"] = run_screens(browser, base, problems)
        finally:
            browser.close()
    file_problems = [q for q in problems if "file://" not in q and "ERR_FILE" not in q]
    checks["no console errors or warnings"] = not file_problems
    report["console"] = file_problems[:20]
    report["checks"] = checks
    print(json.dumps(report, ensure_ascii=False, indent=1))
    failed = [k for k, v in checks.items() if not v]
    print("FAILED: " + "; ".join(failed) if failed else "all checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
