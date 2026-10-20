# OpenMusic

**中文** | [English](README.en.md)

> 本项目的默认语言是中文（对比试听页等界面也都是中文），也可以选择阅读[英文版 README](README.en.md)。

一个给乐手用的开源"分轨练习"小工具的实验仓库：把一首歌拆成人声 / 鼓 / 贝斯 / 吉他 / 其他，
再在本地网页里静音、独奏、对比着听。目标是 Python 跑在自己电脑上的小软件，不做手机版。

现在已经有一个能用的**练习播放器**（见下一节）。仓库里也保留了分轨方法的对比实验：传统信号处理方法和 AI 模型（Demucs、BS-RoFormer）各自分得怎么样。

## 练习播放器（练琴、扒谱用）

歌分好轨以后，在本地网页里练：

- **每轨音量**：−∞ 到 +12 dB，可以把贝斯调得比原曲还响；静音、独奏；总音量带限幅器，调大也不会破音。
- **速度、调式、和弦**：自动识别 BPM、每一拍和小节线；调式给出把握程度和第二可能（通常是关系大小调）；时间轴上显示和弦，并提示当前和下一个和弦。
- **变速不变调**：50%–150%，可以在波形上拖出一段单独设速度，比如难的段落放慢到 70%；也能变调（± 半音和音分）。
- **练习工具**：A-B 循环、速度练习（每循环一遍自动加快一点）、预备拍、跟着歌曲拍子响的节拍器。
- **扒谱**：每轨自动转成 MIDI，网页里有钢琴卷帘，显示音名和"正在响"的音。MIDI 带歌曲的速度变化，导入 Guitar Pro / MuseScore 时小节能对上。贝斯最准，吉他、钢琴是比较密的参考草稿。
- **快捷键**：空格 播放/暂停，L 循环开关，`[` `]` 调速，← → 前后一小节，M 节拍器。

用法（以 RoFormer 分轨为例，`songs/` 里的每首歌都会处理）：

```bash
.venv/bin/python experiments/evaluate_multi.py roformer_sw --full --no-score
.venv/bin/python experiments/analyze.py outputs/roformer_sw_full
.venv/bin/python experiments/transcribe.py outputs/roformer_sw_full
.venv/bin/python experiments/build_player.py outputs/roformer_sw_full
.venv/bin/python -m http.server 8765
```

然后浏览器打开 http://localhost:8765/outputs/roformer_sw_full/player.html （英文版 player.en.html）。

- 必须这样用本地服务器打开：直接双击 html 文件，浏览器不允许网页处理音频。
- 第一条命令是分轨，`--no-score` 表示跳过 MUSDB18 打分，所以不用先下载测试数据；第二条识别速度、调式、和弦；第三条转 MIDI；第四条生成播放页。
- 识别结果是参考：速度在噪音段、变速段可能乱；调式和和弦的准确率大约七八成，和弦的扩展音（7、sus 等）只是提示。
- 一首 6 分多钟的 6 轨歌，浏览器里大约占 1.5 GB 内存。

## 当前结果

分数是 MUSDB18 公开样例里 50 段 7 秒测试片段的中位数 SDR（dB，越高越好；带真实分轨答案）。

**3 轨（人声 / 鼓 / 其他含贝斯）**

| 方法 | 类型 | 人声 | 鼓 | 其他 | 平均 |
|---|---|---|---|---|---|
| 理论上限：理想频谱遮罩（偷看答案） | 参考 | 10.67 | 9.31 | 10.08 | 10.02 |
| Demucs htdemucs | AI | 8.83 | 9.61 | 9.23 | 9.23 |
| 多专家融合（5 种方法按频段加权投票，用练习片段学权重） | 信号处理 | 5.32 | 4.30 | 4.79 | 4.80 |
| HPSS 两级中值滤波 | 信号处理 | 4.51 | 4.01 | 4.63 | 4.38 |
| NMF 矩阵分解（用练习片段学模板） | 信号处理 | 4.18 | 4.16 | 3.88 | 4.07 |
| REPET 重复结构 | 信号处理 | 4.26 | 2.72 | 3.26 | 3.41 |
| 音高追踪 | 信号处理 | 4.10 | 2.44 | 3.65 | 3.40 |
| 纯频率切分（用练习片段学 EQ） | 信号处理 | 3.50 | 2.23 | 2.31 | 2.68 |
| 立体声位置（用练习片段学增益） | 信号处理 | 3.48 | 2.17 | 2.20 | 2.62 |
| 什么都不做（原曲当答案） | 底线 | -4.50 | -4.13 | -1.24 | -3.29 |

**4 轨打分（人声 / 鼓 / 贝斯 / 其他\*）**

| 模型 | 人声 | 鼓 | 贝斯 | 其他\* | 平均 |
|---|---|---|---|---|---|
| BS-RoFormer-SW（6 轨） | 11.97 | 11.86 | 9.45 | 8.00 | 10.32 |
| Demucs htdemucs（4 轨） | 8.83 | 9.61 | 9.14 | 5.27 | 8.21 |
| Demucs htdemucs_6s（6 轨） | 8.64 | 9.49 | 8.31 | 4.82 | 7.82 |

\* MUSDB18 没有单独的吉他、钢琴答案，6 轨模型的 吉他 + 钢琴 + 其他 合起来按"其他"打分；吉他好坏只能靠听。

## 与 Ultimate Vocal Remover（UVR）的比较

[UVR](https://github.com/Anjok07/ultimatevocalremovergui) 是最流行的开源分轨软件。下面只比较两者的功能定位（截至 2026 年 10 月）：

| | UVR | OpenMusic |
|---|---|---|
| 定位 | 分轨工作站：用多种模型产出高质量的分轨文件，供混音、翻唱、制作使用 | 练习工具：分完直接在本地网页里静音、独奏、对比着听，供乐手练琴使用 |
| 形式 | 桌面应用，提供 Windows / macOS / Linux 安装包 | Python 脚本 + 本地网页 |
| 模型 | 多种架构（VR、MDX-Net、MDX23C、Demucs 等），除 Demucs 外大多由 UVR 开发者训练 | Demucs、BS-RoFormer-SW，另有 6 种传统信号处理方法用作对比 |
| 多模型合并 | 有（Ensemble） | 传统方法有融合（`combo`），AI 模型暂时没有 |
| 质量评估 | 靠用户自己试听 | 内置打分（MUSDB18 测试片段上的 SDR）和中英文对比试听页 |
| 变速、变调 | 提供基于 Rubber Band 的处理工具，处理后输出文件 | 播放时实时调整，不同段落可以设不同速度 |
| 吉他 | 可用 Demucs 6 轨分出一条吉他轨，没有区分多把吉他的模型 | 同样是一条吉他轨（Demucs 6 轨或 RoFormer-SW） |
| 练琴、扒谱辅助 | 没有 | 每轨音量可放大、A-B 循环、节拍器；自动识别速度、调式、和弦；每轨转 MIDI 并显示钢琴卷帘 |
| 输出 | 分轨文件 | 成品默认 MP3，对比用无损 FLAC |
| 协议 | 代码 MIT（使用其模型需注明 UVR） | AGPL-3.0 |

两者是互补的：想要尽可能多的模型、尽可能好的分轨文件，用 UVR；想分完直接练琴、扒谱，并客观比较不同方法，用 OpenMusic。

## 目录

```
experiments/
  common.py           路径、读写音频、SDR 打分
  prepare_data.py     把 MUSDB18 样例解成 3 轨标准答案（data/gt/）
  prepare_bass.py     补上贝斯标准答案
  make_excerpts.py    用 Demucs 分整首歌，并截一段 60 秒试听片段
  evaluate.py         给 3 轨方法打分：python experiments/evaluate.py <方法> --split test --song
  evaluate_multi.py   给多轨模型打分：python experiments/evaluate_multi.py <模型> --split test --song --full
  verify_scores.py    从头重新打分，和已存的结果对比
  export_mp3.py       把已有的无损分轨（WAV/FLAC）转成 MP3，保持文件夹结构
  oracle.py           理论上限（理想频谱遮罩）
  build_page.py       生成对比试听页 compare.html（中文）和 compare.en.html（英文）
  analyze.py          识别速度（拍子、小节）、调式、和弦，写 analysis.json
  transcribe.py       每轨转 MIDI 和音符列表（transcription/）
  build_player.py     生成练习播放器 player.html（中文）和 player.en.html（英文）
  tests/              分析、转谱、播放器的自动测试
  vendor/             直接收录的第三方代码（各自带 LICENSE）：Signalsmith Stretch、wavesurfer.js、S-KEY
  methods/            3 轨方法：每个文件一个 separate(mix, sr)
  methods_multi/      多轨 AI 模型：demucs4、demucs6、roformer_sw
results/              每个方法的分数（JSON）和实验说明 method_info.json
```

不进仓库的（见 `.gitignore`）：`.venv/`、`.cache/`（模型权重）、`data/`（数据集）、`songs/`（你的歌）、`outputs/`（分好的音轨）。

## 在新电脑上跑起来

需要 Python 3.10 以上（测试用的是 3.12）。

**1. 建环境**

macOS / Linux：
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --no-deps -r requirements-nodeps.txt
```

Windows + NVIDIA 显卡（例如 4070 Ti）：先按 https://pytorch.org/get-started/locally/ 选 CUDA 版本，
用它给出的命令把带 CUDA 的 torch 装进 `.venv`，再装其余依赖：
```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install --no-deps -r requirements-nodeps.txt
```
装完检查 GPU：`.venv\Scripts\python -c "import torch; print(torch.cuda.is_available())"` 应该打印 `True`。
代码会自动按 CUDA → Apple MPS → CPU 的顺序选设备。

第二行单独装转 MIDI 用的 basic-pitch：它的依赖声明要求的 TensorFlow 版本在 Python 3.12 上装不了，而我们用的 ONNX 方式根本不需要 TensorFlow，所以跳过依赖安装。之后 `pip check` 会报 3 条不匹配，属于正常。
只有想跑播放器的自动测试时才需要 `requirements-dev.txt`（Playwright，安装说明写在文件里）。

下面的命令都以 macOS 写法为例；Windows 把 `.venv/bin/python` 换成 `.venv\Scripts\python`。

**2. 准备打分数据（约 150 MB 下载，解开后约 0.5 GB）**

```bash
curl -L -o data/MUSDB18-7-STEMS.zip https://github.com/sigsep/sigsep-mus-db/releases/download/v0.4.0/MUSDB18-7-STEMS.zip
```
解压到 `data/musdb7/`（里面应有 `train/`、`test/` 两个文件夹），然后：
```bash
.venv/bin/python experiments/prepare_data.py
.venv/bin/python experiments/prepare_bass.py
```

**3. 放歌、跑模型**

把 mp3 放进 `songs/`，然后：
```bash
.venv/bin/python experiments/make_excerpts.py
.venv/bin/python experiments/evaluate_multi.py roformer_sw --split test --song --full
.venv/bin/python experiments/build_page.py
```
模型权重第一次运行时会自动下载到 `.cache/`（Demucs 约 80 MB，RoFormer-SW 约 700 MB）。

**输出格式：**
- 整首歌的分轨（`--full`）是拿来练习的成品，默认存成 **MP3**（VBR 约 V2，大小约为无损的 1/3，听感无差别）。加 `--lossless` 存成无损 FLAC。
- 60 秒对比片段（`--song`）是用来对比方法的，**一律是无损 FLAC**，因为压缩痕迹会盖住方法之间的差别。
- 已有的无损分轨想转 MP3：`.venv/bin/python experiments/export_mp3.py <无损分轨文件夹> <输出文件夹>`。
- 所有格式的分轨长度都精确到采样点，几轨一起播放时保持同步。

**4. 试听**

```bash
.venv/bin/python -m http.server 8765
```
浏览器打开 http://localhost:8765/compare.html （英文版：compare.en.html）。
如果分轨结果是从另一台电脑拷过来的文件夹（里面有 `data/song_excerpts/` 和 `outputs/`，WAV 或 FLAC 都行），
可以用 `.venv/bin/python experiments/build_page.py --root <那个文件夹>` 直接在里面生成两个试听页。

## 许可和注意事项

- Copyright (C) 2026 Lihan Zhong。代码采用 GNU Affero 通用公共许可证 v3.0（AGPL-3.0，见 [LICENSE](LICENSE)）。任何人都可以使用、修改、再发布，但衍生项目也必须以 AGPL-3.0 开源。把修改后的版本做成网站等网络服务提供给别人用，也要向用户公开源代码。
- MUSDB18 数据只能用于非商业的教育和学术用途，所以不放进仓库。`*_stats.npz` 和 `experiments/methods/stereo.py` 里的增益表，是从它的训练集统计出来的少量参数，适用 MUSDB18 的条款，不属于 AGPL 范围。
- Demucs 的模型权重不在 MIT 协议范围内，作者说明“仅供科学研究用途”（facebookresearch/demucs#327）。BS-RoFormer-SW 是社区流传的权重，训练者和协议都不明。个人练习可以，不要拿去商用。
- 不要把别人的歌或分好的音轨提交进仓库。
- `experiments/vendor/` 里收录的第三方代码保留各自的协议（MIT / BSD-3-Clause），见各目录的 LICENSE。S-KEY 只能从仓库里收录的这份用，**不要** `pip install skey`，那是一个不相干的同名包。

## 致谢

这个项目建立在很多开源项目和研究成果之上，在此致谢。

**分离模型和代码**

- [Demucs](https://github.com/adefossez/demucs)：Alexandre Défossez / Meta AI，代码 MIT 协议（原仓库 [facebookresearch/demucs](https://github.com/facebookresearch/demucs) 已归档）。用到的 HTDemucs 模型出自 Rouard, Massa & Défossez, “Hybrid Transformers for Music Source Separation”, ICASSP 2023（[doi](https://doi.org/10.1109/ICASSP49357.2023.10096956)）；6 轨模型 htdemucs_6s 见 Demucs 的 README。
- [BS-RoFormer](https://github.com/lucidrains/BS-RoFormer)：Phil Wang (lucidrains) 的 PyTorch 实现，MIT 协议。架构出自 Lu, Wang, Kong & Hung, “Music Source Separation with Band-Split RoPE Transformer”, ICASSP 2024（[doi](https://doi.org/10.1109/ICASSP48485.2024.10446843)）。
- [bs-roformer-infer](https://github.com/openmirlab/bs-roformer-infer)：OpenMIRLab，MIT 协议；它改编自 lucidrains/BS-RoFormer 和 [python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator)（Andrew Beveridge，MIT 协议）。
- BS-RoFormer SW 6 轨权重：最早由 jarredou 以 “BS-ROFO-SW-Fixed” 上传到 Hugging Face（原账号已删除），我们下载的是 [enerjazzer/BS-ROFO-SW-Fixed](https://huggingface.co/enerjazzer/BS-ROFO-SW-Fixed) 的镜像。训练者和协议均不明。它沿用 [Music-Source-Separation-Training](https://github.com/ZFTurbo/Music-Source-Separation-Training)（ZFTurbo，MIT 协议）的配置格式。

**数据和评测**

- MUSDB18：Rafii, Liutkus, Stöter, Mimilakis & Bittner, “The MUSDB18 corpus for music separation”, 2017（[doi:10.5281/zenodo.1117372](https://doi.org/10.5281/zenodo.1117372)）。7 秒样例来自 [sigsep/sigsep-mus-db](https://github.com/sigsep/sigsep-mus-db)。
- 打分用的全局 SDR 参照 Music Demixing Challenge：Mitsufuji et al., “Music Demixing Challenge 2021”, Frontiers in Signal Processing, 2022（[doi](https://doi.org/10.3389/frsip.2021.808395)）。我们的实现在细节上略有不同。

**传统信号处理方法参考的论文**

- 打击乐/和声分离：D. FitzGerald, “Harmonic/Percussive Separation using Median Filtering”, DAFx-10, 2010（[链接](https://dafx.de/paper-archive/details/DsmIVcydPX66AuaqEmKyTQ)）；D. FitzGerald & M. Gainza, “Single Channel Vocal Separation using Median Filtering and Factorisation Techniques”, ISAST Trans. on Electronic and Signal Processing 4(1), 2010（[链接](https://arrow.tudublin.ie/argart/8)）；J. Driedger, M. Müller & S. Disch, “Extending Harmonic-Percussive Separation of Audio Signals”, ISMIR 2014（[doi](https://doi.org/10.5281/zenodo.1415226)）。
- 重复结构：Z. Rafii & B. Pardo, “REpeating Pattern Extraction Technique (REPET): A Simple Method for Music/Voice Separation”, IEEE TASLP 21(1), 2013（[doi](https://doi.org/10.1109/TASL.2012.2213249)）；Z. Rafii & B. Pardo, “Music/Voice Separation using the Similarity Matrix”, ISMIR 2012（[doi](https://doi.org/10.5281/zenodo.1417631)）；D. FitzGerald, “Vocal Separation using Nearest Neighbours and Median Filtering”, ISSC 2012（[doi](https://doi.org/10.1049/ic.2012.0225)）。
- 旋律引导：J.-L. Durrieu, G. Richard, B. David & C. Févotte, “Source/Filter Model for Unsupervised Main Melody Extraction From Polyphonic Audio Signals”, IEEE TASLP 18(3), 2010（[doi](https://doi.org/10.1109/TASL.2010.2041114)）。
- 矩阵分解（NMF）：D. D. Lee & H. S. Seung, “Learning the parts of objects by non-negative matrix factorization”, Nature 401, 1999（[doi](https://doi.org/10.1038/44565)）；D. D. Lee & H. S. Seung, “Algorithms for Non-negative Matrix Factorization”, NIPS 13, 2000（[链接](https://papers.nips.cc/paper_files/paper/2000/hash/f9d1152547c0bde01830b7e8bd60024c-Abstract.html)）。
- 立体声位置：C. Avendano, “Frequency-domain source identification and manipulation in stereo mixes for enhancement, suppression and re-panning applications”, IEEE WASPAA 2003（[doi](https://doi.org/10.1109/ASPAA.2003.1285818)）；D. Barry, B. Lawlor & E. Coyle, “Sound Source Separation: Azimuth Discrimination and Resynthesis”（ADRess），DAFx-04, 2004（[链接](https://dafx.de/paper-archive/details/jK8kPFnlWXDwy1Kumk3kkQ)）。

**练习播放器与音乐分析**

- [Signalsmith Stretch](https://github.com/Signalsmith-Audio/signalsmith-stretch)（Geraint Luff / Signalsmith Audio，MIT）：变速不变调、变调的音频引擎。收录的是 1.3.2 版网页构建，打了两行补丁，见 `experiments/vendor/signalsmith-stretch/README.md`。
- [wavesurfer.js](https://github.com/katspaugh/wavesurfer.js)（BSD-3-Clause）7.12.12：波形、时间轴和区域选择。
- 设计参考：[Woodshed](https://github.com/KyleKincer/woodshed)（MIT）的音频引擎设计，[BandBuddy](https://github.com/dourgey/BandBuddy)（Apache-2.0）的限幅器参数，[Steminize](https://github.com/Gerschwin/steminize)（MIT）的循环加速练习，[StemDeck](https://github.com/stemdeckapp/stemdeck)（Apache-2.0）用分轨识别调式和弦时的权重和 Albrecht–Shanahan 调式表。
- [Beat This!](https://github.com/CPJKU/beat_this)（CPJKU，ISMIR 2024；代码和权重都是 MIT）：识别拍子和小节。
- [S-KEY](https://github.com/deezer/skey)（Deezer，ICASSP 2025，MIT）：调式识别模型，收录在 `experiments/vendor/skey/`；依赖 [nnAudio](https://github.com/KinWaiCheuk/nnAudio)（MIT）。
- 调式模板数据取自 [Essentia](https://github.com/MTG/essentia) 的 key.cpp（AGPL-3.0）；和弦识别的模板 + Viterbi 做法参照 librosa 文档里的示例。
- [Basic Pitch](https://github.com/spotify/basic-pitch)（Spotify，ICASSP 2022，Apache-2.0）：转 MIDI，用 [ONNX Runtime](https://github.com/microsoft/onnxruntime)（MIT）运行；MIDI 读写用 [pretty_midi](https://github.com/craffel/pretty-midi)（MIT）和 [mido](https://github.com/mido/mido)（MIT），评测用 [mir_eval](https://github.com/mir-evaluation/mir_eval)（MIT），重采样用 [resampy](https://github.com/bmcfee/resampy)（ISC）。
- [Playwright](https://github.com/microsoft/playwright-python)（Apache-2.0）：只用于播放器的自动测试。

**Python 库**

[PyTorch](https://github.com/pytorch/pytorch)（BSD-3-Clause）、[NumPy](https://github.com/numpy/numpy)（BSD-3-Clause）、[SciPy](https://github.com/scipy/scipy)（BSD-3-Clause）、[librosa](https://github.com/librosa/librosa)（ISC；McFee et al., SciPy 2015，[doi](https://doi.org/10.25080/Majora-7b98e3ed-003)）、[python-soundfile](https://github.com/bastibe/python-soundfile)（BSD-3-Clause）及其自带的 [libsndfile](https://github.com/libsndfile/libsndfile)（LGPL-2.1+，MP3 编解码用 LAME 和 mpg123）、[PyAV](https://github.com/PyAV-Org/PyAV)（BSD-3-Clause）及 [FFmpeg](https://ffmpeg.org)（LGPL-2.1+）、[scikit-learn](https://github.com/scikit-learn/scikit-learn)（BSD-3-Clause）、[Matplotlib](https://github.com/matplotlib/matplotlib)（Matplotlib License）、[ML Collections](https://github.com/google/ml_collections)（Apache-2.0）、[PyYAML](https://github.com/yaml/pyyaml)（MIT）。
