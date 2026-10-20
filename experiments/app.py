"""OpenMusic workbench: a local web page that takes a song from an audio file to the practice player.

    python experiments/app.py                       # http://localhost:8780, opens the browser
    python experiments/app.py --port 8781 --no-browser

For each song in songs/ it runs the existing command-line steps, one job at a time (one heavy job at a time
keeps a 16 GB laptop responsive):
  1. split into stems     evaluate_multi.py <model> --full --no-score --only <song>
  2. tempo, key, chords   analyze.py <library> --song <song>
  3. MIDI per stem        transcribe.py <library> --song <song>
     Guitar Pro file      export_gp.py <library> --song <song>   (after every analyze or transcribe)
  4. practice player      build_player.py <library>
It also serves the project folder like `python -m http.server`, so the player opens from the same address.
Only standard-library Python; listens on 127.0.0.1 only and accepts actions only from its own page.
"""
import argparse
import collections
import json
import os
import signal
import subprocess
import tempfile
import unicodedata
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from common import OUTPUTS, ROOT, STEMS_INFO, read_stems_info

SONGS = ROOT / "songs"
AUDIO = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"}
STEM_NAMES = ("vocals", "drums", "bass", "guitar", "piano", "other")
STEM_EXT = (".mp3", ".flac", ".wav")
MAX_UPLOAD = 400 * 1024 * 1024

# Each separation model writes full-song stems to outputs/<model>_full/<song>/ (evaluate_multi.py --full).
# "mp3" is the hand-collected library (export_mp3.py): no split step, the stems are already there.
LIBRARIES = {
    "demucs6": {"root": OUTPUTS / "demucs6_full", "split": "demucs6"},
    "roformer_sw": {"root": OUTPUTS / "roformer_sw_full", "split": "roformer_sw"},
    "mp3": {"root": OUTPUTS / "mp3", "split": None},
}
STEPS = ("split", "analyze", "transcribe", "gp", "player")


def detect_hardware():
    """GPU kind in a child process, so the server itself never imports torch (~300 MB)."""
    code = ("import torch; print('cuda' if torch.cuda.is_available() else "
            "'mps' if torch.backends.mps.is_available() else 'cpu')")
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        return out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def ram_gb():
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong)] + \
                       [(f"x{i}", ctypes.c_ulonglong) for i in range(6)]
        m = MemoryStatus()
        m.length = ctypes.sizeof(MemoryStatus)
        return round(m.total / 2 ** 30) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)) else None
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30)
    except (AttributeError, ValueError, OSError):
        return None


RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MODELS = {"demucs6", "demucs4", "demucs_ai", "roformer_sw"}


def valid_song_name(stem):
    """A song name that is safe as a folder name on macOS and Windows and that build_player.py lists
    (it skips names starting with '_' or '.'); rejects '..', control/format characters and reserved names."""
    return (bool(stem) and stem.strip() == stem and stem not in (".", "..") and stem[0] not in "._-"
            and not stem.endswith(".") and stem.split(".")[0].upper() not in RESERVED
            and not any(ord(c) < 32 or 127 <= ord(c) < 160 or c in '<>:"/\\|?*' or unicodedata.category(c) == "Cf"
                        for c in stem))


def inside(root, name):
    """Defence in depth: <root>/<name> must be a direct child of root."""
    return (root / name).resolve().parent == root.resolve()


def source_files():
    if not SONGS.is_dir():
        return {}
    return {p.stem: p.name for p in SONGS.iterdir() if p.suffix.lower() in AUDIO and valid_song_name(p.stem)}


def stem_files(folder):
    return [folder / f"{s}{e}" for s in STEM_NAMES for e in STEM_EXT if (folder / f"{s}{e}").exists()]


def mtime(paths):
    return max((p.stat().st_mtime for p in paths), default=0)


_summaries = {}


def analysis_summary(path):
    st = path.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _summaries.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        bpb, conf = d["tempo"].get("beats_per_bar"), d["key"].get("confidence")
        summary = {"bpm": round(float(d["tempo"]["bpm"]), 1), "beats_per_bar": int(bpb) if bpb else None,
                   "key_zh": str(d["key"]["label_zh"])[:40], "key_en": str(d["key"]["label_en"])[:40],
                   "confidence": float(conf) if conf is not None else None}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        summary = {"error": True}
    _summaries[path] = (stamp, summary)
    return summary


def song_status(key, name, sources):
    lib = LIBRARIES[key]
    folder = lib["root"] / name
    stems_paths = stem_files(folder)
    stems = [s for s in STEM_NAMES if any(p.stem == s for p in stems_paths)]
    stems_t = mtime(stems_paths)
    path = folder / "analysis.json"
    analysis = analysis_summary(path) if path.exists() else None
    tdir = folder / "transcription"
    mids = sorted(p for p in tdir.glob("*.mid") if p.stem in STEM_NAMES) if tdir.is_dir() else []
    # A step is done only if its output is newer than the stems (a re-split makes analysis and MIDI stale).
    an_ok = bool(analysis and "error" not in analysis) and path.stat().st_mtime >= stems_t
    tr_ok = tdir.is_dir() and mtime([tdir]) >= stems_t and (not mids or min(p.stat().st_mtime for p in mids) >= stems_t)
    gp_file = tdir / f"{name}.gp"   # Guitar Pro 7/8 (export_gp.py)
    gp_inputs = list(tdir.glob("*.notes.json")) * tdir.is_dir() + mids + [path] * path.exists()
    gp_ok = tr_ok and (gp_file.exists() and gp_file.stat().st_mtime >= mtime(gp_inputs) or not mids)
    player = lib["root"] / "player.html"
    readable = stems_paths + [path] * path.exists() + list(tdir.glob("*.notes.json")) * tdir.is_dir() + mids
    readable += [gp_file] * gp_file.exists()
    player_state = "missing" if not player.exists() else ("stale" if player.stat().st_mtime < mtime(readable) else "ok")
    info = read_stems_info(folder) or {}
    model = info.get("model") if info.get("model") in MODELS else (lib["split"] if stems and lib["split"] else None)
    split_ok = bool(stems)
    if lib["split"] and stems:  # evaluate_multi writes stems.json after the last stem; a newer song file means re-split
        src = SONGS / sources[name] if name in sources else None
        marker = folder / STEMS_INFO
        split_ok = (bool(info) and set(info.get("stems") or stems) <= set(stems)
                    and (src is None or src.stat().st_mtime <= marker.stat().st_mtime))
    rel = lib["root"].relative_to(ROOT).as_posix()
    return {"name": name, "source": sources.get(name), "stems": stems, "model": model if stems else None,
            "analysis": analysis, "midi": [p.stem for p in mids],
            "midi_urls": {p.stem: f"/{rel}/{quote(name)}/transcription/{quote(p.name)}" for p in mids},
            "gp_url": f"/{rel}/{quote(name)}/transcription/{quote(gp_file.name)}" if gp_file.exists() else None,
            "player": player_state,
            "player_url": f"/{rel}/player.html?song={quote(name)}&from=workbench" if stems and player.exists() else None,
            "split_stale": bool(stems) and not split_ok,
            "done": {"split": split_ok, "analyze": an_ok, "transcribe": tr_ok, "gp": gp_ok,
                     "player": bool(stems) and player_state == "ok"}}


def library_songs(key, sources):
    lib = LIBRARIES[key]
    root = lib["root"]
    names = {p.name for p in root.iterdir() if p.is_dir() and valid_song_name(p.name) and stem_files(p)} if root.is_dir() else set()
    if lib["split"]:
        names |= set(sources)
    return sorted(names, key=str.lower)


class Jobs:
    """A queue of pipeline steps run one at a time in a worker thread, each as a child process."""

    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue, self.history, self.current, self.next_id = [], collections.deque(maxlen=30), None, 1
        threading.Thread(target=self._worker, daemon=True).start()

    def add(self, library, song, step, force=False):
        with self.lock:
            for j in [self.current, *self.queue]:
                if j and j["status"] != "cancelled" and (j["library"], j["song"], j["step"]) == (library, song, step):
                    return j["id"]  # already planned
            job = {"id": self.next_id, "library": library, "song": song, "step": step, "force": force,
                   "status": "queued", "log": collections.deque(maxlen=300), "started": None, "ended": None,
                   "proc": None}
            self.next_id += 1
            self.queue.append(job)
        self.wake.set()
        return job["id"]

    def cancel(self, job_id):
        with self.lock:
            for j in list(self.queue):
                if job_id in (None, j["id"]):
                    self.queue.remove(j)
                    j["status"] = "cancelled"
                    self.history.appendleft(j)
            cur = self.current
            if cur and job_id in (None, cur["id"]):
                cur["status"] = "cancelled"
                if cur["proc"]:
                    cur["proc"].terminate()
                for j in [j for j in self.queue if (j["library"], j["song"]) == (cur["library"], cur["song"])]:
                    self.queue.remove(j)  # the rest of that song's plan was started by the same click
                    j["status"] = "cancelled"
                    self.history.appendleft(j)

    def snapshot(self):
        def view(j):
            return {k: (list(v)[-80:] if k == "log" else v) for k, v in j.items() if k != "proc"}
        with self.lock:
            return {"current": view(self.current) if self.current else None,
                    "queue": [view(j) for j in self.queue], "history": [view(j) for j in self.history],
                    "now": time.time()}

    def _worker(self):
        while True:
            self.wake.wait()
            with self.lock:
                if not self.queue:
                    self.wake.clear()
                    continue
                job = self.current = self.queue.pop(0)
                job["status"], job["started"] = "running", time.time()
            ok = self._run(job)
            with self.lock:
                job["ended"] = time.time()
                if job["status"] == "running":
                    job["status"] = "done" if ok else "failed"
                if job["status"] == "failed" and job["step"] == "split":  # the other steps need the stems
                    for j in [j for j in self.queue if (j["library"], j["song"]) == (job["library"], job["song"])]:
                        self.queue.remove(j)
                        j["status"] = "skipped"
                        self.history.appendleft(j)
                self.history.appendleft(job)
                self.current = None

    def _run(self, job):
        cmd = command(job)
        job["log"].append("$ python " + " ".join(shown(c) for c in cmd[1:]))
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                    text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            job["log"].append(str(e))
            return False
        with self.lock:
            job["proc"] = proc
            if job["status"] == "cancelled":  # stopped while it was starting
                proc.terminate()
        for line in proc.stdout:
            line = line.rstrip("\n").split("\r")[-1]  # keep the last state of \r progress lines
            if line.strip() and "Warning" not in line and "open http://localhost:8765" not in line:
                job["log"].append(line)
        return proc.wait() == 0


def shown(arg):
    """A command-line argument as the log shows it: paths relative to the project, names quoted if needed."""
    try:
        arg = Path(arg).relative_to(ROOT).as_posix() if Path(arg).is_absolute() else arg
    except ValueError:
        pass
    return f'"{arg}"' if " " in arg else arg


def command(job):
    lib = LIBRARIES[job["library"]]
    py, scripts, root, song = sys.executable, ROOT / "experiments", str(lib["root"]), job["song"]
    if job["step"] == "split":  # "--opt=value" so a name starting with "-" is never read as an option
        return [py, str(scripts / "evaluate_multi.py"), lib["split"], "--full", "--no-score", f"--only={song}"]
    if job["step"] == "player":
        return [py, str(scripts / "build_player.py"), root]
    script = {"analyze": "analyze.py", "transcribe": "transcribe.py", "gp": "export_gp.py"}[job["step"]]
    return [py, str(scripts / script), root, f"--song={song}"] + (["--force"] if job["force"] else [])


class Handler(SimpleHTTPRequestHandler):
    jobs, hardware = None, {"gpu": "unknown"}
    # Pin the types the pages need: on Windows, mimetypes reads the registry and may serve .js as text/plain.
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".html": "text/html; charset=utf-8",
                      ".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json",
                      ".css": "text/css", ".png": "image/png", ".mp3": "audio/mpeg", ".flac": "audio/flac",
                      ".wav": "audio/wav", ".mid": "audio/midi"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        pass

    timeout = 60  # a stalled client cannot hold a thread or an upload forever

    def end_headers(self):
        if getattr(self, "static", False):
            self.send_header("Cache-Control", "no-cache")  # files change whenever a step is redone
        self.send_header("X-Frame-Options", "DENY")  # no other site may frame the page and steer clicks
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _static_ok(self, path):
        rel = unquote(path).lstrip("/")
        return ("\0" not in rel and (rel.startswith("outputs/") or rel in ("compare.html", "compare.en.html"))
                and not any(part.startswith(".") for part in rel.split("/")))

    def do_HEAD(self):
        if not self._host_ok() or not self._static_ok(urlparse(self.path).path):
            return self.send_error(404)
        self.static = True
        return super().do_HEAD()

    def _send(self, code, body, kind):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _host_ok(self):
        # Refuse requests that name another host (DNS rebinding); the page itself always uses localhost.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("localhost", "127.0.0.1", "::1")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        path = urlparse(self.path).path
        if path in ("/", "/app", "/app/"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(self.state())
        if path.startswith("/api/") or not self._static_ok(path):
            return self._json({"error": "not found"}, 404)
        self.static = True
        return super().do_GET()

    def do_POST(self):
        # A custom header cannot be sent cross-site without a CORS preflight, which this server never answers.
        if not self._host_ok() or self.headers.get("X-OpenMusic") != "1":
            return self._json({"error": "forbidden"}, 403)
        url = urlparse(self.path)
        if url.path == "/api/upload":
            return self.upload(parse_qs(url.query))
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "bad length"}, 400)
        if not 0 <= n <= 1 << 16:
            return self._json({"error": "too large"}, 413)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "bad json"}, 400)
        if url.path == "/api/run":
            return self.run(body)
        if url.path == "/api/cancel":
            job_id = body.get("id")
            self.jobs.cancel(job_id if isinstance(job_id, int) else -1)
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def state(self):
        sources = source_files()
        libraries = {}
        for key, lib in LIBRARIES.items():
            libraries[key] = {"root": lib["root"].relative_to(ROOT).as_posix(), "path": str(lib["root"]),
                              "split": lib["split"],
                              "songs": [song_status(key, n, sources) for n in library_songs(key, sources)]}
        return {"libraries": libraries, "jobs": self.jobs.snapshot(), "sources": sorted(sources.values()),
                "hardware": dict(self.hardware, platform=sys.platform, ram_gb=ram_gb())}

    def run(self, body):
        key, song, steps = body.get("library"), body.get("song"), body.get("steps")
        if key not in LIBRARIES:
            return self._json({"error": "unknown library"}, 400)
        sources = source_files()
        if (not isinstance(key, str) or not isinstance(song, str) or song not in library_songs(key, sources)
                or not inside(LIBRARIES[key]["root"], song)):
            return self._json({"error": "unknown song"}, 400)
        if steps is not None and not (isinstance(steps, list) and all(s in STEPS for s in steps)):
            return self._json({"error": "unknown step"}, 400)
        status = song_status(key, song, sources)
        steps = list(steps or [s for s in STEPS if not status["done"][s]])
        if "split" in steps and not (LIBRARIES[key]["split"] and song in sources):
            steps = [s for s in steps if s != "split"]
        if not status["stems"] and "split" not in steps:
            return self._json({"error": "no stems yet"}, 400)
        if any(s in steps for s in ("analyze", "transcribe")) and "gp" not in steps:
            steps.append("gp")  # the Guitar Pro file takes its bars and chords from the analysis, notes from the MIDI
        if any(s in steps for s in ("split", "analyze", "transcribe", "gp")) and "player" not in steps:
            steps.append("player")  # the player page lists songs and their files at build time
        ids = [self.jobs.add(key, song, s, force=bool(body.get("force"))) for s in STEPS if s in steps]
        return self._json({"ok": True, "jobs": ids})

    def upload(self, query):
        name = Path(unquote(self.headers.get("X-Filename") or "")).name.lstrip("_.-").strip()
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if Path(name).suffix.lower() not in AUDIO:
            return self._json({"error": "unsupported file type"}, 400)
        if not valid_song_name(Path(name).stem):
            return self._json({"error": "bad name"}, 400)
        if not 0 < size <= MAX_UPLOAD:
            return self._json({"error": "file too large"}, 413)
        SONGS.mkdir(exist_ok=True)
        target = SONGS / name
        twin = any(p.stem.lower() == target.stem.lower() and p.name != name for p in SONGS.iterdir())
        if twin or (target.exists() and query.get("overwrite") != ["1"]):
            while size > 0:  # drain the body so the connection stays usable
                chunk = self.rfile.read(min(size, 1 << 20))
                if not chunk:
                    break
                size -= len(chunk)
            return self._json({"error": "twin" if twin else "exists", "name": name}, 409)
        fd, part = tempfile.mkstemp(dir=SONGS, prefix=".upload-", suffix=".part")
        part = Path(part)
        try:
            with os.fdopen(fd, "wb") as f:
                left = size
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left:
                part.unlink(missing_ok=True)
                return self._json({"error": "upload interrupted"}, 400)
            os.replace(part, target)
        except OSError as e:  # a socket timeout, or on Windows the song file is open in another program
            part.unlink(missing_ok=True)
            return self._json({"error": "save failed", "detail": str(e)}, 500)
        return self._json({"ok": True, "name": name, "song": target.stem})


def other_workbench(port):
    """The URL of another workbench already running on this computer, if any (one heavy job at a time)."""
    for p in range(8780, 8800):
        if p == port:
            continue
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/state", timeout=0.2) as r:
                if r.status == 200 and b'"libraries"' in r.read(200000):
                    return f"http://localhost:{p}/"
        except (OSError, ValueError):
            pass
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    other = other_workbench(args.port)
    if other:
        print(f"Another workbench is already running at {other} — use that one, so two heavy jobs never run "
              f"at the same time. / 另一个工作台已经在 {other} 运行，请用那个，免得同时跑两个大任务。", flush=True)
    Handler.jobs = Jobs()
    threading.Thread(target=lambda: Handler.hardware.update(gpu=detect_hardware()), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}/"
    print(f"OpenMusic workbench: {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proc = (Handler.jobs.current or {}).get("proc")
        Handler.jobs.cancel(None)
        if proc:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        server.server_close()


PAGE = r"""<!doctype html>
<html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>OpenMusic 工作台</title>
<style>
:root{--bg:#fbfaf8;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e4e2dd;--accent:#b4451f;--on-accent:#fff;--ok:#2f7d4f;--warn:#a86b00;--bad:#b3261e;--chip:#f1efea}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#17171a;--fg:#ececef;--muted:#9a9aa2;--card:#202024;--line:#33333a;--accent:#f08a5d;--on-accent:#17171a;--ok:#6cc58d;--warn:#e0b24a;--bad:#f2867e;--chip:#2a2a2f}}
:root[data-theme=dark]{--bg:#17171a;--fg:#ececef;--muted:#9a9aa2;--card:#202024;--line:#33333a;--accent:#f08a5d;--on-accent:#17171a;--ok:#6cc58d;--warn:#e0b24a;--bad:#f2867e;--chip:#2a2a2f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,"PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 64px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-size:22px;margin:0}h2{font-size:16px;margin:0 0 10px}
.muted{color:var(--muted)}.small{font-size:13px}
a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:14px 0}
.steps{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:0;padding:0;list-style:none;counter-reset:s}
.steps li{background:var(--chip);border-radius:8px;padding:8px 10px;font-size:13px;counter-increment:s}
.steps li b{display:block;font-size:14px}.steps li b::before{content:counter(s) ". ";color:var(--accent)}
@media (max-width:760px){.steps{grid-template-columns:1fr 1fr}}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tabs button{border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:8px;padding:6px 12px;cursor:pointer;font:inherit;font-size:14px;text-align:left}
.tabs button[aria-pressed=true]{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.tabs small{display:block;color:var(--muted);font-size:12px}
.drop{display:block;border:2px dashed var(--line);border-radius:10px;padding:18px;text-align:center;cursor:pointer}
.drop.over{border-color:var(--accent)}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
td:first-child{width:30%}
th{font-size:12px;color:var(--muted);font-weight:600}
.song{font-weight:600}.song,.detail{overflow-wrap:anywhere}
.chip{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:var(--chip);white-space:nowrap}
.chip.ok{color:var(--ok)}.chip.run{color:var(--accent)}.chip.queued{color:var(--warn)}.chip.bad{color:var(--bad)}.chip.todo{color:var(--muted)}
button.act,a.act{display:inline-block;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit;font-size:13px;margin:2px 4px 2px 0;white-space:nowrap;text-decoration:none}
.primary{background:var(--accent)!important;border-color:var(--accent)!important;color:var(--on-accent)!important}
button:disabled{opacity:.45;cursor:default}
.detail{font-size:12px;color:var(--muted);margin-top:2px}
.detail a{color:var(--muted)}
pre{background:var(--chip);border-radius:8px;padding:10px;max-height:260px;overflow:auto;font-size:12px;margin:8px 0 0;white-space:pre-wrap;word-break:break-all}
.warn{color:var(--warn)}.bad{color:var(--bad)}
.hist{margin:4px 0}.hist details{display:inline}
@media (max-width:760px){table,thead,tbody,tr,td{display:block}thead{display:none}tr{border-bottom:1px solid var(--line);padding:8px 0}td{border:0;padding:4px 0;width:auto!important}
 td[data-h]::before{content:attr(data-h) "：";color:var(--muted);font-size:12px}html:lang(en) td[data-h]::before{content:attr(data-h) ": "}td:first-child::before{display:none}}
</style></head><body><main>
<header><h1 data-t=title></h1><span class=small><a href=# data-lang=zh>中文</a> | <a href=# data-lang=en>English</a></span></header>
<p class="muted small" data-t=intro></p>
<ol class="steps card">
 <li><b data-t=s1></b><span data-t=s1d></span></li><li><b data-t=s2></b><span data-t=s2d></span></li>
 <li><b data-t=s3></b><span data-t=s3d></span></li><li><b data-t=s4></b><span data-t=s4d></span></li>
 <li><b data-t=s5></b><span data-t=s5d></span></li>
</ol>
<section class=card><h2 data-t=add></h2>
 <label class=drop id=drop><input type=file id=file accept=".mp3,.m4a,.wav,.flac,.aac,.ogg" hidden multiple><span data-t=drop></span></label>
 <p class="small muted" id=upmsg></p></section>
<section class=card><h2 data-t=lib></h2><div class=tabs id=tabs></div><p class="small muted" id=libnote></p><p class="small warn" id=libwarn hidden></p>
 <table><thead><tr><th data-t=c_song></th><th data-t=c_split></th><th data-t=c_an></th><th data-t=c_midi></th><th data-t=c_player></th><th></th></tr></thead><tbody id=rows></tbody></table></section>
<section class=card id=jobcard><h2 data-t=jobs></h2><div id=jobs class=small></div></section>
<p class="small muted" data-t=foot></p>
</main>
<script>
const MODEL={zh:{demucs6:'Demucs 6 轨',demucs4:'Demucs 4 轨',demucs_ai:'Demucs 4 轨',roformer_sw:'BS-RoFormer-SW 6 轨'},en:{demucs6:'Demucs 6-stem',demucs4:'Demucs 4-stem',demucs_ai:'Demucs 4-stem',roformer_sw:'BS-RoFormer-SW 6-stem'}};
const STEM={zh:{vocals:'人声',drums:'鼓',bass:'贝斯',guitar:'吉他',piano:'钢琴/键盘',other:'其他'},en:{vocals:'vocals',drums:'drums',bass:'bass',guitar:'guitar',piano:'piano/keys',other:'other'}};
const T={zh:{title:'OpenMusic 工作台',intro:'把一首歌从音频文件一路处理到练习播放器。每一步都可以单独运行或重做；“一键处理”会按顺序跑完还没做的步骤。同一时间只跑一个任务，免得电脑卡。',
s1:'添加歌曲',s1d:'拖一个 mp3 / wav / flac 进来，存到 songs/',s2:'分轨',s2d:'用 AI 拆成人声、鼓、贝斯、吉他、钢琴、其他',s3:'分析',s3d:'速度、拍号和小节线、调、和弦',s4:'扒谱（MIDI）',s4d:'把每轨的音符写成 MIDI，并合成一份带六线谱和和弦名的 Guitar Pro 谱（鼓没有音高，不转）',s5:'练习',s5d:'在练习播放器里调音量、变速、循环、扒谱',
add:'1. 添加歌曲',drop:'把音频文件拖到这里，或点这里选择文件',lib:'2–5. 处理歌曲',c_song:'歌曲',c_split:'分轨',c_an:'速度 / 调',c_midi:'MIDI',c_player:'练习播放器',jobs:'任务',
L_demucs6:'Demucs 6 轨',L_demucs6s:'任何电脑都能跑，Mac 上一首约 1 分钟',L_roformer_sw:'RoFormer 6 轨',L_roformer_sws:'效果更好，需要 NVIDIA 显卡',L_mp3:'已整理的资料库',L_mp3s:'outputs/mp3，已经分好轨，每首注明了模型',
note_split:'分轨结果在 {root}/。',note_nosplit:'这里的歌已经分好轨（{root}/），从第 3 步开始。',
gpunote:'这台电脑没有 NVIDIA 显卡：不建议在这里用 RoFormer 分轨（可能占用 6 GB 以上内存、卡住电脑）。已经分好的歌仍可以分析、扒谱和练习。',
gpuwarn:'这台电脑没有 NVIDIA 显卡：RoFormer 会很慢，还可能占用 6 GB 以上内存让电脑卡住，建议在显卡电脑上跑。仍要继续吗？',
all:'一键处理',practise:'去练习',redo:'重做',run:'运行',open:'打开',none:'这里还没有歌。先在上面添加一首。',nosrc:'没有原始音频',
st_ok:'完成',st_todo:'未做',st_run:'进行中',st_queued:'排队中',st_bad:'失败',st_stale:'需更新',
stems:'{n} 轨',midi:'{n} 轨：',gp:'下载 Guitar Pro',gp_title:'整首歌的 Guitar Pro 谱（.gp，Guitar Pro 7、8 用）：所有分轨在一个文件里，吉他、贝斯带六线谱，上面有和弦名',model_unknown:'模型未知',conf:'把握 {p}%',idle:'现在没有任务在跑。',cancel:'停止',queue:'排队中：',recent:'最近：',
step_split:'分轨',step_analyze:'分析',step_transcribe:'扒谱',step_gp:'生成 Guitar Pro 谱',step_player:'生成播放器',elapsed:'已用 {s} 秒',
j_done:'完成',j_failed:'失败',j_cancelled:'已停止',j_skipped:'跳过（前一步没成功）',showlog:'查看日志',
up_busy:'正在添加：{n} …',up_ok:'已添加：{n}',up_exists:'{n} 已经在 songs/ 里了，要覆盖吗？',up_bad:'不支持这个文件类型：{n}（可以用 mp3、wav、flac、m4a、aac、ogg）',
up_big:'文件太大（上限 400 MB）：{n}',up_name:'文件名不能用：{n}（不能以点、下划线、减号开头，也不能含有 \\ / : * ? " < > |）',up_twin:'songs/ 里已经有同名但格式不同的歌：{n}',up_cut:'上传中断了，请再试一次：{n}',up_err:'保存失败：{n}（文件可能正被别的程序占用）',
hw:'这台电脑：{p}，{g}，内存 {r} GB',offline:'连不上工作台了：运行它的终端窗口可能已经关闭。重新运行 python experiments/app.py 后刷新本页。',
foot:'工作台只在本机运行（127.0.0.1）。它调用的就是 README 里的那几条命令，也可以直接在终端里跑。'},
en:{title:'OpenMusic workbench',intro:'Take a song from an audio file all the way to the practice player. Each step can be run or redone on its own; “Process all” runs the missing steps in order. One job runs at a time, so the computer stays responsive.',
s1:'Add a song',s1d:'drop an mp3 / wav / flac here; it is saved to songs/',s2:'Split',s2d:'AI separates vocals, drums, bass, guitar, piano, other',s3:'Analyze',s3d:'tempo, time signature and bar lines, key, chords',s4:'Transcribe (MIDI)',s4d:'notes of each stem as MIDI, plus one Guitar Pro file with tab and chord names (not drums)',s5:'Practise',s5d:'volume, speed, loops and transcription in the player',
add:'1. Add a song',drop:'Drop audio files here, or click to choose',lib:'2–5. Process songs',c_song:'Song',c_split:'Stems',c_an:'Tempo / key',c_midi:'MIDI',c_player:'Practice player',jobs:'Jobs',
L_demucs6:'Demucs 6-stem',L_demucs6s:'runs on any computer, about 1 min per song on a Mac',L_roformer_sw:'RoFormer 6-stem',L_roformer_sws:'better quality, needs an NVIDIA GPU',L_mp3:'Collected library',L_mp3s:'outputs/mp3, already split; each song shows its model',
note_split:'Stems go to {root}/.',note_nosplit:'Songs here are already split ({root}/); start at step 3.',
gpunote:'This computer has no NVIDIA GPU: splitting with RoFormer here is not recommended (it may use over 6 GB of memory and freeze the computer). Songs already split can still be analysed, transcribed and practised.',
gpuwarn:'This computer has no NVIDIA GPU: RoFormer will be slow and may use over 6 GB of memory and freeze the computer. Running it on a GPU machine is recommended. Continue anyway?',
all:'Process all',practise:'Practise',redo:'Redo',run:'Run',open:'Open',none:'No songs here yet. Add one above first.',nosrc:'no source audio',
st_ok:'done',st_todo:'not yet',st_run:'running',st_queued:'queued',st_bad:'failed',st_stale:'needs update',
stems:'{n} stems',midi:'{n} stems: ',gp:'Download Guitar Pro',gp_title:'The whole song as one Guitar Pro file (.gp, for Guitar Pro 7 and 8): every stem a track, tab for guitar and bass, chord names above',model_unknown:'model unknown',conf:'{p}% sure',idle:'No job is running.',cancel:'Stop',queue:'Queued: ',recent:'Recent:',
step_split:'split',step_analyze:'analyze',step_transcribe:'transcribe',step_gp:'Guitar Pro export',step_player:'build player',elapsed:'{s} s so far',
j_done:'done',j_failed:'failed',j_cancelled:'stopped',j_skipped:'skipped (previous step failed)',showlog:'show log',
up_busy:'Adding {n} …',up_ok:'Added: {n}',up_exists:'{n} is already in songs/. Overwrite it?',up_bad:'Unsupported file type: {n} (use mp3, wav, flac, m4a, aac or ogg)',
up_big:'File too large (max 400 MB): {n}',up_name:'This file name cannot be used: {n} (it must not start with a dot, underscore or hyphen, or contain \\ / : * ? " < > |)',up_twin:'songs/ already has a song with this name in another format: {n}',up_cut:'Upload interrupted, please try again: {n}',up_err:'Could not save {n} (it may be open in another program)',
hw:'This computer: {p}, {g}, {r} GB RAM',offline:'Lost contact with the workbench: the terminal running it may have been closed. Start python experiments/app.py again and reload this page.',
foot:'The workbench runs only on this computer (127.0.0.1). It runs the same commands as the README; you can also run them in a terminal.'}};
let lang='zh';try{lang=localStorage.getItem('om-lang')||'zh'}catch(e){}
const qlang=new URLSearchParams(location.search).get('lang');if(qlang in T){lang=qlang;try{localStorage.setItem('om-lang',lang)}catch(e){}}
let lib='demucs6';try{lib=localStorage.getItem('om-lib')||'demucs6'}catch(e){}
let S=null,offline=false;
const t=(k,v={})=>(T[lang][k]??k).replace(/\{(\w+)\}/g,(_,x)=>v[x]??'');
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const post=(u,b,h={})=>fetch(u,{method:'POST',headers:Object.assign({'X-OpenMusic':'1','Content-Type':'application/json'},h),body:b});
const playerHref=s=>s.player_url&&s.player_url.replace('player.html',lang==='zh'?'player.html':'player.en.html');
function applyLang(){document.documentElement.lang=lang;document.title=t('title');document.querySelectorAll('[data-t]').forEach(e=>e.textContent=t(e.dataset.t));jobsHtml='';render()}
document.querySelectorAll('[data-lang]').forEach(a=>a.onclick=e=>{e.preventDefault();lang=a.dataset.lang;try{localStorage.setItem('om-lang',lang)}catch(_){}applyLang()});
function active(){return S?[S.jobs.current,...S.jobs.queue].filter(Boolean):[]}
function jobFor(song,step){return active().find(x=>x.library===lib&&x.song===song&&x.step===step)}
function lastRun(song,step){return S.jobs.history.find(x=>x.library===lib&&x.song===song&&x.step===step)}
function chip(song,step,done,stale){const j=jobFor(song,step);if(j)return `<span class="chip ${j.status==='running'?'run':'queued'}">${t(j.status==='running'?'st_run':'st_queued')}</span>`;
 const h=lastRun(song,step);
 if(h&&h.status==='failed')return `<span class="chip bad" title="${esc(reason(h))}">✗ ${t('st_bad')}</span>`;
 if(stale)return `<span class="chip queued">${t('st_stale')}</span>`;return done?`<span class="chip ok">✓ ${t('st_ok')}</span>`:`<span class="chip todo">○ ${t('st_todo')}</span>`}
function reason(j){const lines=(j.log||[]).filter(l=>l.trim()&&!/^\s/.test(l)&&!l.startsWith('$ '));return lines.at(-1)||''}
function render(){if(!S)return;const L=S.libraries;const tabs=document.getElementById('tabs');
 if(!L[lib])lib='demucs6';
 tabs.innerHTML=Object.keys(L).map(k=>`<button aria-pressed=${k===lib} data-k=${k}>${t('L_'+k)}<small>${t('L_'+k+'s')}</small></button>`).join('');
 tabs.querySelectorAll('button').forEach(b=>b.onclick=()=>{lib=b.dataset.k;try{localStorage.setItem('om-lib',lib)}catch(_){}render()});
 const cur=L[lib],hw=S.hardware,g={cuda:'NVIDIA GPU',mps:'Apple GPU',cpu:'CPU',unknown:'…'}[hw.gpu]||hw.gpu;
 const note=document.getElementById('libnote');note.textContent=(cur.split?t('note_split',{root:cur.root}):t('note_nosplit',{root:cur.root}))+'  '+t('hw',{p:{darwin:'macOS',win32:'Windows'}[hw.platform]||hw.platform,g,r:hw.ram_gb??'?'});note.title=cur.path;
 const noGpu=lib==='roformer_sw'&&hw.gpu!=='cuda'&&hw.gpu!=='unknown';const warn=document.getElementById('libwarn');warn.hidden=!noGpu;warn.textContent=t('gpunote');
 const rows=document.getElementById('rows');
 if(!cur.songs.length){rows.innerHTML=`<tr><td colspan=6 class=muted>${t('none')}</td></tr>`}else rows.innerHTML=cur.songs.map(s=>{const a=s.analysis&&!s.analysis.error?s.analysis:null;const d=s.done;
  const canSplit=!!cur.split&&!!s.source;const busy=active().some(x=>x.library===lib&&x.song===s.name);
  const btn=(step,label,en)=>`<button class=act data-song="${esc(s.name)}" data-step=${step} ${en&&!busy?'':'disabled'}>${label}</button>`;
  const allDone=d.split&&d.analyze&&d.transcribe&&d.gp&&d.player;
  const midi=(s.midi.length?t('midi',{n:s.midi.length})+s.midi.map(m=>`<a href="${esc(s.midi_urls[m])}" download>${esc(STEM[lang][m]||m)}</a>`).join(lang==='zh'?'、':', '):'')
   +(s.gp_url?`<div><a href="${esc(s.gp_url)}" download title="${esc(t('gp_title'))}">⬇ ${t('gp')}</a></div>`:'');
  const gpRun=lastRun(s.name,'gp'),trRun=lastRun(s.name,'transcribe');
  const midiStep=jobFor(s.name,'transcribe')?'transcribe':jobFor(s.name,'gp')||(gpRun&&gpRun.status==='failed'&&!(trRun&&trRun.status==='failed'))?'gp':'transcribe';
  const last=allDone&&!busy?`<a class="act primary" href="${playerHref(s)}">${t('practise')} →</a>`
   :`<button class="act ${noGpu&&!d.split?'':'primary'}" data-song="${esc(s.name)}" data-all=1 ${(d.split||canSplit)&&!busy?'':'disabled'}>${t('all')}</button>`;
  return `<tr><td data-h="${t('c_song')}"><div class=song>${esc(s.name)}</div><div class=detail>${s.source?esc(s.source):t('nosrc')}</div></td>
  <td data-h="${t('c_split')}">${chip(s.name,'split',d.split,s.split_stale)}<div class=detail>${s.stems.length?t('stems',{n:s.stems.length})+' · '+(s.model?esc(MODEL[lang][s.model]||s.model):t('model_unknown')):''}</div>${canSplit?btn('split',d.split?t('redo'):t('run'),true):''}</td>
  <td data-h="${t('c_an')}">${chip(s.name,'analyze',d.analyze,a&&!d.analyze)}<div class=detail>${a?`${esc(a.bpm)} BPM${a.beats_per_bar?' · '+esc(a.beats_per_bar)+'/4':''} · ${esc(lang==='zh'?a.key_zh:a.key_en)}${a.confidence!=null?' · '+t('conf',{p:Math.round(a.confidence*100)}):''}`:''}</div>${btn('analyze',a?t('redo'):t('run'),d.split)}</td>
  <td data-h="${t('c_midi')}">${chip(s.name,midiStep,d.transcribe&&d.gp,s.midi.length&&!(d.transcribe&&d.gp))}<div class=detail>${midi}</div>${btn('transcribe',s.midi.length?t('redo'):t('run'),d.split)}</td>
  <td data-h="${t('c_player')}">${chip(s.name,'player',d.player,s.player==='stale')}<div class=detail></div>${s.player_url?`<a class="act" href="${playerHref(s)}">${t('open')} →</a>`:''}</td>
  <td>${last}</td></tr>`}).join('');
 rows.querySelectorAll('button[data-song]').forEach(b=>b.onclick=()=>run(b.dataset.song,b.dataset.step,!!b.dataset.all));
 renderJobs()}
let jobsHtml='';
function renderJobs(){const J=S.jobs,box=document.getElementById('jobs');let h='';
 if(offline)h+=`<div class=bad>${t('offline')}</div>`;
 if(J.current){const c=J.current;h+=`<div><b>${esc(c.song)}</b> · ${t('step_'+c.step)} · <span id=elapsed></span> <button class=act data-cancel=${c.id}>${t('cancel')}</button></div><pre id=log>${esc(c.log.join('\n'))}</pre>`}else if(!offline)h+=`<div class=muted>${t('idle')}</div>`;
 if(J.queue.length)h+=`<div style="margin-top:8px">${t('queue')}${J.queue.map(q=>`${esc(q.song)} · ${t('step_'+q.step)} <button class=act data-cancel=${q.id}>${t('cancel')}</button>`).join(' ')}</div>`;
 if(J.history.length)h+=`<div style="margin-top:8px" class=muted>${t('recent')}</div>`+J.history.slice(0,8).map(x=>`<div class=hist>${esc(x.song)} · ${t('step_'+x.step)} · <span class=${x.status==='failed'?'bad':''}>${t('j_'+x.status)}</span>${x.status==='failed'?`：${esc(reason(x))} <details><summary>${t('showlog')}</summary><pre>${esc(x.log.join('\n'))}</pre></details>`:''}</div>`).join('');
 if(h!==jobsHtml){const open=[...box.querySelectorAll('details')].map(d=>d.open);box.innerHTML=h;jobsHtml=h;box.querySelectorAll('details').forEach((d,i)=>d.open=!!open[i]);
  box.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=()=>post('/api/cancel',JSON.stringify({id:+b.dataset.cancel})).then(poll));
  const pre=document.getElementById('log');if(pre)pre.scrollTop=pre.scrollHeight}
 const el=document.getElementById('elapsed');if(el&&J.current)el.textContent=t('elapsed',{s:Math.max(0,Math.round(J.now-J.current.started))})}
async function run(song,step,all){const cur=S.libraries[lib];const s=cur.songs.find(x=>x.name===song);
 const willSplit=step==='split'||(all&&!s.done.split);
 if(willSplit&&lib==='roformer_sw'&&S.hardware.gpu!=='cuda'&&!confirm(t('gpuwarn')))return;
 const body={library:lib,song};if(all)body.steps=null;else{body.steps=[step];if(step!=='split'&&step!=='player')body.force=true}
 try{const r=await post('/api/run',JSON.stringify(body));if(!r.ok)alert((await r.json()).error);await poll();document.getElementById('jobcard').scrollIntoView({behavior:'smooth',block:'nearest'})}catch(e){offline=true;renderJobs()}}
async function upload(files){const msg=document.getElementById('upmsg');
 for(const f of files){msg.textContent=t('up_busy',{n:f.name});const h={'Content-Type':'application/octet-stream','X-Filename':encodeURIComponent(f.name)};
  try{let r=await post('/api/upload',f,h);if(r.status===409&&(await r.clone().json()).error==='exists'){if(!confirm(t('up_exists',{n:f.name}))){msg.textContent='';continue}r=await post('/api/upload?overwrite=1',f,h)}
   const e=r.ok?null:(await r.json()).error;
   msg.textContent=r.ok?t('up_ok',{n:f.name}):e==='unsupported file type'?t('up_bad',{n:f.name}):e==='bad name'?t('up_name',{n:f.name}):e==='twin'?t('up_twin',{n:f.name}):e==='file too large'?t('up_big',{n:f.name}):e==='upload interrupted'?t('up_cut',{n:f.name}):t('up_err',{n:f.name})}
  catch(e){offline=true;msg.textContent=t('up_err',{n:f.name})}}
 if(S&&!S.libraries[lib].split){lib='demucs6';try{localStorage.setItem('om-lib',lib)}catch(_){}}poll()}
const drop=document.getElementById('drop'),file=document.getElementById('file');
file.onchange=()=>{upload([...file.files]);file.value=''};
['dragenter','dragover'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('over')}));
drop.addEventListener('drop',ev=>upload([...ev.dataTransfer.files]));
let timer=null;
async function poll(){clearTimeout(timer);try{const r=await fetch('/api/state');S=await r.json();offline=false;render()}catch(e){offline=true;if(S)renderJobs()}
 const busy=S&&(S.jobs.current||S.jobs.queue.length);timer=setTimeout(poll,document.hidden?10000:busy?1000:5000)}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});
applyLang();poll();
</script></body></html>"""

if __name__ == "__main__":
    main()
