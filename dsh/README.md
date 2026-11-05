# OpenMusic × DeepSeek Harness

**中文** | [English](#english)

这是 OpenMusic 的 DeepSeek Harness 插件（一个 bundle）：装进 Harness 以后，可以在对话里让 agent 直接查看曲库、分轨、分析、扒谱、导出 Guitar Pro，或者打开某首歌的练习播放器。

它**不重写** OpenMusic 的任何流程：Demucs / RoFormer、Beat This!、Basic Pitch、鼓谱模型、Guitar Pro 导出和练习播放器都还是原来那套 Python 代码。插件只是启动（或复用）工作台 `experiments/app.py`，调用它本来就有的本地 JSON 接口。所以：

- 工作台、播放器、导出的谱子跟手动操作完全一样；
- 原来的 `OpenMusic.command` / `OpenMusic.bat`、命令行流程都不受影响；
- 同一时间仍然只跑一个重任务（工作台自己的队列）。

## 装法

1. 先按主 [README](../README.md) 把 OpenMusic 的 Python 环境装好（`.venv` 里有依赖，`songs/` 里有歌，能双击 `OpenMusic.command` 打开工作台）。
2. 让 DeepSeek Harness 里的 agent 安装这个 bundle：

   > 用 plugin_manager 把 `<OpenMusic 文件夹>/dsh` 这个目录作为 bundle 安装。

   等价地，在支持本地 bundle 的 Harness 设置 → 插件里选择这个目录。它没有 npm 依赖，装完就能用。
3. 新开（或继续）一个**工作目录在 OpenMusic 文件夹里**的会话，然后直接说：

   > 看看我的曲库
   > 把「某首歌」分轨、扒谱并生成 Guitar Pro
   > 打开「某首歌」的练习播放器

插件会自己找到项目目录并启动工作台（没有开着的话，会像 `OpenMusic.command` 一样弹出一个终端窗口，默认 `http://localhost:8780`；已经开着就直接用）。

## 六个工具

| 工具 | 做什么 |
|---|---|
| `openmusic_status` | 列出每首歌的分轨 / 分析 / MIDI / Guitar Pro / 播放器完成情况，当前和排队的任务；给 `song` 时显示这首歌的详情、`.gp` 路径和播放器链接 |
| `openmusic_process` | 跑处理流程，可以只跑某几步；默认只跑还没做的步骤。工作台一次只跑一个任务 |
| `openmusic_import` | 把一个本地音频文件加进 `songs/`（mp3 / wav / flac / m4a / aac / ogg，最大 400 MB） |
| `openmusic_export_gp` | 按所选的谱内音频轨（分轨混音 / 原曲 / 不放）生成一份 `.gp`，存到 `outputs/_dsh_gp/` |
| `openmusic_open` | 打开工作台或某首歌的练习播放器（默认在浏览器里打开，也可以只返回链接） |
| `openmusic_stop` | 停止插件在**后台**启动的工作台；在终端窗口里启动的，按那个窗口的 Ctrl+C 停 |

处理长歌时，`openmusic_process` 默认最多等 120 秒（`wait_seconds` 可改，最多 590 秒）；超时后任务继续在后台跑，再调 `openmusic_status` 看进度就行。

## 它长什么样

装好后，Harness 左侧会多一个 **OpenMusic** 图标；点开是 Harness 中央的一个完整页面，里面就是原来的工作台和练习播放器（通过插件的同源代理挂进来）。拖歌、分轨、分析、扒谱、Guitar Pro 弹窗、中英文切换，以及播放器的波形、分轨音量、A-B 循环、变速变调都和网页版一样。页面顶栏只有三个按钮：刷新、在新标签页打开、更换项目目录。

- 不看页面时，也可以在会话里用下面六个工具，让 agent 代你操作；
- 工作台和播放器**不是重写的**：页面就是 `app.py` / `build_player.py` 生成的那一份，插件只负责把它挂到 DSH 的 `/openmusic/` 下并去掉防嵌套头；
- `app.py`、`build_player.py` 和所有 Python 流程都没有改动。

## 工作方式

1. 在 `8780`–`8799` 里找一个已经在跑的工作台；没有的话，像 `OpenMusic.command` 一样弹出一个终端窗口，在里面用项目
   `.venv` 的 Python 启动 `experiments/app.py --no-browser --port <端口>`。窗口归你：按 Ctrl+C 就停，DSH 退出时不会去动它。
   只有实在打不开终端时（例如 Linux 无图形界面），才退回到后台子进程，日志在 `.cache/dsh-openmusic/workbench.log`。
2. DSH 页面请求 `/openmusic/...` 时，插件的 Host 半代理到工作台：`/openmusic/app` → `/app`，
   `/openmusic/api/*` → `/api/*`，`/openmusic/outputs/*` → `/outputs/*`。代理只做三件事：改写页面里的绝对路径、去掉
   `X-Frame-Options` / `frame-ancestors`、把 `/api/gp` 请求里的 `openmusic/` 前缀去掉。`app.py` 完全不知道自己在被嵌入。
3. 通过工作台的本地接口操作：`GET /api/state`、`POST /api/run`、`POST /api/upload`、`POST /api/gp`。
   这些接口本来只接受本机请求和 `X-OpenMusic` 头，插件在 Host 进程里调用，和网页用的是同一套。
4. 不往歌文件夹里写额外文件；只有 `openmusic_export_gp` 会把生成的谱写到 `outputs/_dsh_gp/`（已在 `.gitignore` 里）。

## 配置

一般不用配。需要时可以用工具参数或环境变量：

| 变量 | 作用 |
|---|---|
| `OPENMUSIC_ROOT` | 项目目录；默认从会话工作目录向上找 `experiments/app.py`，再不行从正在跑的工作台推断 |
| `OPENMUSIC_PYTHON` | 启动工作台用的 Python；默认 `<项目>/.venv/bin/python`（Windows 是 `.venv\Scripts\python.exe`） |
| `OPENMUSIC_PORT` | 启动工作台的首选端口，默认 8780 |

## 限制

- 桌面端本地使用：需要装好 OpenMusic 的 Python 环境，模型权重还是第一次运行时下载到 `.cache/`；手机和在线网页版仍然没有。
- 插件不会把 Python 打进 Harness，也不会替用户跑安装；环境没装好时工具会报错并提示先装环境。
- 页面里的播放器是一个同源 iframe（音频处理仍跑在它自己的页面里），所以它的配色跟随 OpenMusic 自己的浅色/深色，而不是 Harness 主题；这是为了不重写播放器。
- 第一次打开页面时如果找不到项目目录（没有正在跑的工作台、会话也不在 OpenMusic 文件夹里），页面会让你填一次路径，记在这个浏览器里。
- 插件在终端窗口里启动的工作台是一个普通的前台进程：DSH 退出时**不会**替你关它，看完按那个窗口的 Ctrl+C 就停。这样不会有看不见的后台进程，也不会把正在跑的任务悄悄带走。
- 没有终端可用的兜底情况下，工作台才记在 `.cache/dsh-openmusic/workbench.json`，DSH 正常退出时收到 `SIGTERM`（Windows 上连子进程一起结束），也可以用 `openmusic_stop` 停；热重载/重启后能凭这个文件认领。
- 如果 DSH 被强制杀掉，退出钩子不会执行；下次打开面板或调用工具时会认领那个后台工作台，并在下一次正常退出时停掉它。也可以按 `workbench.json` 里的 PID 手动 `kill`。
- 许可：AGPL-3.0，和 OpenMusic 仓库一致。

---

## English

A DeepSeek Harness bundle for OpenMusic. It puts the **installed** app into a DSH page: a sidebar entry opens the original workbench and practice player, proxied same-origin under `/openmusic/`, and six tools let a session drive the same workbench. It does not reimplement the pipeline. It starts (or reuses) `experiments/app.py` and calls the local JSON API that the workbench page itself uses, so the stems, scores and player are exactly the ones you get by hand, and `OpenMusic.command` / `OpenMusic.bat` keep working unchanged.

### Install

1. Install OpenMusic's Python environment first (see the main [README](../README.en.md)); the `.venv` must exist and `OpenMusic.command` must be able to start the workbench.
2. Ask the agent in DeepSeek Harness to install the bundle:

   > Install the directory `<OpenMusic>/dsh` as a bundle with plugin_manager.

   The package has no npm dependencies.
3. Open a session whose working directory is the OpenMusic folder and ask, for example, "show my library", "split, transcribe and export song X", or "open the practice player for song X".

### Tools

`openmusic_status` (library, steps, jobs, one song's details), `openmusic_process` (run the pipeline or selected steps; one heavy job at a time), `openmusic_import` (copy an audio file into `songs/`), `openmusic_export_gp` (`.gp` with a chosen audio track, saved under `outputs/_dsh_gp/`), `openmusic_open` (workbench or practice player in the browser) and `openmusic_stop` (stop a background workbench this plugin started; a workbench in a Terminal window is stopped with Ctrl+C there).

### What it looks like

A sidebar **OpenMusic** icon opens a full page in the Harness; inside it is the original workbench and practice player, served through the plugin's same-origin proxy. Dragging songs, splitting, analysis, transcription, the Guitar Pro dialog, the language switch and the player (waveform, per-stem volume, A-B loop, speed/pitch) behave exactly as on the web. The page bar only adds refresh, open-in-new-tab and change-folder. The same capabilities are also available as tool calls in the conversation.

### How it works

It probes ports 8780–8799 for a running workbench. If there is none, it opens a Terminal (macOS) or cmd (Windows) window that runs `experiments/app.py --no-browser` with the project's `.venv` Python — a normal foreground process the user stops with Ctrl+C; the Harness does not close it. Only when no terminal can be opened does it fall back to a hidden child (log: `.cache/dsh-openmusic/workbench.log`). Requests for `/openmusic/app`, `/openmusic/api/*` and `/openmusic/outputs/*` are proxied to `/app`, `/api/*` and `/outputs/*`; the proxy rewrites root-absolute URLs, strips `X-Frame-Options` / `frame-ancestors`, and removes the `openmusic/` prefix from the player's `/api/gp` body. `app.py` is untouched and does not know it is embedded.

### Limits

The page needs the OpenMusic Python environment and a running workbench. When none is running and the plugin can open a terminal, that window is a normal foreground process: it is not closed when the Harness exits — press Ctrl+C in it. In the no-terminal fallback the workbench is recorded in `.cache/dsh-openmusic/workbench.json`, gets `SIGTERM` when the Harness exits (on Windows its child processes are ended too), and `openmusic_stop` stops it. If the Harness is force-killed the exit hook cannot run; opening the page again adopts that background workbench, and the next normal exit stops it — or kill the PID in `workbench.json` by hand. The player is a same-origin iframe, so it keeps OpenMusic's own light/dark theme rather than the Harness theme. Environment: `OPENMUSIC_ROOT`, `OPENMUSIC_PYTHON`, `OPENMUSIC_PORT` (all optional). Licence: AGPL-3.0.
