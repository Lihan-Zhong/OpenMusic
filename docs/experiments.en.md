# Separation method comparison

[中文](experiments.md) | **English** · [Back to README](../README.en.md)

Before building the practice player we measured how cleanly classical signal-processing methods and AI models separate songs; the conclusion was to use only the AI models (Demucs, BS-RoFormer-SW). Here are the results and how to reproduce them.

## Results

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

## Reproducing

The commands below use macOS paths; on Windows replace `.venv/bin/python` with `.venv\Scripts\python`. For the environment see the [README](../README.en.md#installation).

**1. Scoring data (about 150 MB to download, about 0.5 GB unpacked)**

```bash
curl -L -o data/MUSDB18-7-STEMS.zip https://github.com/sigsep/sigsep-mus-db/releases/download/v0.4.0/MUSDB18-7-STEMS.zip
```
Unzip it into `data/musdb7/` (it should contain `train/` and `test/`), then:
```bash
.venv/bin/python experiments/prepare_data.py
.venv/bin/python experiments/prepare_bass.py
```

**2. Add songs and run the models (scores + 60 s comparison excerpts)**

Put your mp3 files in `songs/`, then:
```bash
.venv/bin/python experiments/make_excerpts.py
.venv/bin/python experiments/evaluate_multi.py roformer_sw --split test --song --full
.venv/bin/python experiments/build_page.py
```
Model weights download to `.cache/` on first use (Demucs about 80 MB, RoFormer-SW about 700 MB).

**3. Comparison listening**

```bash
.venv/bin/python -m http.server 8765
```
Open http://localhost:8765/compare.html in a browser (English: compare.en.html).
If the separated stems were copied over from another computer as a folder (with `data/song_excerpts/` and
`outputs/`, WAV or FLAC), `.venv/bin/python experiments/build_page.py --root <that folder>` builds both
pages inside it.

The code is in `experiments/`: `evaluate.py` (scores 3-stem methods), `evaluate_multi.py` (scores multi-stem models), `oracle.py` (the ceiling), `verify_scores.py` (re-scores from scratch to check), `methods/` (six classical methods + fusion), `methods_multi/` (AI models); scores are in `results/`.
