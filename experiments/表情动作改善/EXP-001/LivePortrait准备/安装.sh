#!/usr/bin/env bash
# Linux remote preparation only. This file has not been executed remotely.
set -Eeuo pipefail
umask 022

LP_EXP_ROOT="${LP_EXP_ROOT:-/root/autodl-tmp/avatar-motion/EXP-001}"
LP_REPO_DIR="$LP_EXP_ROOT/LivePortrait"
LP_ENV_DIR="$LP_EXP_ROOT/env-liveportrait"
LP_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LP_MANIFEST_PATH="${LP_MANIFEST_PATH:-$LP_SCRIPT_DIR/机器清单.json}"
LP_GIT_COMMIT=9b294b3d0536135442ea73cb01e6cb3ca7029dd3
LP_GIT_URL=https://github.com/KlingAIResearch/LivePortrait.git

for lp_command in conda git ffmpeg ffprobe; do
  command -v "$lp_command" >/dev/null || {
    printf 'Missing command: %s\n' "$lp_command" >&2
    exit 1
  }
done
test -f "$LP_MANIFEST_PATH"
mkdir -p "$LP_EXP_ROOT/logs" "$LP_EXP_ROOT/inputs" "$LP_EXP_ROOT/outputs/liveportrait"
LP_INSTALL_LOG="$LP_EXP_ROOT/logs/liveportrait-install-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LP_INSTALL_LOG") 2>&1
trap 'printf "LivePortrait setup failed at line %s; see %s\n" "$LINENO" "$LP_INSTALL_LOG" >&2' ERR

if [[ -e "$LP_ENV_DIR" ]]; then
  test -d "$LP_ENV_DIR/conda-meta"
  test -x "$LP_ENV_DIR/bin/python"
else
  conda create --yes --prefix "$LP_ENV_DIR" python=3.10
fi
LP_PYTHON="$LP_ENV_DIR/bin/python"
"$LP_PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 10), sys.version'

if [[ -e "$LP_REPO_DIR" ]]; then
  test -d "$LP_REPO_DIR/.git"
  test "$(git -C "$LP_REPO_DIR" rev-parse HEAD)" = "$LP_GIT_COMMIT"
  test -z "$(git -C "$LP_REPO_DIR" status --porcelain --untracked-files=no)"
else
  git init "$LP_REPO_DIR"
  git -C "$LP_REPO_DIR" remote add origin "$LP_GIT_URL"
  git -C "$LP_REPO_DIR" fetch --depth 1 origin "$LP_GIT_COMMIT"
  git -C "$LP_REPO_DIR" checkout --detach FETCH_HEAD
fi

"$LP_PYTHON" -m pip install \
  torch==2.3.0 torchvision==0.18.0 torchaudio==2.3.0 \
  --index-url https://download.pytorch.org/whl/cu118
"$LP_PYTHON" -m pip install \
  -r "$LP_REPO_DIR/requirements.txt" huggingface_hub==0.25.2
"$LP_PYTHON" -m pip check

"$LP_PYTHON" - "$LP_REPO_DIR" "$LP_MANIFEST_PATH" <<'PY'
import hashlib
import json
import pathlib
import sys
from huggingface_hub import snapshot_download

repo = pathlib.Path(sys.argv[1])
manifest = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
weights_root = repo / "pretrained_weights"
snapshot_download(
    repo_id=manifest["hf_repo"],
    revision=manifest["hf_revision"],
    local_dir=str(weights_root),
    allow_patterns=[item["path"] for item in manifest["weights"]],
    max_workers=4,
)
for item in manifest["weights"]:
    path = weights_root / item["path"]
    assert path.stat().st_size == item["bytes"], f"Size mismatch: {path}"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    assert digest.hexdigest() == item["sha256"], f"SHA256 mismatch: {path}"
    print(f"verified {item['path']} ({item['bytes']} bytes)")
print(f"Verified {len(manifest['weights'])} human-model weights.")
PY

cd "$LP_REPO_DIR"
"$LP_PYTHON" inference.py --help > "$LP_EXP_ROOT/logs/liveportrait-cli-help.txt"
"$LP_PYTHON" -m pip freeze > "$LP_EXP_ROOT/logs/liveportrait-pip-freeze.txt"
"$LP_PYTHON" - <<'PY'
import json
import torch
import onnxruntime
print(json.dumps({
    "torch": torch.__version__,
    "torch_cuda": torch.version.cuda,
    "cuda_available": torch.cuda.is_available(),
    "onnxruntime": onnxruntime.__version__,
    "onnx_available_providers": onnxruntime.get_available_providers(),
}, indent=2))
assert torch.version.cuda == "11.8", torch.version.cuda
assert torch.cuda.is_available(), "CUDA unavailable; no model inference attempted."
assert "CUDAExecutionProvider" in onnxruntime.get_available_providers()
PY
printf 'Environment and weight preparation complete. No LivePortrait inference has run.\n'
