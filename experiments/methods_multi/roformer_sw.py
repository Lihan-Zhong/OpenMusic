"""BS-RoFormer-SW (community 6-stem model by jarredou): vocals, drums, bass, guitar, piano, other.

Weights: .cache/bs-roformer/ (~700 MB, re-hosted community checkpoint; licence not stated, so
personal experiments only). Inference via the MIT-licensed bs-roformer-infer package.
"""
import warnings

import torch
import yaml
from bs_roformer.download import ensure_model_assets
from bs_roformer.inference import SafeLoaderWithTuple
from bs_roformer.utils import demix_track, get_model_from_config
from ml_collections import ConfigDict

from common import ROOT

warnings.filterwarnings("ignore", message=".*torch.cuda.amp.autocast.*")
warnings.filterwarnings("ignore", message=".*CUDA is not available.*")

CKPT, CFG = ensure_model_assets("roformer-model-bs-roformer-sw-by-jarredou", models_dir=ROOT / ".cache" / "bs-roformer")
with open(CFG) as f:
    CONFIG = ConfigDict(yaml.load(f, Loader=SafeLoaderWithTuple))
MODEL = get_model_from_config("bs_roformer", CONFIG)
MODEL.load_state_dict(torch.load(CKPT, map_location="cpu"))
MODEL.eval()
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
MODEL.to(DEVICE)


def separate(mix, sr):
    out, _ = demix_track(CONFIG, MODEL, torch.from_numpy(mix).float(), DEVICE, first_chunk_time=0.0)
    return {k: v[:, : mix.shape[1]] for k, v in out.items()}
