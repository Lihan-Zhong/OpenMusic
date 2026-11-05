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

插件会自己找到项目目录并启动工作台（默认 `http://localhost:8780`，已经开着就直接用）。

## 六个工具

| 工具 | 做什么 |
|---|---|
| `openmusic_status` | 列出每首歌的分轨 / 分析 / MIDI / Guitar Pro / 播放器完成情况，当前和排队的任务；给 `song` 时显示这首歌的详情、`.gp` 路径和播放器链接 |
| `openmusic_process` | 跑处理流程，可以只跑某几步；默认只跑还没做的步骤。工作台一次只跑一个任务 |
| `openmusic_import` | 把一个本地音频文件加进 `songs/`（mp3 / wav / flac / m4a / aac / ogg，最大 400 MB） |
| `openmusic_export_gp` | 按所选的谱内音频轨（分轨混音 / 原曲 / 不放）生成一份 `.gp`，存到 `outputs/_dsh_gp/` |
| `openmusic_open` | 打开工作台或某首歌的练习播放器（默认在浏览器里打开，也可以只返回链接） |
| `openmusic_stop` | 停止**这个插件启动的**工作台；用启动器开的工作台不动 |

处理长歌时，`openmusic_process` 默认最多等 120 秒（`wait_seconds` 可改，最多 590 秒）；超时后任务继续在后台跑，再调 `openmusic_status` 看进度就行。

## 它长什么样

插件只注册工具，**不改变 Harness 的界面**：

- 在会话里，工具调用会显示成普通的工具卡片（参数、输出、日志）；
- 曲库、工作台和练习播放器仍然在**浏览器标签页**里打开，就是现在的 OpenMusic 网页；
- 不会在 Harness 里多出一个「OpenMusic 页面」。如果想在 Harness 侧边栏里放一个歌曲列表和按钮，需要另做一个 Client UI 插件（目前的版本没有做，避免动到播放器）。

## 工作方式

1. 在 `8780`–`8799` 里找一个已经在跑的工作台；没有的话，用项目 `.venv` 的 Python 启动
   `experiments/app.py --no-browser --port <端口>`，日志在 `.cache/dsh-openmusic/workbench.log`。
2. 通过工作台的本地接口操作：`GET /api/state`、`POST /api/run`、`POST /api/upload`、`POST /api/gp`。
   这些接口本来只接受本机请求和 `X-OpenMusic` 头，插件在 Host 进程里调用，和网页用的是同一套。
3. 不往歌文件夹里写额外文件；只有 `openmusic_export_gp` 会把生成的谱写到 `outputs/_dsh_gp/`（已在 `.gitignore` 里）。

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
- 练习播放器还是独立的浏览器页面（音频处理需要本地服务器，不能直接塞进 Harness 的插件页）。
- 许可：AGPL-3.0，和 OpenMusic 仓库一致。

---

## English

A DeepSeek Harness bundle for OpenMusic. It exposes the **installed** app to a session: song status, split, analysis, MIDI, Guitar Pro and the practice player. It does not reimplement the pipeline. It starts (or reuses) the existing workbench `experiments/app.py` and calls the local JSON API that the workbench page itself uses, so the stems, scores and player are exactly the ones you get by hand, and `OpenMusic.command` / `OpenMusic.bat` keep working unchanged.

### Install

1. Install OpenMusic's Python environment first (see the main [README](../README.en.md)); the `.venv` must exist and `OpenMusic.command` must be able to start the workbench.
2. Ask the agent in DeepSeek Harness to install the bundle:

   > Install the directory `<OpenMusic>/dsh` as a bundle with plugin_manager.

   The package has no npm dependencies.
3. Open a session whose working directory is the OpenMusic folder and ask, for example, "show my library", "split, transcribe and export song X", or "open the practice player for song X".

### Tools

`openmusic_status` (library, steps, jobs, one song's details), `openmusic_process` (run the pipeline or selected steps; one heavy job at a time), `openmusic_import` (copy an audio file into `songs/`), `openmusic_export_gp` (`.gp` with a chosen audio track, saved under `outputs/_dsh_gp/`), `openmusic_open` (workbench or practice player in the browser) and `openmusic_stop` (stop only a workbench this plugin started).

### What it looks like

Tools only. Tool calls appear as ordinary tool cards in the conversation; the workbench and player stay in a browser tab as the same web pages. There is no extra OpenMusic page inside the Harness (a sidebar UI would be a separate Client plugin).

### How it works

It probes ports 8780–8799 for a running workbench, otherwise starts `experiments/app.py --no-browser` with the project's `.venv` Python (log: `.cache/dsh-openmusic/workbench.log`), then uses `GET /api/state`, `POST /api/run`, `POST /api/upload` and `POST /api/gp`. It writes nothing into song folders; only `openmusic_export_gp` writes a score to the git-ignored `outputs/_dsh_gp/`.

Environment: `OPENMUSIC_ROOT`, `OPENMUSIC_PYTHON`, `OPENMUSIC_PORT` (all optional). Licence: AGPL-3.0.
