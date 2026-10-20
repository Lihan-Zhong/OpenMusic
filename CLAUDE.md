# OpenMusic: notes for Claude

## Goal and scope
A small, personal, open-source practice tool for musicians, like MWM's "Audio Jam": split a song into
vocals / drums / bass / guitar / other and practise with the stems (per-stem volume, mute/solo, A-B loop,
speed/pitch). Desktop only: Python backend plus a locally served web page. The user does NOT want a mobile
app or a big software-engineering effort; keep proposals small. Talk to the user in Chinese.

## Current state (Oct 2026)
- Core purpose (user's words): "方便练琴扒谱子". Judge features by whether they help practising / transcribing.
- Practice player works: `analyze.py` (tempo/beats/bars via Beat This! small0, key = S-KEY + Shaath profiles,
  chords = template + Viterbi on stems) -> `transcribe.py` (Basic Pitch ONNX -> MIDI with tempo map + notes.json)
  -> `build_player.py` (Signalsmith Stretch engine, wavesurfer.js UI; gain to +12 dB with limiter, speed regions,
  A-B loop + speed trainer, pitch shift, metronome on detected beats, chord lane, piano roll). See README.
- Workbench `experiments/app.py` (stdlib only, http://localhost:8780) is the main entry: upload to songs/ ->
  split (Demucs 6 or RoFormer via `evaluate_multi.py <model> --full --no-score --only=<song>`) -> analyze ->
  transcribe -> build_player, one job at a time; the player links back to it. Hardened: 127.0.0.1, Host check,
  X-OpenMusic header on POST, no framing, static files only under outputs/, song-name validator.
- Each split song folder has stems.json (model, format, ...; written after the last stem = split finished);
  export_mp3.py carries it along. Libraries: outputs/demucs6_full, outputs/roformer_sw_full, outputs/mp3 (hand-collected).
- Heavy: run RoFormer on the Windows GPU, not on the 16 GB Mac.
- Separation experiments (docs/experiments.md): classical DSP best `combo` 4.8 dB; Demucs 9.2 dB (3 stems);
  BS-RoFormer-SW 10.3 dB. The README only keeps a short model guide.
- Known gaps: chord extensions are hints; Beat This! locks to 2x/4x in noise passages (Carsick 140-306 s, no drums):
  analyze.py evens the grid out (regular_beats, bar_lines; transcribe.py uses it as-is), but there it is a guess;
  guitar MIDI is a dense draft, fast low bass is missed; nobody has listened to slow-speed quality yet.

## Testing
- `experiments/tests/test_analyze.py` (~20 s), `experiments/tests/test_transcribe.py` (~6 s),
  `experiments/tests/player_test.py` (~1 min, headless Chromium via Playwright, dev deps in requirements-dev.txt,
  browsers in .cache/ms-playwright). `--memory`/`--cpu` decode the 6:45 song (~1.5 GB): run them alone.
- `experiments/vendor/signalsmith-stretch/SignalsmithStretch.mjs` carries a documented two-line patch; re-apply
  it if upgrading. basic-pitch is installed with --no-deps (requirements-nodeps.txt).

## Next steps
- On the Windows desktop (RTX 4070 Ti): install requirements.txt + requirements-nodeps.txt, run
  `experiments/tests/player_test.py` there, split new songs with RoFormer on the GPU, then analyze -> transcribe
  -> build_player. It has no GitHub login: send patches to the Mac session, which pushes.
- Have the user listen: slow speeds (0.5-0.75x), a boosted quiet stem, ±12 semitones, and judge MIDI per stem.

## Conventions
- Work inside this folder. Don't browse the user's personal folders; ask them to put files here.
- Keep downloads inside the project: `common.py` sets TORCH_HOME / HF_HOME to `.cache/`;
  RoFormer weights go to `.cache/bs-roformer/`.
- Never commit `data/`, `songs/`, `outputs/` (copyright / licence); see `.gitignore`.
- Run scripts from the project root with the project venv. Tune only on the train split, test once.
- Don't add Claude as a co-author or contributor in commits or PRs.
- Code is AGPL-3.0: the user wants the project and every derivative to stay open source, including
  network/web deployments. Dependencies and copied code must be AGPL-3.0-compatible (MIT, BSD, ISC,
  Apache-2.0, LGPL, GPL-3.0 are fine; avoid GPL-2.0-only, non-commercial or proprietary code).
  README is bilingual and Chinese is the default language: README.md is Chinese (shown by GitHub),
  README.en.md is English. Keep them in sync.
- Output formats (the user's rule): finished full-song stems default to MP3 (`--lossless` for FLAC);
  anything used to compare methods by ear stays lossless FLAC. Keep stems sample-aligned.
- On the 16 GB Mac, run at most one heavy model job at a time.
