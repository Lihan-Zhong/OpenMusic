"""AI reference point: Demucs htdemucs (4 stems), with bass folded into "other" to match our 3 stems."""
import common  # noqa: F401  (sets TORCH_HOME / HF_HOME inside the project)
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model

MODEL = get_model("htdemucs")
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
    src = dict(zip(MODEL.sources, out))
    return {"vocals": src["vocals"], "drums": src["drums"], "other": src["bass"] + src["other"]}
