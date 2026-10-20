# OpenMusic

[中文](README.md) | **English**

> This project's default language is Chinese (the listening page and other UI are in Chinese).
> This English README is provided as an alternative; the [Chinese README](README.md) is the main one.

Experiments toward a small open-source practice tool for musicians: split a song into vocals / drums /
bass / guitar / other, then mute, solo and compare the stems in a local web page. The goal is a small
Python program that runs on your own computer. There is no mobile version.

There is now a working **practice player** (next section). The repo also keeps the separation experiments,
which compare classical signal-processing methods with AI models (Demucs, BS-RoFormer).

## Practice player (for practising and transcribing)

Once a song is split into stems, practise with it in a local web page:

- **Per-stem volume** from −∞ to +12 dB, so the bass can be louder than in the original; mute and solo; a
  master limiter so boosting never clips.
- **Tempo, key and chords**: detected BPM, every beat and bar line; the key with a confidence and the runner-up
  (usually the relative major/minor); a chord lane on the timeline showing the current and next chord.
- **Speed change without pitch change**, 50%–150%; drag across the waveform to give a passage its own speed,
  e.g. 70% for the hard part. Pitch shift in semitones and cents.
- **Practice tools**: A-B loop, a speed trainer (each loop a little faster), count-in, and a metronome that
  clicks on the song's own beats.
- **Transcription**: every stem is turned into MIDI; the page shows a piano roll with note names and the notes
  sounding now. The MIDI carries the song's tempo changes, so bars line up when imported into Guitar Pro or
  MuseScore. Bass is the most accurate; guitar and piano are dense rough drafts.
- **Shortcuts**: space play/pause, L loop on/off, `[` `]` speed, ← → one bar back/forward, M metronome.

Usage (with RoFormer stems; every song in `songs/` is processed):

```bash
.venv/bin/python experiments/evaluate_multi.py roformer_sw --full --no-score
.venv/bin/python experiments/analyze.py outputs/roformer_sw_full
.venv/bin/python experiments/transcribe.py outputs/roformer_sw_full
.venv/bin/python experiments/build_player.py outputs/roformer_sw_full
.venv/bin/python -m http.server 8765
```

Then open http://localhost:8765/outputs/roformer_sw_full/player.html (English: player.en.html).

- It must be served like this: opened by double-clicking the HTML file, the browser won't let the page process audio.
- The first command splits the songs (`--no-score` skips MUSDB18 scoring, so no test data is needed); the second
  detects tempo, key and chords; the third makes MIDI; the fourth builds the pages.
- Detection is a guide: tempo can go wrong in noise or rubato passages; key and chords are right roughly 70–80%
  of the time, and chord extensions (7, sus, ...) are hints.
- A 6-minute song with 6 stems uses about 1.5 GB of browser memory.

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

## Comparison with Ultimate Vocal Remover (UVR)

[UVR](https://github.com/Anjok07/ultimatevocalremovergui) is the most popular open-source stem separation app. This compares only what each project is for (as of October 2026):

| | UVR | OpenMusic |
|---|---|---|
| Purpose | A separation workstation: produce high-quality stem files with many models, for mixing, covers and production | A practice tool: split a song, then mute, solo and compare the stems in a local web page, for musicians practising |
| Form | Desktop app with installers for Windows / macOS / Linux | Python scripts + a local web page |
| Models | Many architectures (VR, MDX-Net, MDX23C, Demucs and more), mostly trained by the UVR developers apart from Demucs | Demucs and BS-RoFormer-SW, plus six classical signal-processing methods for comparison |
| Combining models | Yes (Ensemble) | Fusion of the classical methods (`combo`); none yet for the AI models |
| Quality evaluation | Listening by the user | Built-in scoring (SDR on MUSDB18 test clips) and Chinese/English listening pages |
| Speed / pitch change | Tools based on Rubber Band that process and write new files | Adjusted in real time during playback, with different speeds for different passages |
| Guitar | One guitar stem via Demucs 6-stem; no model that splits several guitars | Likewise one guitar stem (Demucs 6-stem or RoFormer-SW) |
| Practice and transcription aids | None | Per-stem boost, A-B loop, metronome; detected tempo, key and chords; per-stem MIDI with a piano roll |
| Output | Stem files | MP3 by default for finished stems, lossless FLAC for comparison |
| Licence | MIT code (credit UVR when using its models) | AGPL-3.0 |

The two complement each other: for the most models and the best possible stem files, use UVR; to practise and transcribe right after splitting, and compare methods objectively, use OpenMusic.

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
  export_mp3.py       convert existing lossless stems (WAV/FLAC) to MP3, keeping the folder layout
  oracle.py           the ceiling (ideal ratio mask)
  build_page.py       build the listening pages compare.html (Chinese) and compare.en.html (English)
  analyze.py          detect tempo (beats, bars), key and chords; writes analysis.json
  transcribe.py       turn each stem into MIDI and a note list (transcription/)
  build_player.py     build the practice player player.html (Chinese) and player.en.html (English)
  tests/              automated tests for analysis, transcription and the player
  vendor/             third-party code included as is, each with its LICENSE: Signalsmith Stretch, wavesurfer.js, S-KEY
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
.venv/bin/pip install --no-deps -r requirements-nodeps.txt
```

Windows with an NVIDIA GPU (e.g. RTX 4070 Ti): first pick your CUDA version at
https://pytorch.org/get-started/locally/ and use the command it gives to install a CUDA build of torch
into `.venv`, then install the rest:
```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install --no-deps -r requirements-nodeps.txt
```
Check the GPU afterwards: `.venv\Scripts\python -c "import torch; print(torch.cuda.is_available())"`
should print `True`. The code picks a device in the order CUDA → Apple MPS → CPU.

The second install line adds basic-pitch (MIDI transcription) without its dependencies: it pins TensorFlow
versions that don't exist for Python 3.12, and the ONNX path we use needs no TensorFlow at all. `pip check`
then lists 3 mismatches; that is expected. `requirements-dev.txt` (Playwright) is only needed for the player's
automated tests; install notes are in the file.

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

**Output formats:**
- Full-song stems (`--full`) are the finished product for practice and default to **MP3** (VBR about V2, roughly 1/3 the size of lossless, no audible difference). Add `--lossless` to get FLAC.
- The 60 s comparison excerpts (`--song`) are **always lossless FLAC**, because compression artifacts would blur the differences between methods.
- To convert existing lossless stems to MP3: `.venv/bin/python experiments/export_mp3.py <lossless stems folder> <output folder>`.
- Stems in every format are sample-exact in length, so they stay in sync when played together.

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
- MUSDB18 is for non-commercial educational and academic use only, so it is not in the repo. The `*_stats.npz` files and
  the gain table in `experiments/methods/stereo.py` are a small set of parameters derived from its
  train split; they fall under MUSDB18's terms, not the AGPL.
- The Demucs model weights are not covered by its MIT licence; the author says they are "provided only
  for scientific purposes" (facebookresearch/demucs#327). BS-RoFormer-SW is a community-shared checkpoint
  whose trainer and licence are unknown. Fine for personal practice;
  don't use them commercially.
- Don't commit other people's songs or separated stems to the repo.
- Third-party code in `experiments/vendor/` keeps its own licence (MIT / BSD-3-Clause), see the LICENSE in each
  folder. Use S-KEY only from the copy in the repo; **don't** `pip install skey`, which is an unrelated package.

## Acknowledgements

This project builds on many open-source projects and research results. Thank you to all of them.

**Separation models and code**

- [Demucs](https://github.com/adefossez/demucs) by Alexandre Défossez / Meta AI, MIT-licensed code (the original repo, [facebookresearch/demucs](https://github.com/facebookresearch/demucs), is archived). The HTDemucs model is from Rouard, Massa & Défossez, “Hybrid Transformers for Music Source Separation”, ICASSP 2023 ([doi](https://doi.org/10.1109/ICASSP49357.2023.10096956)); the 6-stem htdemucs_6s model is described in the Demucs README.
- [BS-RoFormer](https://github.com/lucidrains/BS-RoFormer), the PyTorch implementation by Phil Wang (lucidrains), MIT. The architecture is from Lu, Wang, Kong & Hung, “Music Source Separation with Band-Split RoPE Transformer”, ICASSP 2024 ([doi](https://doi.org/10.1109/ICASSP48485.2024.10446843)).
- [bs-roformer-infer](https://github.com/openmirlab/bs-roformer-infer) by OpenMIRLab, MIT, which adapts code from lucidrains/BS-RoFormer and [python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator) (Andrew Beveridge, MIT).
- The BS-RoFormer SW 6-stem checkpoint: first uploaded to Hugging Face by jarredou as “BS-ROFO-SW-Fixed” (that account has since been deleted); we download the mirror at [enerjazzer/BS-ROFO-SW-Fixed](https://huggingface.co/enerjazzer/BS-ROFO-SW-Fixed). Its trainer and licence are unknown. It uses the config format of [Music-Source-Separation-Training](https://github.com/ZFTurbo/Music-Source-Separation-Training) (ZFTurbo, MIT).

**Data and evaluation**

- MUSDB18: Rafii, Liutkus, Stöter, Mimilakis & Bittner, “The MUSDB18 corpus for music separation”, 2017 ([doi:10.5281/zenodo.1117372](https://doi.org/10.5281/zenodo.1117372)). The 7-second sample comes from [sigsep/sigsep-mus-db](https://github.com/sigsep/sigsep-mus-db).
- Our global SDR score follows the Music Demixing Challenge: Mitsufuji et al., “Music Demixing Challenge 2021”, Frontiers in Signal Processing, 2022 ([doi](https://doi.org/10.3389/frsip.2021.808395)). Our implementation differs in small details.

**Papers behind the classical signal-processing methods**

- Harmonic/percussive separation: D. FitzGerald, “Harmonic/Percussive Separation using Median Filtering”, DAFx-10, 2010 ([link](https://dafx.de/paper-archive/details/DsmIVcydPX66AuaqEmKyTQ)); D. FitzGerald & M. Gainza, “Single Channel Vocal Separation using Median Filtering and Factorisation Techniques”, ISAST Trans. on Electronic and Signal Processing 4(1), 2010 ([link](https://arrow.tudublin.ie/argart/8)); J. Driedger, M. Müller & S. Disch, “Extending Harmonic-Percussive Separation of Audio Signals”, ISMIR 2014 ([doi](https://doi.org/10.5281/zenodo.1415226)).
- Repetition: Z. Rafii & B. Pardo, “REpeating Pattern Extraction Technique (REPET): A Simple Method for Music/Voice Separation”, IEEE TASLP 21(1), 2013 ([doi](https://doi.org/10.1109/TASL.2012.2213249)); Z. Rafii & B. Pardo, “Music/Voice Separation using the Similarity Matrix”, ISMIR 2012 ([doi](https://doi.org/10.5281/zenodo.1417631)); D. FitzGerald, “Vocal Separation using Nearest Neighbours and Median Filtering”, ISSC 2012 ([doi](https://doi.org/10.1049/ic.2012.0225)).
- Melody-guided: J.-L. Durrieu, G. Richard, B. David & C. Févotte, “Source/Filter Model for Unsupervised Main Melody Extraction From Polyphonic Audio Signals”, IEEE TASLP 18(3), 2010 ([doi](https://doi.org/10.1109/TASL.2010.2041114)).
- Matrix factorization (NMF): D. D. Lee & H. S. Seung, “Learning the parts of objects by non-negative matrix factorization”, Nature 401, 1999 ([doi](https://doi.org/10.1038/44565)); D. D. Lee & H. S. Seung, “Algorithms for Non-negative Matrix Factorization”, NIPS 13, 2000 ([link](https://papers.nips.cc/paper_files/paper/2000/hash/f9d1152547c0bde01830b7e8bd60024c-Abstract.html)).
- Stereo position: C. Avendano, “Frequency-domain source identification and manipulation in stereo mixes for enhancement, suppression and re-panning applications”, IEEE WASPAA 2003 ([doi](https://doi.org/10.1109/ASPAA.2003.1285818)); D. Barry, B. Lawlor & E. Coyle, “Sound Source Separation: Azimuth Discrimination and Resynthesis” (ADRess), DAFx-04, 2004 ([link](https://dafx.de/paper-archive/details/jK8kPFnlWXDwy1Kumk3kkQ)).

**Practice player and music analysis**

- [Signalsmith Stretch](https://github.com/Signalsmith-Audio/signalsmith-stretch) (Geraint Luff / Signalsmith Audio, MIT): the audio engine for speed and pitch change. The 1.3.2 web build is included with a two-line patch, see `experiments/vendor/signalsmith-stretch/README.md`.
- [wavesurfer.js](https://github.com/katspaugh/wavesurfer.js) (BSD-3-Clause) 7.12.12: waveform, timeline and region selection.
- Design references: the audio engine design of [Woodshed](https://github.com/KyleKincer/woodshed) (MIT), the limiter settings of [BandBuddy](https://github.com/dourgey/BandBuddy) (Apache-2.0), the loop speed trainer of [Steminize](https://github.com/Gerschwin/steminize) (MIT), and the stem weighting for key and chord detection plus the Albrecht–Shanahan key profile from [StemDeck](https://github.com/stemdeckapp/stemdeck) (Apache-2.0).
- [Beat This!](https://github.com/CPJKU/beat_this) (CPJKU, ISMIR 2024; code and weights MIT): beat and bar detection.
- [S-KEY](https://github.com/deezer/skey) (Deezer, ICASSP 2025, MIT): key detection model, included in `experiments/vendor/skey/`; needs [nnAudio](https://github.com/KinWaiCheuk/nnAudio) (MIT).
- Key profile tables from [Essentia](https://github.com/MTG/essentia)'s key.cpp (AGPL-3.0); the template + Viterbi chord recipe follows the example in librosa's documentation.
- [Basic Pitch](https://github.com/spotify/basic-pitch) (Spotify, ICASSP 2022, Apache-2.0): MIDI transcription, run with [ONNX Runtime](https://github.com/microsoft/onnxruntime) (MIT); MIDI I/O with [pretty_midi](https://github.com/craffel/pretty-midi) (MIT) and [mido](https://github.com/mido/mido) (MIT), evaluation with [mir_eval](https://github.com/mir-evaluation/mir_eval) (MIT), resampling with [resampy](https://github.com/bmcfee/resampy) (ISC).
- [Playwright](https://github.com/microsoft/playwright-python) (Apache-2.0): only for the player's automated tests.

**Python libraries**

[PyTorch](https://github.com/pytorch/pytorch) (BSD-3-Clause), [NumPy](https://github.com/numpy/numpy) (BSD-3-Clause), [SciPy](https://github.com/scipy/scipy) (BSD-3-Clause), [librosa](https://github.com/librosa/librosa) (ISC; McFee et al., SciPy 2015, [doi](https://doi.org/10.25080/Majora-7b98e3ed-003)), [python-soundfile](https://github.com/bastibe/python-soundfile) (BSD-3-Clause) with its bundled [libsndfile](https://github.com/libsndfile/libsndfile) (LGPL-2.1+; MP3 via LAME and mpg123), [PyAV](https://github.com/PyAV-Org/PyAV) (BSD-3-Clause) with [FFmpeg](https://ffmpeg.org) (LGPL-2.1+), [scikit-learn](https://github.com/scikit-learn/scikit-learn) (BSD-3-Clause), [Matplotlib](https://github.com/matplotlib/matplotlib) (Matplotlib License), [ML Collections](https://github.com/google/ml_collections) (Apache-2.0), [PyYAML](https://github.com/yaml/pyyaml) (MIT).
