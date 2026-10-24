# OpenMusic: notes for coding agents

Instructions for AI coding agents (Claude Code, Codex, Cursor, ...) and human contributors working on this
repository. The user-facing documentation is the README (README.md in Chinese, README.en.md in English).

## What it is
An open-source practice and transcription tool for musicians: split a song into 6 stems (vocals, drums, bass,
guitar, piano, other), detect beats, bars, key and chords, transcribe the stems to MIDI and a Guitar Pro file, and
practise with the stems in a locally served web page (per-stem volume, mute/solo, A-B loop, speed and pitch).
Python backend plus local web pages; desktop only. Judge a change by whether it helps practising or transcribing,
and keep changes small and focused.

## Pipeline
- Workbench `experiments/app.py` (standard library only, http://localhost:8780, 127.0.0.1 on Windows) is the main
  entry, started by `OpenMusic.command` (macOS) / `OpenMusic.bat` (Windows), which run `app.py --reuse`. Per song,
  one job at a time: split (`evaluate_multi.py <model> --full --no-score --only=<song>`, Demucs 6-stem or
  BS-RoFormer-SW) -> `analyze.py` -> `transcribe.py` -> `export_gp.py` -> `build_player.py`. Songs dropped into
  `songs/` appear on their own. Hardened: 127.0.0.1 only, Host check, X-OpenMusic header on POST, no framing,
  static files only under `outputs/`, a song-name validator.
- `analyze.py`: beats and bars with Beat This! (small0); key = S-KEY plus key profiles on the stems; chords =
  templates + Viterbi. In noise passages Beat This! can lock to 2x/4x; the grid is evened out there
  (regular_beats, bar_lines) and is a guess.
- `transcribe.py`: Basic Pitch (ONNX) per pitched stem, with thresholds scaled to how sure it is about the stem
  (scaled_settings), MIDI with the analysis tempo map plus notes.json. Drums: `drums.py` (onset candidates + a
  small MLP, weights `experiments/models/drums.npz` trained by `train_drums.py` on E-GMD and `drum_synth.py`
  loops) -> `transcription/drums.mid` on channel 10, or the optional ADTOF-pytorch plugin (below).
- `export_gp.py` + `gp7.py`: a Guitar Pro 8 `.gp` per song, laid out with PyGuitarPro and written by alphaTab
  1.8.4 (vendored) inside mini-racer. The guitar track starts muted; an audio track (by default a mix of vocals,
  guitar and other; the user can choose when downloading) has a sync point per bar. Details that matter:
  - layout fields as Guitar Pro 8 writes them (`gp8_layout`): a file GP8 had re-saved from alphaTab's CDATA layout
    held a garbage bars-per-line value and hung GP8 on opening;
  - drum double strokes (two hits of one drum a 32nd apart) go on 32nds, each run of strokes placed on its beats'
    grids (`drum_pairs`, `place_strokes`, `drum_ticks`); every other drum hit stays on 16ths; pitched tracks keep
    16ths and 8th-note triplets;
  - a drum hit is never tied (a tied drum note reads back as the kit's first piece, a snare);
  - an onset off its beat's grid raises instead of being written (a tick inside a triplet beat once made
    `triplet_pieces` loop and eat memory).
- `build_player.py`: the practice player (Signalsmith Stretch engine, wavesurfer.js): gain up to +12 dB with a
  limiter, speed regions, A-B loop and speed trainer, pitch shift, a metronome on the detected beats, chord lane,
  piano roll and drum lane. "⬇ Guitar Pro…" (player and workbench) POSTs to /api/gp, which runs
  `export_gp.py --out` to a temporary file and streams it; nothing is stored.
- Each split song folder has `stems.json` (model, format, ...), written after the last stem, so it marks a
  finished split; `export_mp3.py` carries it along. Libraries: `outputs/demucs6_full`, `outputs/roformer_sw_full`,
  `outputs/mp3`.
- Separation experiments are in docs/experiments.md (classical DSP up to 4.8 dB, Demucs, BS-RoFormer-SW 10.3 dB);
  drum transcription in docs/drums.md.

## Rules
- Stems are read-only for every step after the split; analysis, MIDI and Guitar Pro never change stem audio
  (test_transcribe checks the stems stay byte for byte the same). MIDI and Guitar Pro are references.
- Never commit `data/`, `songs/`, `outputs/` or `plugins/` (copyright and licences); see `.gitignore`.
- ADTOF-pytorch is an optional plugin the user installs into `plugins/`. Its weights are CC BY-NC-SA 4.0: never
  commit, vendor or import it; `transcribe.py --drums auto` runs its `adtof` command as a subprocess when it is
  installed, and drums.notes.json's "backend" says which model was used.
- Licence: AGPL-3.0, including network and web deployments. Dependencies and copied code must be
  AGPL-3.0-compatible (MIT, BSD, ISC, Apache-2.0, LGPL, GPL-3.0); no GPL-2.0-only, non-commercial or proprietary
  code. Vendored code in `experiments/vendor/` keeps its own licence.
- The README is bilingual with Chinese as the default: README.md (Chinese) and README.en.md (English) say the
  same; so do docs/*.md and docs/*.en.md. Change both together.
- Output formats: finished full-song stems default to MP3 (`--lossless` for FLAC); anything used to compare
  methods by ear stays lossless FLAC. Keep stems sample-aligned.
- Keep downloads inside the project: `common.py` sets TORCH_HOME / HF_HOME to `.cache/`; RoFormer weights go to
  `.cache/bs-roformer/`.
- Run scripts from the project root with the project venv (`.venv/bin/python`, on Windows `.venv\Scripts\python`).
  Tune only on the train split, and test once.
- RoFormer needs over 6 GB of memory: run it on an NVIDIA GPU, and run at most one heavy model job at a time.
- Commits carry no AI co-author or attribution lines.

## Testing
- `experiments/tests/test_analyze.py` (~20 s), `test_transcribe.py` (~6 s), `test_drums.py` (~5 s),
  `test_export_gp.py` (~5 s; `--songs outputs/<library>` also checks every stored `.gp`; it includes random drum
  parts), `player_test.py` (~1 min, headless Chromium via Playwright; dev requirements in requirements-dev.txt,
  browsers in `.cache/ms-playwright`; `--browser msedge` uses the installed Edge on Windows). `--memory` and
  `--cpu` decode a 6:45 song (~1.5 GB) from `outputs/mp3`: run them alone.
- `experiments/vendor/signalsmith-stretch/SignalsmithStretch.mjs` carries a documented two-line patch; re-apply it
  when upgrading. S-KEY is used only from `experiments/vendor/skey/`; never `pip install skey` (an unrelated
  package). basic-pitch is installed with `--no-deps` (requirements-nodeps.txt).
- docs/flow/render.py redraws the README diagram, docs/screenshots/shoot.py retakes the README screenshots (both
  need the dev requirements).

## Known gaps
- Chord extensions (7, sus, ...) are hints; key and chords are right roughly 70–80% of the time.
- Guitar MIDI is a dense draft; fast low bass is missed; repeated same-pitch notes can merge.
- Several guitars in one stem are not separated (lead vs rhythm): a planned direction.
- Guitar Pro 7 has not been tried (the file is in Guitar Pro 8's format).
