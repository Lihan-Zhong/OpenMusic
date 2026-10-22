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
- Drums: `drums.py` (onset candidates + MLP, weights experiments/models/drums.npz trained by `train_drums.py` on
  E-GMD CC BY 4.0 + `drum_synth.py` loops) -> transcription/drums.mid on channel 10; player drum lane. Held-out
  MDB Drums (Demucs-split): kick 0.90, snare 0.82, hi-hat 0.64, cymbals 0.49, toms 0.16 (docs/drums.md).
  Optional plugin ADTOF-pytorch (user installs into git-ignored plugins/; weights CC BY-NC-SA, never commit or
  import it): transcribe.py --drums auto uses its `adtof` CLI as a subprocess when installed (better on real
  recordings: hi-hat 0.79, cymbals 0.83); drums.notes.json "backend" says which.
- Guitar Pro: `export_gp.py` + `gp7.py` (alphaTab 1.8.4 in mini-racer) write transcription/<song>.gp in GP8's format:
  the guitar track starts muted, and an audio track with a sync point per bar, laid out as GP8 stores it
  (Content/Assets/<id>.mp3), carries by default a mix of vocals + guitar + other (the user's choice: bass, drums,
  vocal melody MIDI are accurate, guitar is learnt by ear). "⬇ Guitar Pro…" in the player and workbench lets
  the user pick stems / the original song / none; POST /api/gp runs export_gp.py --out to a temp file and streams
  it (nothing stored). Checked in GP8 on the user's Mac.
  If "no sound" is reported, check the audio output first (it was AirPods once), not the file.
  If GP8 hangs and eats memory opening a .gp (80 GB once, froze the Mac): a .gp GP8 had saved again had
  ScoreSystemsDefaultLayout -1955444992 (alphaTab writes it in CDATA, GP8 does not read that); gp7.gp8_layout now
  writes plain numbers. GP8-saved copies made before Oct 21 can still hang: re-download or set that number to 3.
  Drum double strokes (two hits of one drum a 32nd apart, Surabaya's snare) go on 32nds (drum_pairs, drum_ticks);
  a drum hit is never tied (a tied drum note reads back as the kit's first piece, a snare). Pitched tracks keep
  16ths/triplets: their same-pitch pairs that merge are mostly one held note split by Basic Pitch.
- Player usability pass (eec5571): loops can be cleared (✕, Esc, double-click), Set A/B keys, a sticky transport
  with a loop readout, trainer progress and restore, a restored-settings notice and 本曲恢复默认; checked by the
  ux section of `tests/player_test.py`. Stems are read-only for every step but the split (test_transcribe checks).
- Workbench `experiments/app.py` (stdlib only, http://localhost:8780) is the main entry, started by double-clicking
  OpenMusic.command (Mac) / OpenMusic.bat (Windows), which run `app.py --reuse` (opens a running one instead of a
  second); songs dropped into songs/ appear on their own, and "全部一键处理" queues every unfinished song: upload to songs/ ->
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
  guitar MIDI is a dense draft, fast low bass is missed; transcribe.py scales Basic Pitch's thresholds by the
  stem's confidence (scaled_settings: 75th percentile / 0.6, floor 0.25 one-voice / 0.3 polyphonic), which
  fixed Surabaya's bass (23 % -> 59 % of its sounding time covered); repeated same-pitch notes can still merge; nobody has listened to slow-speed quality yet. The user
  hears damage in the guitar stem: separation or the stretch engine, not yet measured.

## Testing
- `experiments/tests/test_analyze.py` (~20 s), `experiments/tests/test_transcribe.py` (~6 s),
  `experiments/tests/test_drums.py` (~5 s),
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
