# OpenMusic

[中文](README.md) | **English**

> This project's default language is Chinese (the listening page and other UI are in Chinese).
> This English README is provided as an alternative; the [Chinese README](README.md) is the main one.

Experiments toward a small open-source practice tool for musicians: split a song into vocals / drums /
bass / guitar / other, then mute, solo and compare the stems in a local web page. The goal is a small
Python program that runs on your own computer. There is no mobile version.

This repo is at the **experiment stage**. It compares classical signal-processing methods with AI models
(Demucs, BS-RoFormer) at stem separation.

## Current results

Scores are median SDR (dB, higher is better) over 50 seven-second test clips from the public MUSDB18
sample, which comes with the true separated stems.

**3 stems (vocals / drums / other, bass included in other)**

| Method | Type | Vocals | Drums | Other | Mean |
|---|---|---|---|---|---|
| Ceiling: ideal ratio mask (reads the true stems) | Reference | 10.67 | 9.31 | 10.08 | 10.02 |
| Demucs htdemucs | AI | 8.83 | 9.61 | 9.23 | 9.23 |
| Multi-expert fusion (5 methods, per-band weights learned on the train clips) | DSP | 5.32 | 4.30 | 4.79 | 4.80 |
| HPSS, two-stage median filtering | DSP | 4.51 | 4.01 | 4.63 | 4.38 |
| NMF (templates learned on the train clips) | DSP | 4.18 | 4.16 | 3.88 | 4.07 |
| REPET, repetition-based | DSP | 4.26 | 2.72 | 3.26 | 3.41 |
| Pitch / melody tracking | DSP | 4.10 | 2.44 | 3.65 | 3.40 |
| Frequency bands only (EQ learned on the train clips) | DSP | 3.50 | 2.23 | 2.31 | 2.68 |
| Stereo position (gains learned on the train clips) | DSP | 3.48 | 2.17 | 2.20 | 2.62 |
| Do nothing (the mixture as every stem) | Floor | -4.50 | -4.13 | -1.24 | -3.29 |

**4-stem scores (vocals / drums / bass / other\*)**

| Model | Vocals | Drums | Bass | Other\* | Mean |
|---|---|---|---|---|---|
| BS-RoFormer-SW (6 stems) | 11.97 | 11.86 | 9.45 | 8.00 | 10.32 |
| Demucs htdemucs (4 stems) | 8.83 | 9.61 | 9.14 | 5.27 | 8.21 |
| Demucs htdemucs_6s (6 stems) | 8.64 | 9.49 | 8.31 | 4.82 | 7.82 |

\* MUSDB18 has no separate guitar or piano stems, so for the 6-stem models guitar + piano + other are
scored together as "other". Guitar quality can only be judged by ear.

## Layout

```
experiments/
  common.py           paths, audio I/O, SDR scoring
  prepare_data.py     decode the MUSDB18 sample into 3-stem ground truth (data/gt/)
  prepare_bass.py     add bass ground truth
  make_excerpts.py    split each full song with Demucs and cut a 60 s listening excerpt
  evaluate.py         score a 3-stem method: python experiments/evaluate.py <method> --split test --song
  evaluate_multi.py   score a multi-stem model: python experiments/evaluate_multi.py <model> --split test --song --full
  verify_scores.py    re-score methods from scratch and compare with the stored results
  oracle.py           the ceiling (ideal ratio mask)
  build_page.py       build the listening pages compare.html (Chinese) and compare.en.html (English)
  methods/            3-stem methods, one separate(mix, sr) per file
  methods_multi/      multi-stem AI models: demucs4, demucs6, roformer_sw
results/              each method's scores (JSON) and experiment notes in method_info.json
```

Not in the repo (see `.gitignore`): `.venv/`, `.cache/` (model weights), `data/` (dataset),
`songs/` (your music), `outputs/` (separated stems).

## Running it on a new computer

Python 3.10 or newer (tested with 3.12).

**1. Environment**

macOS / Linux:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Windows with an NVIDIA GPU (e.g. RTX 4070 Ti): first pick your CUDA version at
https://pytorch.org/get-started/locally/ and use the command it gives to install a CUDA build of torch
into `.venv`, then install the rest:
```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```
Check the GPU afterwards: `.venv\Scripts\python -c "import torch; print(torch.cuda.is_available())"`
should print `True`. The code picks a device in the order CUDA → Apple MPS → CPU.

The commands below use macOS paths; on Windows replace `.venv/bin/python` with `.venv\Scripts\python`.

**2. Scoring data (about 150 MB to download, about 0.5 GB unpacked)**

```bash
curl -L -o data/MUSDB18-7-STEMS.zip https://github.com/sigsep/sigsep-mus-db/releases/download/v0.4.0/MUSDB18-7-STEMS.zip
```
Unzip it into `data/musdb7/` (it should contain `train/` and `test/`), then:
```bash
.venv/bin/python experiments/prepare_data.py
.venv/bin/python experiments/prepare_bass.py
```

**3. Add songs and run the models**

Put your mp3 files in `songs/`, then:
```bash
.venv/bin/python experiments/make_excerpts.py
.venv/bin/python experiments/evaluate_multi.py roformer_sw --split test --song --full
.venv/bin/python experiments/build_page.py
```
Model weights download to `.cache/` on first use (Demucs about 80 MB, RoFormer-SW about 700 MB).

**4. Listen**

```bash
.venv/bin/python -m http.server 8765
```
Open http://localhost:8765/compare.html in a browser (English: compare.en.html).
If the separated stems were copied over from another computer as a folder (with `data/song_excerpts/` and
`outputs/`, WAV or FLAC), `.venv/bin/python experiments/build_page.py --root <that folder>` builds both
pages inside it.

## Licences and caveats

- Copyright (C) 2026 Lihan Zhong. The code is licensed under the GNU Affero General Public License
  v3.0 (AGPL-3.0, see [LICENSE](LICENSE)). You may use, modify and redistribute it, but any derivative
  work must also be released as open source under AGPL-3.0. That includes running a modified version
  as a network service, e.g. a website: its users must be able to get the source code.
- MUSDB18 is for non-commercial research only, so it is not in the repo. The `*_stats.npz` files and
  the gain table in `experiments/methods/stereo.py` are a small set of parameters derived from its
  train split; they fall under MUSDB18's terms, not the AGPL.
- The Demucs authors say its model weights are for research use only (facebookresearch/demucs#327).
  BS-RoFormer-SW is a community re-hosted checkpoint with no stated licence. Fine for personal practice;
  don't use them commercially.
- Don't commit other people's songs or separated stems to the repo.
