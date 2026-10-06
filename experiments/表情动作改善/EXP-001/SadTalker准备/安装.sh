#!/usr/bin/env bash
# Prepared only. Execute on the remote Linux host after review.
# Independent Python 3.10 venv; no writes to the MuseTalk environment.
# Usage: SAD_BOOTSTRAP_PY=/path/to/python3.10 bash 安装.sh
# Optional: SAD_SIZE=512 SAD_PREPROCESS=full (default: 256 / crop).
set -euo pipefail
umask 022

readonly SAD_ROOT='/root/autodl-tmp/avatar-motion/EXP-001'
readonly SAD_REPO="$SAD_ROOT/SadTalker"
readonly SAD_ENV="$SAD_ROOT/env-sadtalker"
readonly SAD_COMMIT='cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8'
SAD_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SAD_SCRIPT_DIR
SAD_BOOTSTRAP_PY="${SAD_BOOTSTRAP_PY:-python3.10}"
SAD_SIZE="${SAD_SIZE:-256}"
SAD_PREPROCESS="${SAD_PREPROCESS:-crop}"
case "$SAD_SIZE" in 256|512) ;; *) printf '%s\n' 'SAD_SIZE must be 256 or 512.' >&2; exit 2 ;; esac
case "$SAD_PREPROCESS" in crop|extcrop|resize|full|extfull) ;; *) printf '%s\n' 'Unsupported SAD_PREPROCESS.' >&2; exit 2 ;; esac

for SAD_TOOL in git ffmpeg ffprobe; do
  command -v "$SAD_TOOL" >/dev/null || { printf 'Required command missing: %s\n' "$SAD_TOOL" >&2; exit 2; }
done
command -v "$SAD_BOOTSTRAP_PY" >/dev/null || { printf '%s\n' 'Set SAD_BOOTSTRAP_PY to a Python 3.10 executable.' >&2; exit 2; }
"$SAD_BOOTSTRAP_PY" -c 'import sys; assert sys.version_info[:2] == (3, 10), sys.version; print(sys.version)'
test -f "$SAD_SCRIPT_DIR/权重清单.json"

# Existing experiments are preserved. Do not retry over a partial installation.
if [[ -e "$SAD_REPO" || -e "$SAD_ENV" ]]; then
  printf '%s\n' 'SadTalker or env-sadtalker already exists. Preserve it and inspect before any resume; this script will not overwrite it.' >&2
  exit 2
fi
mkdir -p "$SAD_ROOT/logs" "$SAD_ROOT/evidence/sadtalker" "$SAD_ROOT/cache/sadtalker"
SAD_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
readonly SAD_RUN_ID
SAD_LOG="$SAD_ROOT/logs/sadtalker-install-$SAD_RUN_ID.log"
exec > >(tee -a "$SAD_LOG") 2>&1
printf 'Installation log: %s\n' "$SAD_LOG"
df -h "$SAD_ROOT"
trap 'printf "Installation failed at line %s; keep the log and partial files. No automatic retry.\n" "$LINENO" >&2' ERR

export PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME
export TORCH_HOME="$SAD_ROOT/cache/sadtalker/torch"
export XDG_CACHE_HOME="$SAD_ROOT/cache/sadtalker/xdg"
export MPLCONFIGDIR="$SAD_ROOT/cache/sadtalker/matplotlib"
export PIP_CACHE_DIR="$SAD_ROOT/cache/sadtalker/pip"
export PIP_REQUIRE_VIRTUALENV=true

git init "$SAD_REPO"
git -C "$SAD_REPO" remote add origin https://github.com/OpenTalker/SadTalker.git
git -C "$SAD_REPO" fetch --depth 1 origin "$SAD_COMMIT"
git -C "$SAD_REPO" checkout --detach FETCH_HEAD
test "$(git -C "$SAD_REPO" rev-parse HEAD)" = "$SAD_COMMIT"
"$SAD_BOOTSTRAP_PY" -m venv "$SAD_ENV"
readonly SAD_PY="$SAD_ENV/bin/python"
"$SAD_PY" -c 'import sys; from pathlib import Path; assert sys.prefix != sys.base_prefix; p=Path(sys.prefix)/"pyvenv.cfg"; assert "include-system-site-packages = false" in p.read_text().lower(); print(sys.executable)'
cd "$SAD_REPO"

"$SAD_PY" -m pip install --retries 0 'pip==24.0' 'setuptools==69.5.1' wheel
"$SAD_PY" -m pip install --retries 0 torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2 \
  --index-url https://download.pytorch.org/whl/cu118

SAD_CONSTRAINTS="$SAD_ROOT/evidence/sadtalker/constraints-$SAD_RUN_ID.txt"
cat > "$SAD_CONSTRAINTS" <<'EOF'
torch==2.0.1
torchvision==0.15.2
torchaudio==2.0.2
numpy==1.23.4
numba==0.57.1
llvmlite==0.40.1
Pillow==9.5.0
opencv-python==4.8.1.78
scikit-learn==1.1.3
matplotlib==3.7.5
contourpy==1.1.1
gfpgan==1.3.8
av==10.0.0
safetensors==0.4.5
EOF
"$SAD_PY" -c 'from pathlib import Path; p=Path("requirements.txt"); Path("requirements.cli.txt").write_text("\n".join(x for x in p.read_text().splitlines() if x.strip() != "gradio")+"\n")'
"$SAD_PY" -m pip install --retries 0 'numpy==1.23.4'
"$SAD_PY" -m pip install --retries 0 --no-build-isolation -c "$SAD_CONSTRAINTS" -r requirements.cli.txt
"$SAD_PY" -m pip check
"$SAD_PY" -c 'import sys, torch, torchvision, torchaudio, numpy, librosa, numba, PIL, cv2; from src.utils.preprocess import CropAndExtract; from src.test_audio2coeff import Audio2Coeff; from src.facerender.animate import AnimateFromCoeff; print(sys.executable); print(torch.__version__, torchvision.__version__, torchaudio.__version__); assert torch.__version__.startswith("2.0.1"); assert torch.version.cuda == "11.8", torch.version.cuda; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))'
"$SAD_PY" inference.py --help > "$SAD_ROOT/evidence/sadtalker/inference-help-$SAD_RUN_ID.txt"
"$SAD_PY" -m pip freeze > "$SAD_ROOT/evidence/sadtalker/pip-freeze-$SAD_RUN_ID.txt"
git rev-parse HEAD > "$SAD_ROOT/evidence/sadtalker/git-commit-$SAD_RUN_ID.txt"
ffmpeg -version
ffprobe -version

# Download exactly four assets for the selected profile. No retries or enhancers.
# .part files are retained on failure. Official SHA-256 is unavailable.
"$SAD_PY" - "$SAD_SCRIPT_DIR/权重清单.json" "$SAD_REPO" "$SAD_ROOT/evidence/sadtalker" "$SAD_SIZE" "$SAD_PREPROCESS" "$SAD_RUN_ID" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen

manifest_path, repo_arg, evidence_arg, size_arg, preprocess, run_id = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
repo = Path(repo_arg).resolve()
evidence = Path(evidence_arg)
selected = [a for a in manifest['assets'] if
            (a['role'] == 'renderer' and a['size_profile'] == int(size_arg)) or
            (a['role'] == 'mapping' and preprocess in a['preprocess_profiles']) or
            a['role'] in ('required_alignment', 'required_detection')]
assert len(selected) == 4, selected
print('Selected weight bytes:', sum(a['bytes'] for a in selected))
log_path = evidence / f'weights-observed-{run_id}.jsonl'
with log_path.open('x', encoding='utf-8') as log:
    for asset in selected:
        destination = (repo / asset['path']).resolve()
        if not destination.is_relative_to(repo):
            raise ValueError('Weight destination escapes repository')
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + '.part')
        if destination.exists() or partial.exists():
            raise FileExistsError(f'Preserve existing weight/partial: {destination}')
        digest = hashlib.sha256()
        observed_bytes = 0
        request = Request(asset['url'], headers={'User-Agent': 'SadTalker-EXP-001-preparation'})
        with urlopen(request, timeout=60) as response, partial.open('xb') as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
                digest.update(chunk)
                observed_bytes += len(chunk)
        observed_sha256 = digest.hexdigest()
        record = dict(asset, observed_bytes=observed_bytes, observed_sha256=observed_sha256,
                      expected_size_matches=(observed_bytes == asset['bytes']),
                      upstream_sha256_verified=False)
        log.write(json.dumps(record, ensure_ascii=False) + '\n')
        log.flush()
        if observed_bytes != asset['bytes']:
            raise ValueError(f'Size mismatch; retained {partial}: {observed_bytes} != {asset["bytes"]}')
        if asset.get('sha256') and observed_sha256 != asset['sha256']:
            raise ValueError(f'SHA-256 mismatch; retained {partial}')
        if destination.exists():
            raise FileExistsError(f'Destination appeared during download: {destination}')
        partial.rename(destination)
        print(asset['path'], observed_bytes, observed_sha256)
PY

printf '%s\n' 'Installation/import/weight-download steps finished. Actual video inference and visual acceptance have NOT run.'
printf 'Repository: %s\nPython: %s\nProfile: size=%s preprocess=%s\n' "$SAD_REPO" "$SAD_PY" "$SAD_SIZE" "$SAD_PREPROCESS"
printf '%s\n' 'Run from the repository directory. For a motion comparison omit --still and all enhancer options; preserve --verbose intermediates.'
