/**
 * OpenMusic × DeepSeek Harness bridge.
 *
 * The OpenMusic pipeline (Demucs / BS-RoFormer, Beat This!, Basic Pitch, the drum model and the Guitar Pro
 * writer) is Python with model weights and a virtualenv; it cannot run inside the Harness process.  This
 * plugin therefore drives the *installed* app from a session: it starts the existing workbench (app.py, which
 * already queues one heavy job at a time) and talks to its local JSON API.  No pipeline code is duplicated
 * and no original behaviour changes.
 *
 * Everything here is standard-library Node: no npm dependencies, so the bundle installs as a plain directory.
 */
import { spawn, execFileSync } from "node:child_process";
import { existsSync, mkdirSync, openSync, closeSync, readFileSync, statSync, writeFileSync, unlinkSync, chmodSync } from "node:fs";
import { createServer } from "node:net";
import { basename, dirname, join, resolve, sep } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const STEPS = ["split", "analyze", "transcribe", "gp", "player"];
const STEP_LABEL = { split: "split", analyze: "analyze", transcribe: "MIDI", gp: "Guitar Pro", player: "player" };
const AUDIO_EXT = /\.(mp3|m4a|wav|flac|aac|ogg)$/i;
const MAX_UPLOAD = 400 * 1024 * 1024;
const DEFAULT_PORTS = Array.from({ length: 20 }, (_, i) => 8780 + i);

/**
 * Workbenches this plugin started (or adopted from its own PID file).  The map and the exit hook live on the
 * `process` object so a hot reload keeps them, and the hook kills exactly those processes when the Harness
 * exits.  A workbench started by OpenMusic.command / the user is never touched.
 */
const OWNED_KEY = Symbol.for("openmusic-dsh.owned-servers");
const EXIT_KEY = Symbol.for("openmusic-dsh.exit-hook");
const VISIBLE_KEY = Symbol.for("openmusic-dsh.terminal-servers");
const ownedServers = process[OWNED_KEY] ?? (process[OWNED_KEY] = new Map());
/** Workbenches the plugin opened in a Terminal / cmd window: the user stops those with Ctrl+C. */
const terminalServers = process[VISIBLE_KEY] ?? (process[VISIBLE_KEY] = new Map());

function workbenchStateFile(root) {
  return join(root, ".cache", "dsh-openmusic", "workbench.json");
}

function readServerState(root) {
  try {
    return JSON.parse(readFileSync(workbenchStateFile(root), "utf8"));
  } catch {
    return undefined;
  }
}

function writeServerState(server) {
  try {
    const file = workbenchStateFile(server.root);
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(
      file,
      JSON.stringify({ version: 1, pid: server.pid, port: server.port, root: server.root, url: server.url }),
    );
  } catch {
    // Best effort: without the file the server still works, it just cannot be adopted after a reload.
  }
}

function clearServerState(server) {
  try {
    const file = workbenchStateFile(server.root);
    const saved = readServerState(server.root);
    if (!saved || saved.pid === server.pid) unlinkSync(file);
  } catch {
    // Already gone.
  }
}

function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** Stop one process and, on Windows, its job children; SIGTERM first, SIGKILL when forced. */
function killProcessTree(pid, force) {
  if (!Number.isInteger(pid) || pid <= 0) return;
  if (process.platform === "win32") {
    try {
      execFileSync("taskkill", ["/PID", String(pid), "/T", ...(force ? ["/F"] : [])], { stdio: "ignore" });
      return;
    } catch {
      // Fall through to process.kill.
    }
  }
  try {
    process.kill(pid, force ? "SIGKILL" : "SIGTERM");
  } catch {
    // Already gone.
  }
}

function ensureExitHook() {
  if (process[EXIT_KEY]) return;
  process[EXIT_KEY] = true;
  process.once("exit", () => {
    for (const server of ownedServers.values()) {
      killProcessTree(server.pid, false);
    }
  });
}

/** Claim a running workbench only when its own PID file names the same port and that PID is alive. */
function adoptSaved(running) {
  const root = rootFromState(running.state);
  if (!root) return undefined;
  const saved = readServerState(root);
  if (!saved || saved.port !== running.port || !processAlive(saved.pid)) return undefined;
  const server = { pid: saved.pid, root, port: running.port, url: running.url, adopted: true };
  ownedServers.set(running.url, server);
  ensureExitHook();
  return server;
}

/** Stop one workbench this plugin owns: SIGTERM first, SIGKILL if it is still there after five seconds. */
async function stopServer(server) {
  killProcessTree(server.pid, false);
  for (let i = 0; i < 50 && processAlive(server.pid); i++) await delay(100);
  if (processAlive(server.pid)) killProcessTree(server.pid, true);
  ownedServers.delete(server.url);
  clearServerState(server);
}

/** The project folder the DSH page last opened (from its URL or a tool call), reused by later panel requests. */
let knownRoot;

function rememberRoot(root) {
  if (typeof root === "string" && root && isProjectDir(root)) {
    knownRoot = root;
    return root;
  }
  return undefined;
}

/** The most recently created live Session whose working directory is inside an OpenMusic checkout. */
function lastSessionRoot(ctx) {
  try {
    const sessions = ctx.get("sessions");
    if (!sessions) return undefined;
    const live = sessions.list();
    for (let index = live.length - 1; index >= 0; index--) {
      const cwd = live[index]?.header?.cwd;
      const found = typeof cwd === "string" && cwd ? walkUp(cwd) : undefined;
      if (found) return found;
    }
  } catch {
    // sessions is optional; the panel still works from OPENMUSIC_ROOT or a running workbench.
  }
  return undefined;
}

/** Root for a page request with no explicit folder: remembered, env, running workbench, then a live Session. */
async function autoRoot(ctx) {
  if (knownRoot && isProjectDir(knownRoot)) return knownRoot;
  const env = process.env.OPENMUSIC_ROOT?.trim();
  if (env) {
    const root = resolve(env);
    if (isProjectDir(root)) return rememberRoot(root);
  }
  const running = await findWorkbench();
  const fromState = running && rootFromState(running.state);
  if (fromState) return rememberRoot(fromState);
  return rememberRoot(lastSessionRoot(ctx));
}

const textOutput = {
  schema: {
    type: "object",
    additionalProperties: false,
    properties: { text: { type: "string" } },
    required: ["text"],
  },
  render: (_args, value) => [{ type: "text", text: value.text }],
};

/* ------------------------------------------------------------------ paths */

function isProjectDir(dir) {
  return existsSync(join(dir, "experiments", "app.py")) && existsSync(join(dir, "experiments", "analyze.py"));
}

function walkUp(start) {
  let dir = resolve(start);
  for (let i = 0; i < 8; i++) {
    if (isProjectDir(dir)) return dir;
    const up = dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return undefined;
}

function sessionCwd(exec) {
  const cwd = exec?.agent?.session?.header?.cwd;
  return typeof cwd === "string" && cwd.length > 0 ? cwd : undefined;
}

function pythonFor(root) {
  const explicit = process.env.OPENMUSIC_PYTHON?.trim();
  if (explicit) return explicit;
  const venv =
    process.platform === "win32"
      ? join(root, ".venv", "Scripts", "python.exe")
      : join(root, ".venv", "bin", "python");
  if (existsSync(venv)) return venv;
  return process.platform === "win32" ? "python" : "python3";
}

/** Project root from one library's absolute path: <root>/outputs/<library>. */
function rootFromState(state) {
  for (const lib of Object.values(state?.libraries ?? {})) {
    if (typeof lib?.path !== "string") continue;
    const parts = lib.path.split(sep);
    const index = parts.lastIndexOf("outputs");
    if (index > 0) return parts.slice(0, index).join(sep);
  }
  return undefined;
}

/* --------------------------------------------------------------- workbench */

async function probe(port) {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/state`, {
      signal: AbortSignal.timeout(500),
    });
    if (!response.ok) return undefined;
    const state = await response.json();
    return state && state.libraries ? { url: `http://127.0.0.1:${port}`, port, state } : undefined;
  } catch {
    return undefined;
  }
}

/** The workbench already running on this computer (started by the launchers, the user, or this plugin). */
async function findWorkbench() {
  const found = await Promise.all(DEFAULT_PORTS.map(probe));
  return found.filter(Boolean).sort((a, b) => a.port - b.port)[0];
}

function portFree(port) {
  return new Promise((done) => {
    const server = createServer();
    server.once("error", () => done(false));
    server.once("listening", () => server.close(() => done(true)));
    server.listen(port, "127.0.0.1");
  });
}

function tail(file, bytes = 4000) {
  try {
    const text = readFileSync(file, "utf8");
    return text.slice(-bytes);
  } catch {
    return "";
  }
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, "'\\''")}'`;
}

async function firstFreePort(preferredPort) {
  const start = Number.isInteger(preferredPort) ? preferredPort : 8780;
  for (let offset = 0; offset < 10; offset++) {
    const port = start + offset;
    if (await portFree(port)) return port;
  }
  throw new Error(`No free port in ${start}–${start + 9} for the OpenMusic workbench.`);
}

/** A small launcher the user can see and Ctrl+C, in the same spirit as OpenMusic.command / OpenMusic.bat. */
function writeStartScript(root, python, port) {
  const dir = join(root, ".cache", "dsh-openmusic");
  mkdirSync(dir, { recursive: true });
  if (process.platform === "win32") {
    const file = join(dir, "start-workbench.bat");
    const quote = (value) => `"${String(value).replace(/%/g, "%%").replace(/"/g, '""')}"`;
    writeFileSync(
      file,
      `@echo off\r\ncd /d ${quote(root)}\r\n${quote(python)} experiments\\app.py --no-browser --port ${port}\r\n` +
        `echo.\r\necho OpenMusic workbench stopped. Press any key to close.\r\npause >nul\r\n`,
    );
    return file;
  }
  const file = join(dir, "start-workbench.command");
  writeFileSync(
    file,
    `#!/bin/sh\ncd ${shellQuote(root)} || exit 1\nexec ${shellQuote(python)} experiments/app.py --no-browser --port ${port}\n`,
    { mode: 0o755 },
  );
  try {
    chmodSync(file, 0o755);
  } catch {
    // The mode option above normally suffices.
  }
  return file;
}

/**
 * Open the workbench in a Terminal (macOS) or cmd (Windows) window so the user can watch it and stop it with
 * Ctrl+C.  Returns undefined only when a terminal could not be opened at all; once the window is open, a
 * failure to start is reported instead of silently starting a second, hidden workbench.
 */
async function startVisibleWorkbench(root, preferredPort) {
  if (process.platform !== "darwin" && process.platform !== "win32") return undefined;
  const port = await firstFreePort(preferredPort);
  const python = pythonFor(root);
  const script = writeStartScript(root, python, port);
  const url = `http://127.0.0.1:${port}`;
  try {
    if (process.platform === "darwin") {
      execFileSync("open", ["-a", "Terminal", script], { stdio: "ignore" });
    } else {
      spawn("cmd.exe", ["/c", "start", "", script], {
        cwd: root,
        detached: true,
        stdio: "ignore",
        windowsHide: false,
      }).unref();
    }
  } catch (error) {
    return undefined;
  }
  const deadline = Date.now() + 45_000;
  while (Date.now() < deadline) {
    const hit = await probe(port);
    if (hit) {
      terminalServers.set(hit.url, { root, port, url: hit.url, terminal: true });
      return hit;
    }
    await delay(300);
  }
  throw new Error(
    `A Terminal window was opened in ${root}, but the workbench did not answer on ${url} within 45 s. ` +
      `Check that window for the error (is the .venv installed?), or set OPENMUSIC_PYTHON.`,
  );
}

/** Terminal first (visible and user-stopped); a hidden child only where no terminal can be opened. */
async function startWorkbench(root, preferredPort) {
  const visible = await startVisibleWorkbench(root, preferredPort);
  if (visible) return visible;
  return startHiddenWorkbench(root, preferredPort);
}

async function startHiddenWorkbench(root, preferredPort) {
  const python = pythonFor(root);
  const logDir = join(root, ".cache", "dsh-openmusic");
  mkdirSync(logDir, { recursive: true });
  const logPath = join(logDir, "workbench.log");
  const logFd = openSync(logPath, "a");

  const start = Number.isInteger(preferredPort) ? preferredPort : 8780;
  let lastError = "";
  try {
    for (let offset = 0; offset < 10; offset++) {
      const port = start + offset;
      if (!(await portFree(port))) continue;
      const proc = spawn(python, ["experiments/app.py", "--no-browser", "--port", String(port)], {
        cwd: root,
        stdio: ["ignore", logFd, logFd],
        env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
      });
      const url = `http://127.0.0.1:${port}`;
      const deadline = Date.now() + 30_000;
      while (Date.now() < deadline) {
        if (proc.exitCode !== null) break;
        const hit = await probe(port);
        if (hit) {
          const server = { proc, pid: proc.pid, root, port, url };
          ownedServers.set(url, server);
          writeServerState(server);
          ensureExitHook();
          proc.once("exit", () => {
            if (ownedServers.get(url) === server) ownedServers.delete(url);
            clearServerState(server);
          });
          return hit;
        }
        await delay(300);
      }
      if (proc.exitCode !== null) {
        lastError = tail(logPath).trim().split("\n").slice(-3).join(" ");
      } else {
        proc.kill("SIGTERM");
        lastError = "workbench did not answer /api/state within 30 s";
      }
    }
  } finally {
    closeSync(logFd);
  }
  throw new Error(
    `Could not start the OpenMusic workbench with ${python}${lastError ? `: ${lastError}` : ""}. ` +
      `Run "${python} experiments/app.py" once in ${root} to see the full error, or set OPENMUSIC_PYTHON.`,
  );
}

async function ensureWorkbench(root, preferredPort) {
  const existing = await findWorkbench();
  if (existing) {
    rememberRoot(rootFromState(existing.state));
    if (!ownedServers.has(existing.url)) adoptSaved(existing);
    return existing;
  }
  if (!root) {
    throw new Error(
      "OpenMusic is not running and no project folder was found. Open the session in the OpenMusic folder, " +
        "pass root (for example the folder containing experiments/app.py), or set OPENMUSIC_ROOT.",
    );
  }
  rememberRoot(root);
  return startWorkbench(root, preferredPort);
}

/** Project folder for a tool call: explicit root, OPENMUSIC_ROOT, the session folder, or a running workbench. */
async function projectRoot(args, exec) {
  const explicit = args?.root ?? process.env.OPENMUSIC_ROOT;
  if (typeof explicit === "string" && explicit.trim()) {
    const root = resolve(sessionCwd(exec) ?? process.cwd(), explicit);
    if (!isProjectDir(root)) {
      throw new Error(`${root} does not look like the OpenMusic project (experiments/app.py is missing).`);
    }
    return rememberRoot(root);
  }
  const fromSession = walkUp(sessionCwd(exec) ?? process.cwd());
  if (fromSession) return rememberRoot(fromSession);
  const running = await findWorkbench();
  const fromState = running && rootFromState(running.state);
  if (fromState) return rememberRoot(fromState);
  throw new Error(
    "Cannot find the OpenMusic project. Pass root=<folder containing experiments/app.py>, or open the session there.",
  );
}

/* -------------------------------------------------------------------- HTTP */

async function request(url, options = {}) {
  const { method = "GET", headers = {}, body, timeout = 15_000 } = options;
  let response;
  try {
    response = await fetch(url, { method, headers, body, signal: AbortSignal.timeout(timeout) });
  } catch (error) {
    throw new Error(`Cannot reach ${url}: ${error?.message ?? error}`);
  }
  return response;
}

async function json(url, options) {
  const response = await request(url, options);
  const data = await response.json().catch(() => undefined);
  if (!response.ok) {
    const reason = data?.error ? String(data.error) : `HTTP ${response.status}`;
    const detail = data?.detail ? ` (${data.detail})` : "";
    throw new Error(`${reason}${detail}`);
  }
  if (data === undefined) throw new Error(`${url} did not return JSON`);
  return data;
}

function postJson(url, path, body, timeout = 20_000) {
  return json(`${url}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-OpenMusic": "1" },
    body: JSON.stringify(body),
    timeout,
  });
}

/* ------------------------------------------------------------------ output */

function stepMarks(song) {
  return STEPS.map((step) => (song.done?.[step] ? "✓" : "·")).join("");
}

function songLine(song) {
  const parts = [`  ${stepMarks(song)}  ${song.name}`];
  if (song.model) parts.push(`[${song.model}]`);
  if (song.analysis && !song.analysis.error) {
    const analysis = song.analysis;
    const bar = analysis.beats_per_bar ? `${analysis.beats_per_bar}/4` : "?/4";
    const key = analysis.key_zh || analysis.key_en || "";
    parts.push(`${analysis.bpm} BPM · ${bar}${key ? ` · ${key}` : ""}`);
  }
  if (song.split_stale) parts.push("(stems older than the song file)");
  return parts.join(" ");
}

function songDetail(song, library, workbenchUrl, root) {
  const lines = [];
  lines.push(`${song.name}  [library ${library.key}]`);
  const done = STEPS.map((step) => `${STEP_LABEL[step]} ${song.done?.[step] ? "✓" : "·"}`).join(" · ");
  lines.push(`  steps: ${done}`);
  if (song.stems?.length) lines.push(`  stems: ${song.stems.join(", ")}${song.model ? ` (${song.model})` : ""}`);
  if (song.analysis && !song.analysis.error) {
    const a = song.analysis;
    const confidence = typeof a.confidence === "number" ? ` (${Math.round(a.confidence * 100)}% sure)` : "";
    lines.push(`  analysis: ${a.bpm} BPM · ${a.beats_per_bar ?? "?"} beats/bar · ${a.key_zh || a.key_en}${confidence}`);
  }
  if (song.midi?.length) lines.push(`  MIDI: ${song.midi.join(", ")}`);
  if (song.split_stale) lines.push("  ! the song file is newer than its stems: run split again");
  const projectFolder = join(root, library.root, song.name);
  const gp = join(projectFolder, "transcription", `${song.name}.gp`);
  if (existsSync(gp)) lines.push(`  Guitar Pro file: ${gp}`);
  if (song.player_url) lines.push(`  player: ${workbenchUrl}${song.player_url}`);
  else if (song.stems?.length) lines.push("  player: not built yet (run the player step)");
  return lines.join("\n");
}

function jobsText(jobs) {
  const lines = [];
  if (jobs.current) lines.push(`  running: #${jobs.current.id} ${jobs.current.song} → ${jobs.current.step}`);
  for (const job of jobs.queue ?? []) lines.push(`  queued: #${job.id} ${job.song} → ${job.step}`);
  for (const job of (jobs.history ?? []).slice(0, 3)) {
    const when = job.ended ? new Date(job.ended * 1000).toLocaleTimeString() : "";
    lines.push(`  last: #${job.id} ${job.song} → ${job.step}: ${job.status}${when ? ` (${when})` : ""}`);
  }
  return lines.length > 0 ? lines.join("\n") : "  none";
}

function jobText(job, withLog) {
  const head = `#${job.id} ${job.song} → ${job.step}: ${job.status}`;
  if (!withLog) return head;
  const log = (job.log ?? []).slice(-12).map((line) => `    ${line}`).join("\n");
  return log ? `${head}\n${log}` : head;
}

/* ------------------------------------------------------- DSH page / proxy */

const PANEL_PREFIX = "/openmusic";

function panelJson(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function panelError(status, message) {
  const safe = String(message).replace(/[&<>"]/g, (character) => `&#${character.charCodeAt(0)};`);
  return new Response(
    `<!doctype html><meta charset="utf-8"><title>OpenMusic</title>` +
      `<body style="font:14px/1.6 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;padding:2rem;max-width:46rem;margin:auto">` +
      `<h1 style="font-size:1.1rem">OpenMusic</h1><p>${safe}</p></body>`,
    { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } },
  );
}

/** The page shown inside the DSH tab before a project folder is known. */
function panelSetupPage() {
  return new Response(
    `<!doctype html><meta charset="utf-8"><title>OpenMusic</title>` +
      `<body style="font:14px/1.6 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;padding:2rem;max-width:46rem;margin:auto">` +
      `<h1 style="font-size:1.1rem">OpenMusic</h1>` +
      `<p>没有找到 OpenMusic 项目目录。请在 DSH 的 OpenMusic 页面里填写项目文件夹（包含 <code>experiments/app.py</code>），` +
      `或设置环境变量 <code>OPENMUSIC_ROOT</code>。</p>` +
      `<p>No OpenMusic project folder was found. Set it in the OpenMusic page in DSH, or set <code>OPENMUSIC_ROOT</code>.</p>` +
      `</body>`,
    { status: 200, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } },
  );
}

/** Point the workbench page's root-absolute URLs at our same-origin prefix so the page works inside DSH. */
function rewriteHtml(text) {
  return text
    .replace(/(["'`])\/api\//g, "$1/openmusic/api/")
    .replace(/(["'`])\/outputs\//g, "$1/openmusic/outputs/")
    .replace(/href=(["'])\/\?lang=/g, "href=$1/openmusic/app?lang=");
}

/** /api/state returns absolute /outputs/... links; the player and MIDI links must stay inside the prefix. */
function rewriteJson(text) {
  return text.replace(/"\/outputs\//g, '"/openmusic/outputs/');
}

async function forwardToWorkbench(request, target, upstreamPath) {
  const headers = new Headers();
  for (const [name, value] of request.headers) {
    if (["host", "connection", "content-length", "accept-encoding", "cookie", "origin", "referer"].includes(name)) {
      continue;
    }
    headers.set(name, value);
  }

  let body;
  if (request.method !== "GET" && request.method !== "HEAD") {
    body = Buffer.from(await request.arrayBuffer());
    if (upstreamPath === "/api/gp" && body.length > 0) {
      // The player names its library as "outputs/<library>" (or "openmusic/outputs/<library>" through the
      // proxy); /api/gp expects the path relative to the project.
      try {
        const data = JSON.parse(body.toString("utf8"));
        if (typeof data.root === "string" && data.root.startsWith("openmusic/")) {
          data.root = data.root.slice("openmusic/".length);
          body = Buffer.from(JSON.stringify(data));
        }
      } catch {
        // Not JSON: forward the body untouched.
      }
    }
  }

  const slow = upstreamPath === "/api/gp" || upstreamPath.startsWith("/api/upload");
  let upstream;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body,
      redirect: "manual",
      signal: AbortSignal.timeout(slow ? 300_000 : 90_000),
    });
  } catch (error) {
    return panelError(502, `Cannot reach the OpenMusic workbench: ${error?.message ?? error}`);
  }

  const responseHeaders = new Headers();
  for (const [name, value] of upstream.headers) {
    if (
      ["x-frame-options", "content-security-policy", "content-encoding", "content-length", "transfer-encoding",
        "connection", "set-cookie"].includes(name)
    ) {
      continue;
    }
    responseHeaders.set(name, value);
  }

  const type = upstream.headers.get("content-type") ?? "";
  if (upstream.ok && type.includes("text/html")) {
    return new Response(rewriteHtml(await upstream.text()), { status: upstream.status, headers: responseHeaders });
  }
  if (upstream.ok && type.includes("application/json")) {
    return new Response(rewriteJson(await upstream.text()), { status: upstream.status, headers: responseHeaders });
  }
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
}

async function panelInfo(ctx, url) {
  const requested = url.searchParams.get("root");
  let error;
  if (requested) {
    const candidate = resolve(process.cwd(), requested);
    if (isProjectDir(candidate)) rememberRoot(candidate);
    else error = `${candidate} 里没有 experiments/app.py`;
  }
  const running = await findWorkbench();
  const root = error
    ? rememberRoot(knownRoot) ?? null
    : (requested ? rememberRoot(resolve(process.cwd(), requested)) : await autoRoot(ctx)) ?? null;
  return panelJson({
    root,
    running: Boolean(running),
    url: running?.url ?? null,
    ...(error ? { error } : {}),
  });
}

/** The /openmusic/* resource behind the DSH page: information, the workbench page, its API and its files. */
function panelResource(ctx) {
  return async (request) => {
    const url = new URL(request.url);
    const path = url.pathname;
    if (path === `${PANEL_PREFIX}/info`) return panelInfo(ctx, url);
    if (path === PANEL_PREFIX || path === `${PANEL_PREFIX}/`) {
      return new Response(null, { status: 302, headers: { location: `${PANEL_PREFIX}/app${url.search}` } });
    }

    try {
      const requested = url.searchParams.get("root");
      let root;
      if (requested) {
        const candidate = resolve(process.cwd(), requested);
        if (!isProjectDir(candidate)) return panelError(400, `${candidate} 里没有 experiments/app.py`);
        root = rememberRoot(candidate);
      } else {
        root = await autoRoot(ctx);
      }
      if (!root) return panelSetupPage();

      const workbench = await ensureWorkbench(root, 8780);
      const actualRoot = rootFromState(workbench.state);
      if (actualRoot) rememberRoot(actualRoot);

      let upstreamPath;
      if (path === `${PANEL_PREFIX}/app` || path === `${PANEL_PREFIX}/app/`) upstreamPath = "/app";
      else if (path.startsWith(`${PANEL_PREFIX}/api/`)) upstreamPath = path.slice(PANEL_PREFIX.length);
      else if (path.startsWith(`${PANEL_PREFIX}/outputs/`)) upstreamPath = path.slice(PANEL_PREFIX.length);
      else if (path === `${PANEL_PREFIX}/compare.html` || path === `${PANEL_PREFIX}/compare.en.html`) {
        upstreamPath = path.slice(PANEL_PREFIX.length);
      } else return panelError(404, "Not found");

      const search = new URLSearchParams(url.search);
      search.delete("root");
      const query = search.toString();
      return await forwardToWorkbench(request, workbench.url + upstreamPath + (query ? `?${query}` : ""), upstreamPath);
    } catch (error) {
      return panelError(502, error?.message ?? String(error));
    }
  };
}

/* ------------------------------------------------------------------- tools */

function findSong(state, name, onlyLibrary) {
  const matches = [];
  for (const [key, library] of Object.entries(state.libraries ?? {})) {
    if (onlyLibrary && key !== onlyLibrary) continue;
    for (const song of library.songs ?? []) {
      if (song.name === name) matches.push({ key, library, song });
    }
  }
  return matches;
}

function defaultLibrary(state, name) {
  for (const key of ["roformer_sw", "mp3", "demucs6"]) {
    const library = state.libraries?.[key];
    if (library?.songs?.some((song) => song.name === name && song.stems?.length)) return key;
  }
  return state.hardware?.gpu === "cuda" ? "roformer_sw" : "demucs6";
}

function workbenchPort(args) {
  const value = Number(args?.port ?? process.env.OPENMUSIC_PORT);
  return Number.isInteger(value) && value > 0 ? value : 8780;
}

function definitions() {
  return [
    {
      name: "openmusic_status",
      description:
        "Show the local OpenMusic library: every song, which pipeline steps are done (split, analyze, MIDI, " +
        "Guitar Pro, player), the current/queued jobs, and the workbench URL. Starts the workbench if needed. " +
        "Pass song for one song's details (analysis, stems, MIDI, .gp path, player URL).",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          song: { type: "string", description: "Exact song name (as shown by openmusic_status). Omit for the library overview." },
          library: {
            type: "string",
            enum: ["demucs6", "roformer_sw", "mp3"],
            description: "Limit the view to one library. mp3 is the hand-collected library.",
          },
          root: { type: "string", description: "OpenMusic project folder. Defaults to the session folder / OPENMUSIC_ROOT." },
          port: { type: "integer", description: "Preferred workbench port when it has to be started (default 8780)." },
        },
      },
      output: textOutput,
      timeoutMs: 60_000,
      isConcurrencySafe: () => true,
      async execute(args, exec) {
        const root = await projectRoot(args, exec);
        const workbench = await ensureWorkbench(root, workbenchPort(args));
        const state = workbench.state;
        const libraries = Object.entries(state.libraries ?? {}).filter(
          ([key, library]) => (!args.library || key === args.library) && (library.songs?.length ?? 0) > 0,
        );
        if (args.song) {
          const matches = findSong(state, args.song, args.library);
          if (matches.length === 0) {
            const names = libraries.flatMap(([key, library]) => library.songs.map((song) => `${song.name} [${key}]`));
            throw new Error(`Unknown song "${args.song}". Known songs: ${names.join("; ") || "(none)"}`);
          }
          return {
            text: [
              `OpenMusic workbench: ${workbench.url}`,
              ...matches.map(({ key, library, song }) =>
                songDetail(song, { ...library, key }, workbench.url, root),
              ),
              "",
              "jobs:",
              jobsText(state.jobs),
            ].join("\n"),
          };
        }
        const lines = [
          `OpenMusic workbench: ${workbench.url} (${state.hardware?.gpu ?? "unknown GPU"}, ${state.hardware?.ram_gb ?? "?"} GB RAM)`,
          `project: ${root}`,
        ];
        for (const [key, library] of libraries) {
          lines.push("", `library ${key} (${library.root}) — ${library.songs.length} songs`);
          for (const song of library.songs) lines.push(songLine(song));
        }
        lines.push("", "steps: split · analyze · MIDI · Guitar Pro · player", "jobs:", jobsText(state.jobs));
        return { text: lines.join("\n") };
      },
    },

    {
      name: "openmusic_process",
      description:
        "Process an OpenMusic song with the installed pipeline: split into 6 stems, detect tempo/key/chords, " +
        "transcribe to MIDI, write the Guitar Pro file and rebuild the practice player. Omit steps to run only " +
        "what is missing (the workbench queues one heavy job at a time). Returns the job logs; if wait_seconds " +
        "runs out the jobs keep running — call openmusic_status to follow them.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          song: { type: "string", description: "Exact song name (a file in songs/ that the workbench lists)." },
          library: {
            type: "string",
            enum: ["demucs6", "roformer_sw", "mp3"],
            description: "Where the stems live (or will be written). Defaults to the library that already has this song's stems, else the GPU-appropriate model.",
          },
          steps: {
            type: "array",
            items: { type: "string", enum: ["split", "analyze", "transcribe", "gp", "player"] },
            description: "Subset to run, in pipeline order. Omit for every missing step.",
          },
          force: { type: "boolean", description: "Redo steps whose outputs already exist." },
          wait_seconds: {
            type: "integer",
            description: "How long to wait for the jobs (default 120, max 590). 0 returns immediately.",
          },
          root: { type: "string", description: "OpenMusic project folder. Defaults to the session folder / OPENMUSIC_ROOT." },
          port: { type: "integer", description: "Preferred workbench port when it has to be started (default 8780)." },
        },
        required: ["song"],
      },
      output: textOutput,
      timeoutMs: 600_000,
      async execute(args, exec) {
        const root = await projectRoot(args, exec);
        const workbench = await ensureWorkbench(root, workbenchPort(args));
        const library = args.library ?? defaultLibrary(workbench.state, args.song);
        const started = await postJson(workbench.url, "/api/run", {
          library,
          song: args.song,
          steps: args.steps,
          force: Boolean(args.force),
        });
        const ids = started.jobs ?? [];
        if (ids.length === 0) return { text: `Nothing to do for "${args.song}" in library ${library}.` };

        const waitMs = Math.min(Math.max(Number(args.wait_seconds ?? 120), 0), 590) * 1000;
        const deadline = Date.now() + waitMs;
        const terminal = (job) => job && ["done", "failed", "cancelled", "skipped"].includes(job.status);
        let latest;
        let finished = [];
        do {
          latest = await json(`${workbench.url}/api/state`);
          const jobs = latest.jobs ?? {};
          const all = [jobs.current, ...(jobs.queue ?? []), ...(jobs.history ?? [])].filter(Boolean);
          finished = ids.map((id) => all.find((entry) => entry.id === id)).filter(Boolean);
          if (ids.every((id) => terminal(finished.find((entry) => entry.id === id)))) break;
          if (Date.now() >= deadline) break;
          await delay(2000);
        } while (Date.now() < deadline);

        const settled = ids.every((id) => terminal(finished.find((entry) => entry.id === id)));
        const header = `"${args.song}" → library ${library}: ${ids.map((id) => `#${id}`).join(", ")}`;
        if (!settled) {
          return {
            text: [
              header,
              `still running after ${Math.round(waitMs / 1000)} s; the workbench keeps working in the background.`,
              ...finished.map((job) => jobText(job, false)),
              "",
              "call openmusic_status (or openmusic_process with the same song and wait_seconds) to follow up.",
            ].join("\n"),
          };
        }
        const lines = [header];
        for (const id of ids) {
          const job = finished.find((entry) => entry.id === id);
          if (!job) {
            lines.push(`#${id}: no longer in the workbench history (it may have been queued before a restart)`);
            continue;
          }
          lines.push(jobText(job, true));
        }
        const failed = finished.some((job) => ["failed", "cancelled", "skipped"].includes(job.status));
        if (!failed) {
          const song = latest?.libraries?.[library]?.songs?.find((entry) => entry.name === args.song);
          if (song?.player_url) lines.push("", `player: ${workbench.url}${song.player_url}`);
        }
        return { text: lines.join("\n") };
      },
    },

    {
      name: "openmusic_import",
      description:
        "Add a local audio file (mp3, wav, flac, m4a, aac, ogg; up to 400 MB) to OpenMusic's songs/ folder so the " +
        "workbench lists it and it can be processed. Copies the file; the original stays where it is.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          path: { type: "string", description: "Audio file to import. Relative paths use the session folder." },
          overwrite: { type: "boolean", description: "Replace a song with the same file name." },
          root: { type: "string", description: "OpenMusic project folder. Defaults to the session folder / OPENMUSIC_ROOT." },
          port: { type: "integer", description: "Preferred workbench port when it has to be started (default 8780)." },
        },
        required: ["path"],
      },
      output: textOutput,
      timeoutMs: 600_000,
      async execute(args, exec) {
        const source = resolve(sessionCwd(exec) ?? process.cwd(), args.path);
        const name = basename(source);
        if (!AUDIO_EXT.test(name)) throw new Error(`${name} is not an audio file OpenMusic accepts (mp3, wav, flac, m4a, aac, ogg).`);
        let size;
        try {
          size = statSync(source).size;
        } catch {
          throw new Error(`File not found: ${source}`);
        }
        if (size <= 0) throw new Error(`${source} is empty.`);
        if (size > MAX_UPLOAD) throw new Error(`${name} is ${Math.round(size / 2 ** 20)} MB; the workbench accepts up to 400 MB.`);

        const root = await projectRoot(args, exec);
        const workbench = await ensureWorkbench(root, workbenchPort(args));
        const bytes = readFileSync(source);
        const response = await request(`${workbench.url}/api/upload${args.overwrite ? "?overwrite=1" : ""}`, {
          method: "POST",
          headers: { "X-OpenMusic": "1", "X-Filename": encodeURIComponent(name) },
          body: bytes,
          timeout: 300_000,
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          if (response.status === 409) {
            throw new Error(
              data.error === "twin"
                ? `Another file with the same stem already exists in songs/. Rename this file or delete the other one.`
                : `"${name}" is already in songs/. Pass overwrite=true to replace it.`,
            );
          }
          throw new Error(`${data.error ?? `HTTP ${response.status}`}${data.detail ? ` (${data.detail})` : ""}`);
        }
        return {
          text: `Imported ${name} (${Math.round(size / 2 ** 20)} MB) into ${root}/songs/. Song name: ${data.song ?? name}.`,
        };
      },
    },

    {
      name: "openmusic_export_gp",
      description:
        "Build a Guitar Pro 8 (.gp) file for a transcribed OpenMusic song and save it under outputs/_dsh_gp/. " +
        "Choose the audio track built into the score: stems (default vocals + guitar + other), the original song " +
        "file, or none. Use this when the user wants the .gp delivered; the regular export stores <song>.gp in " +
        "the song folder during the gp step.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          song: { type: "string", description: "Exact song name." },
          library: { type: "string", enum: ["demucs6", "roformer_sw", "mp3"], description: "Defaults to the library with this song's MIDI." },
          audio: { type: "string", enum: ["stems", "original", "none"], description: "Audio track inside the score (default stems)." },
          stems: {
            type: "array",
            items: { type: "string", enum: ["vocals", "drums", "bass", "guitar", "piano", "other"] },
            description: "Which stems to mix when audio=stems (default vocals, guitar, other).",
          },
          root: { type: "string", description: "OpenMusic project folder. Defaults to the session folder / OPENMUSIC_ROOT." },
          port: { type: "integer", description: "Preferred workbench port when it has to be started (default 8780)." },
        },
        required: ["song"],
      },
      output: textOutput,
      timeoutMs: 360_000,
      async execute(args, exec) {
        const root = await projectRoot(args, exec);
        const workbench = await ensureWorkbench(root, workbenchPort(args));
        const library = args.library ?? defaultLibrary(workbench.state, args.song);
        const audio = args.audio ?? "stems";
        const stems = audio === "stems" ? args.stems ?? ["vocals", "guitar", "other"] : undefined;
        if (audio === "stems" && (!stems.length || stems.some((stem) => !["vocals", "drums", "bass", "guitar", "piano", "other"].includes(stem)))) {
          throw new Error("audio=stems needs one or more of: vocals, drums, bass, guitar, piano, other.");
        }
        const response = await request(`${workbench.url}/api/gp`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-OpenMusic": "1" },
          body: JSON.stringify({ library, song: args.song, audio, ...(stems ? { stems } : {}) }),
          timeout: 300_000,
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(`${data.error ?? `HTTP ${response.status}`}${data.detail ? ` (${data.detail})` : ""}`);
        }
        const bytes = Buffer.from(await response.arrayBuffer());
        if (bytes.length === 0) throw new Error("The workbench returned an empty Guitar Pro file.");
        const folder = join(root, "outputs", "_dsh_gp");
        mkdirSync(folder, { recursive: true });
        const label = audio === "stems" ? stems.join(" ") : audio;
        const file = join(folder, `${args.song} (${label}).gp`);
        writeFileSync(file, bytes);
        return {
          text: `Guitar Pro file: ${file}\n${bytes.length} bytes · audio: ${audio === "stems" ? stems.join(" + ") : audio}. Open it in Guitar Pro 8, or present it to the user.`,
        };
      },
    },

    {
      name: "openmusic_open",
      description:
        "Get (and by default open in the browser) the OpenMusic workbench URL, or one song's practice player URL. " +
        "Starts the workbench if it is not running. The player has per-stem volume, A-B loop, speed/pitch, chords, " +
        "piano roll and the drum lane.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          target: { type: "string", enum: ["workbench", "player"], description: "What to open (default workbench)." },
          song: { type: "string", description: "Required for target=player: exact song name." },
          library: { type: "string", enum: ["demucs6", "roformer_sw", "mp3"], description: "Limit the song lookup to one library." },
          open_browser: { type: "boolean", description: "Open it in the default browser (default true)." },
          root: { type: "string", description: "OpenMusic project folder. Defaults to the session folder / OPENMUSIC_ROOT." },
          port: { type: "integer", description: "Preferred workbench port when it has to be started (default 8780)." },
        },
      },
      output: textOutput,
      timeoutMs: 60_000,
      async execute(args, exec) {
        const root = await projectRoot(args, exec);
        const workbench = await ensureWorkbench(root, workbenchPort(args));
        let url = `${workbench.url}/`;
        if (args.target === "player") {
          if (!args.song) throw new Error("target=player needs a song name.");
          const matches = findSong(workbench.state, args.song, args.library);
          if (matches.length === 0) throw new Error(`Unknown song "${args.song}".`);
          const match = matches.find((entry) => entry.song.player_url) ?? matches[0];
          if (!match.song.player_url) throw new Error(`"${args.song}" has no player page yet; run the player step first.`);
          url = `${workbench.url}${match.song.player_url}`;
        }
        const open = args.open_browser !== false;
        if (open) {
          const command =
            process.platform === "darwin"
              ? ["open", [url]]
              : process.platform === "win32"
                ? ["cmd", ["/c", "start", "", url]]
                : ["xdg-open", [url]];
          try {
            spawn(command[0], command[1], { detached: true, stdio: "ignore" }).unref();
          } catch {
            // The URL is still returned; the caller can open it by hand.
          }
        }
        return { text: `${args.target === "player" ? "Practice player" : "Workbench"}: ${url}${open ? " (opened in the browser)" : ""}` };
      },
    },

    {
      name: "openmusic_stop",
      description:
        "Stop a workbench this plugin started in the background. A workbench the plugin opened in a Terminal / " +
        "cmd window is stopped with Ctrl+C in that window; one started by OpenMusic.command / OpenMusic.bat is " +
        "left alone.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      output: textOutput,
      timeoutMs: 20_000,
      async execute() {
        const running = await findWorkbench();
        if (!running) return { text: "OpenMusic is not running." };
        if (terminalServers.has(running.url)) {
          return {
            text: `OpenMusic is running in a Terminal window at ${running.url}; press Ctrl+C there to stop it.`,
          };
        }
        const owned = ownedServers.get(running.url) ?? adoptSaved(running);
        if (!owned) {
          return {
            text: `OpenMusic is running at ${running.url}, but this plugin did not start it; close its terminal or use the launcher.`,
          };
        }
        await stopServer(owned);
        return { text: `Stopped the OpenMusic workbench at ${running.url}.` };
      },
    },
  ];
}

export const inject = ["tools", "webServer"];

export function apply(ctx) {
  for (const definition of definitions()) {
    ctx.effect(() => ctx.tools.register(definition));
  }
  ctx.effect(() =>
    ctx.webServer.register({ kind: "prefix", path: PANEL_PREFIX, resource: panelResource(ctx) }),
  );
}
