# OpenMusic

一个给乐手用的开源"分轨练习"小工具的实验仓库：把一首歌拆成人声 / 鼓 / 贝斯 / 吉他 / 其他，
再在本地网页里静音、独奏、对比着听。目标是 Python 跑在自己电脑上的小软件，不做手机版。

目前这里是**实验阶段**：比较传统信号处理方法和 AI 模型（Demucs、BS-RoFormer）分轨的效果。

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

## 目录

```
experiments/
  common.py           路径、读写音频、SDR 打分
  prepare_data.py     把 MUSDB18 样例解成 3 轨标准答案（data/gt/）
  prepare_bass.py     补上贝斯标准答案
  make_excerpts.py    用 Demucs 分整首歌，并截一段 60 秒试听片段
  evaluate.py         给 3 轨方法打分：python experiments/evaluate.py <方法> --split test --song
  evaluate_multi.py   给多轨模型打分：python experiments/evaluate_multi.py <模型> --split test --song --full
  oracle.py           理论上限（理想频谱遮罩）
  build_page.py       生成对比试听页 compare.html
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
```

Windows + NVIDIA 显卡（例如 4070 Ti）：先按 https://pytorch.org/get-started/locally/ 选 CUDA 版本，
用它给出的命令把带 CUDA 的 torch 装进 `.venv`，再装其余依赖：
```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```
装完检查 GPU：`.venv\Scripts\python -c "import torch; print(torch.cuda.is_available())"` 应该打印 `True`。
代码会自动按 CUDA → Apple MPS → CPU 的顺序选设备。

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

**4. 试听**

```bash
.venv/bin/python -m http.server 8765
```
浏览器打开 http://localhost:8765/compare.html 。

## 许可和注意事项

- 本仓库代码尚未选定开源协议。
- MUSDB18 数据只能用于非商业研究，所以不放进仓库。`*_stats.npz` 是从它的训练集统计出来的少量参数。
- Demucs 的模型权重，作者说明只供科研用途（facebookresearch/demucs#327）。BS-RoFormer-SW 是社区重新托管的权重，没有写明协议。个人练习可以，不要拿去商用。
- 不要把别人的歌或分好的音轨提交进仓库。
