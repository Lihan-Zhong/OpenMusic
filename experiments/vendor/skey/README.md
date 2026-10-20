# S-KEY (vendored)

Source: https://github.com/deezer/skey at commit 918b83d273568d5041569bb8068843d19a335726 (2025-06-09), MIT licence (see LICENSE, Copyright (c) 2019-present, Deezer SA). Not on PyPI: the PyPI package called `skey` is an unrelated project, never `pip install skey`.

Copied unchanged: `skey/chromanet.py`, `skey/convnext.py` (originally from Meta FAIR's ConvNeXt, MIT), `skey/hcqt.py`, `skey/models/skey.pt` (the pretrained checkpoint shipped in the same MIT repository; sha256 78dfd0ad4fa9434bf7cec70a25934b7c575bda9c80e994700140770ad3a5ead4). `__init__.py` is OpenMusic's own (empty, so that `import skey.chromanet` finds this folder and nothing else). `experiments/analyze.py` loads the checkpoint with `torch.load(weights_only=True)` and a small allow-list, and replaces upstream's `key_detection.py` with a short wrapper. Needs `nnAudio==0.3.3` (MIT).

Paper: Kong, Meseguer-Brocal, Lostanlen, Lagrange, Hennequin, "S-KEY: Self-supervised Learning of Major and Minor Keys from Audio", ICASSP 2025, arXiv:2501.12907.
