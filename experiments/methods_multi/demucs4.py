"""Demucs htdemucs, 4 stems: vocals, drums, bass, other. Bass reference for the 6-stem models."""
import common  # noqa: F401  (keeps model downloads inside the project)
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model

NAME = "htdemucs"
MODEL = get_model(NAME)
MODEL.eval()
DEVICE = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"


def separate(mix, sr):
    wav = torch.from_numpy(mix).float()
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        out = apply_model(MODEL, ((wav - mean) / std)[None], device=DEVICE, shifts=0,
                          split=True, overlap=0.25, progress=False)[0]
    out = (out * std + mean).cpu().numpy()
    return dict(zip(MODEL.sources, out))
