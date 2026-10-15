"""Shared helpers for the separation experiments: paths, audio I/O, the 3-stem target set, SDR scoring."""
import os
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
GT = DATA / "gt"                    # GT/<split>/<track>/{mixture,vocals,drums,other}.wav
EXCERPTS = DATA / "song_excerpts"   # 60 s excerpts of the user's songs, for listening
OUTPUTS = ROOT / "outputs"          # OUTPUTS/<method>/<song>/<stem>.wav
RESULTS = ROOT / "results"          # RESULTS/<method>_<split>.json

# Keep every download (model weights, caches) inside the project folder.
os.environ.setdefault("TORCH_HOME", str(ROOT / ".cache" / "torch"))
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))

SR = 44100
STEMS = ("vocals", "drums", "other")  # "other" = bass + everything that is not vocals or drums


def load(path, sr=SR):
    """Read audio as float32, shape (2, samples). Formats libsndfile can't read (e.g. m4a) go through PyAV."""
    try:
        y, file_sr = sf.read(str(path), dtype="float32", always_2d=True)
        y = y.T
    except sf.LibsndfileError:
        y, file_sr = _decode_av(path, sr), sr
    if y.shape[0] == 1:
        y = np.repeat(y, 2, axis=0)
    if file_sr != sr:
        import librosa
        y = librosa.resample(y, orig_sr=file_sr, target_sr=sr)
    return np.ascontiguousarray(y[:2])


def _decode_av(path, sr):
    import av
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=sr)
        chunks = []
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray() for f in resampler.resample(frame)]
        chunks += [f.to_ndarray() for f in resampler.resample(None)]
    return np.concatenate(chunks, axis=1).astype(np.float32)


def save(path, y, sr=SR):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    y = np.clip(np.asarray(y, dtype=np.float32), -1.0, 1.0)
    sf.write(str(path), y.T, sr, subtype="PCM_16")


def sdr(ref, est, eps=1e-8):
    """Global SDR in dB, as in the Sound Demixing Challenge. NaN when the target is absent (silent) in the clip."""
    ref = np.asarray(ref, np.float64)
    est = np.asarray(est, np.float64)
    n = min(ref.shape[-1], est.shape[-1])
    ref, est = ref[..., :n], est[..., :n]
    num = np.sum(ref ** 2)
    if num / ref.size < 1e-8:  # RMS below -80 dBFS
        return float("nan")
    return float(10 * np.log10((num + eps) / (np.sum((ref - est) ** 2) + eps)))


def tracks(split):
    return sorted(p for p in (GT / split).iterdir() if p.is_dir())


def load_track(track_dir):
    mix = load(track_dir / "mixture.wav")
    refs = {s: load(track_dir / f"{s}.wav") for s in STEMS}
    return mix, refs
