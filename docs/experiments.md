# 分轨方法对比实验

**中文** | [English](experiments.en.md) · [返回 README](../README.md)

做练习播放器之前，我们先比较了传统信号处理方法和 AI 模型能把歌分得多干净，结论是只用 AI 模型（Demucs、BS-RoFormer-SW）。下面是实验结果和复现步骤。

## 结果

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

## 复现

下面的命令都以 macOS 写法为例；Windows 把 `.venv/bin/python` 换成 `.venv\Scripts\python`。环境安装见 [README](../README.md#安装)。

**1. 准备打分数据（约 150 MB 下载，解开后约 0.5 GB）**

```bash
curl -L -o data/MUSDB18-7-STEMS.zip https://github.com/sigsep/sigsep-mus-db/releases/download/v0.4.0/MUSDB18-7-STEMS.zip
```
解压到 `data/musdb7/`（里面应有 `train/`、`test/` 两个文件夹），然后：
```bash
.venv/bin/python experiments/prepare_data.py
.venv/bin/python experiments/prepare_bass.py
```

**2. 放歌、跑模型（打分 + 60 秒对比片段）**

把 mp3 放进 `songs/`，然后：
```bash
.venv/bin/python experiments/make_excerpts.py
.venv/bin/python experiments/evaluate_multi.py roformer_sw --split test --song --full
.venv/bin/python experiments/build_page.py
```
模型权重第一次运行时会自动下载到 `.cache/`（Demucs 约 80 MB，RoFormer-SW 约 700 MB）。

**3. 对比试听**

```bash
.venv/bin/python -m http.server 8765
```
浏览器打开 http://localhost:8765/compare.html （英文版：compare.en.html）。
如果分轨结果是从另一台电脑拷过来的文件夹（里面有 `data/song_excerpts/` 和 `outputs/`，WAV 或 FLAC 都行），
可以用 `.venv/bin/python experiments/build_page.py --root <那个文件夹>` 直接在里面生成两个试听页。

代码在 `experiments/`：`evaluate.py`（3 轨方法打分）、`evaluate_multi.py`（多轨模型打分）、`oracle.py`（理论上限）、`verify_scores.py`（从头重新打分核对）、`methods/`（6 种传统方法 + 融合）、`methods_multi/`（AI 模型）；分数在 `results/`。
