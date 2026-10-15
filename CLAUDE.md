# OpenMusic: notes for Claude

## Goal and scope
A small, personal, open-source practice tool for musicians, like MWM's "Audio Jam": split a song into
vocals / drums / bass / guitar / other and practise with the stems (per-stem volume, mute/solo, A-B loop,
speed/pitch). Desktop only: Python backend plus a locally served web page. The user does NOT want a mobile
app or a big software-engineering effort; keep proposals small. Talk to the user in Chinese.

## Current state (Oct 2026)
- Experiment harness in `experiments/` (see README). Scores are median SDR on 50 MUSDB18 7 s test clips.
- Classical DSP methods (`experiments/methods/`): best single family HPSS 4.4 dB; `combo` (learned per-band
  fusion of 5 families) 4.8 dB. AI is far ahead: Demucs 9.2 dB (3 stems); BS-RoFormer-SW 10.3 dB
  (4-stem score), clearly better on vocals/drums. Experiment reports: `results/method_info.json`.
- MUSDB has no guitar stem, so guitar quality is judged by ear on the user's songs in `songs/`.
- Not finished: RoFormer-SW has not yet been run on the user's song (it froze the 16 GB Mac).

## Next step on the desktop (RTX 4070 Ti)
Set up per README, put the user's songs in `songs/`, then run
`experiments/make_excerpts.py`, `experiments/evaluate_multi.py roformer_sw --split test --song --full`,
`experiments/build_page.py`, and let the user compare guitar stems (Demucs 6-stem vs RoFormer-SW).

## Conventions
- Work inside this folder. Don't browse the user's personal folders; ask them to put files here.
- Keep downloads inside the project: `common.py` sets TORCH_HOME / HF_HOME to `.cache/`;
  RoFormer weights go to `.cache/bs-roformer/`.
- Never commit `data/`, `songs/`, `outputs/` (copyright / licence); see `.gitignore`.
- Run scripts from the project root with the project venv. Tune only on the train split, test once.
- Don't add Claude as a co-author or contributor in commits or PRs.
- Code is GPL-3.0; only add GPL-compatible dependencies. README is bilingual: keep README.md (English)
  and README.zh-CN.md (Chinese) in sync.
- On the 16 GB Mac, run at most one heavy model job at a time.
