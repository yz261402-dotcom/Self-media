"""固定本轮随机种子，运行原版 SadTalker，并登记唯一原片；不修改模型代码。"""
from pathlib import Path
import random
import runpy
import shutil
import sys

import numpy as np
import torch

root = Path("/root/autodl-tmp/avatar-motion/EXP-001")
repo = root / "SadTalker"
output = root / "results/S1"
if output.exists() and any(output.iterdir()):
    raise FileExistsError("Preserve existing SadTalker output")
seed = 20260922
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed_all(seed)
print("Experiment random seed:", seed, "bitwise determinism not guaranteed", flush=True)
sys.path.insert(0, str(repo))
sys.argv = [str(repo / "inference.py"),
            "--source_image", str(root / "inputs/person_B_even.png"),
            "--driven_audio", str(root / "inputs/voice3.wav"),
            "--result_dir", str(output), "--preprocess", "full",
            "--size", "512", "--pose_style", "0", "--expression_scale", "1.0",
            "--batch_size", "2", "--verbose"]
print("SadTalker arguments:", sys.argv, flush=True)
runpy.run_path(str(repo / "inference.py"), run_name="__main__")
originals = list(output.glob("*.mp4"))
if len(originals) != 1 or originals[0].stat().st_size == 0:
    raise RuntimeError("Expected exactly one non-empty SadTalker original")
with originals[0].open("rb") as source, (output / "S1_original.mp4").open("xb") as target:
    shutil.copyfileobj(source, target)
print("Original retained:", originals[0], flush=True)
