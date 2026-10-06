#!/usr/bin/env bash
# Recovery for av==10.0.0 sdist metadata failure (Cython missing).
# Prepared only: continue the existing isolated environment, never recreate it.
# PyPI and upstream API verification: 2026-09-22. Runtime checks run on execution.
set -euo pipefail
umask 022

readonly SAD_ROOT='/root/autodl-tmp/avatar-motion/EXP-001'
readonly SAD_REPO="$SAD_ROOT/SadTalker"
readonly SAD_ENV="$SAD_ROOT/env-sadtalker"
readonly SAD_PY="$SAD_ENV/bin/python"
readonly SAD_COMMIT='cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8'
readonly SAD_AV_SHA256='0220fce2a62d71cc5e89617419b6224ddb43f1753b00f68b5c9af8b5f41d38c9'
readonly SAD_AV_URL='https://files.pythonhosted.org/packages/0a/5f/5ab859d8770ac1203d492e418cf949cfcac5c25994e9754c536fb37578fc/av-12.3.0-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl'
SAD_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SAD_SCRIPT_DIR
SAD_SIZE="${SAD_SIZE:-256}"
SAD_PREPROCESS="${SAD_PREPROCESS:-crop}"
case "$SAD_SIZE" in 256|512) ;; *) exit 2 ;; esac
case "$SAD_PREPROCESS" in crop|extcrop|resize|full|extfull) ;; *) exit 2 ;; esac
test -x "$SAD_PY"
test -f "$SAD_SCRIPT_DIR/权重清单.json"
test "$(git -C "$SAD_REPO" rev-parse HEAD)" = "$SAD_COMMIT"
test -f "$SAD_REPO/requirements.cli.txt"

export PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME
export TORCH_HOME="$SAD_ROOT/cache/sadtalker/torch"
export XDG_CACHE_HOME="$SAD_ROOT/cache/sadtalker/xdg"
export MPLCONFIGDIR="$SAD_ROOT/cache/sadtalker/matplotlib"
export PIP_CACHE_DIR="$SAD_ROOT/cache/sadtalker/pip"
export PIP_REQUIRE_VIRTUALENV=true

mkdir -p "$SAD_ROOT/logs" "$SAD_ROOT/evidence/sadtalker"
SAD_RUN_ID="recovery1-$(date -u +%Y%m%dT%H%M%SZ)-$$"
readonly SAD_RUN_ID
readonly SAD_EVIDENCE="$SAD_ROOT/evidence/sadtalker"
SAD_LOG="$SAD_ROOT/logs/sadtalker-install-$SAD_RUN_ID.log"
exec > >(tee -a "$SAD_LOG") 2>&1
trap 'printf "Recovery failed at line %s; preserve logs and partial files. No automatic retry.\n" "$LINENO" >&2' ERR
printf 'Recovery log: %s\nProfile: size=%s preprocess=%s\n' "$SAD_LOG" "$SAD_SIZE" "$SAD_PREPROCESS"
cd "$SAD_REPO"

# Confirm the existing environment before any package mutation.
"$SAD_PY" - <<'PY'
from importlib.metadata import version
from pathlib import Path
import platform
import sys
assert sys.version_info[:2] == (3, 10), sys.version
assert sys.platform == 'linux' and platform.machine() == 'x86_64'
assert Path(sys.prefix).resolve() == Path('/root/autodl-tmp/avatar-motion/EXP-001/env-sadtalker')
assert 'include-system-site-packages = false' in (Path(sys.prefix)/'pyvenv.cfg').read_text().lower()
for package, expected in [('torch', '2.0.1+cu118'), ('torchvision', '0.15.2+cu118'), ('torchaudio', '2.0.2+cu118'), ('numpy', '1.23.4')]:
    actual = version(package)
    assert actual == expected, (package, actual, expected)
    print('Reusing:', package, actual)
PY

# Exact official Linux CPython 3.10 wheel. Hash is supplied by PyPI metadata.
# Direct URL bypasses a mirror choosing an sdist. No FFmpeg/Cython compilation.
"$SAD_PY" -m pip install --retries 0 --no-deps --only-binary=:all: \
  "$SAD_AV_URL#sha256=$SAD_AV_SHA256"

SAD_CONSTRAINTS="$SAD_EVIDENCE/constraints-$SAD_RUN_ID.txt"
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
av==12.3.0
safetensors==0.4.5
EOF
# Preserve the original requirements and constraints; resume only dependencies.
"$SAD_PY" -m pip install --retries 0 --no-build-isolation --only-binary=av \
  -c "$SAD_CONSTRAINTS" -r requirements.cli.txt
"$SAD_PY" -m pip check

# Exercise the actual old torchvision writer and PyAV decoder before GB weights.
"$SAD_PY" - "$SAD_EVIDENCE" "$SAD_RUN_ID" <<'PY'
from pathlib import Path
import json
import sys
import av
import numpy as np
import torch
import torchvision
import torchaudio

assert av.__version__ == '12.3.0', av.__version__
assert torch.__version__ == '2.0.1+cu118', torch.__version__
assert torchvision.__version__ == '0.15.2+cu118', torchvision.__version__
assert torchaudio.__version__ == '2.0.2+cu118', torchaudio.__version__
frame = av.VideoFrame.from_ndarray(np.zeros((64, 64, 3), dtype=np.uint8), format='rgb24')
frame.pict_type = 'NONE'
assert hasattr(av, 'AVError')
destination = Path(sys.argv[1]) / f'torchvision-write-video-{sys.argv[2]}.mp4'
if destination.exists():
    raise FileExistsError(destination)
frames = torch.zeros((25, 64, 64, 3), dtype=torch.uint8)
for index in range(25):
    frames[index, :, :, 0] = index * 10
torchvision.io.write_video(str(destination), frames, fps=25, video_codec='libx264')
with av.open(str(destination)) as container:
    fps = float(container.streams.video[0].average_rate)
    decoded = list(container.decode(video=0))
assert len(decoded) == 25 and fps == 25, (len(decoded), fps)
assert all(f.width == 64 and f.height == 64 for f in decoded)
report = {'av': av.__version__, 'torch': torch.__version__, 'torchvision': torchvision.__version__,
          'pict_type_string_NONE': True, 'write_video_decode_passed': True,
          'frames': len(decoded), 'fps': fps, 'output': str(destination)}
with destination.with_suffix('.json').open('x') as handle:
    json.dump(report, handle, indent=2)
print(json.dumps(report))
PY

"$SAD_PY" -c 'import torch, torchvision, torchaudio, numpy, librosa, numba, PIL, cv2, av; from src.utils.preprocess import CropAndExtract; from src.test_audio2coeff import Audio2Coeff; from src.facerender.animate import AnimateFromCoeff; assert torch.cuda.is_available(); print("SadTalker imports passed; CUDA:", torch.cuda.get_device_name(0)); print("PyAV:", av.__version__)'
"$SAD_PY" inference.py --help > "$SAD_EVIDENCE/inference-help-$SAD_RUN_ID.txt"
"$SAD_PY" -m pip freeze > "$SAD_EVIDENCE/pip-freeze-$SAD_RUN_ID.txt"
git rev-parse HEAD > "$SAD_EVIDENCE/git-commit-$SAD_RUN_ID.txt"

# Resume the selected four assets. Reuse complete files after size/hash recording;
# reject existing .part files or wrong sizes instead of overwriting old evidence.
"$SAD_PY" - "$SAD_SCRIPT_DIR/权重清单.json" "$SAD_REPO" "$SAD_EVIDENCE" "$SAD_SIZE" "$SAD_PREPROCESS" "$SAD_RUN_ID" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen

manifest_path, repo_arg, evidence_arg, size_arg, preprocess, run_id = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
repo = Path(repo_arg).resolve()
selected = [a for a in manifest['assets'] if
            (a['role'] == 'renderer' and a['size_profile'] == int(size_arg)) or
            (a['role'] == 'mapping' and preprocess in a['preprocess_profiles']) or
            a['role'] in ('required_alignment', 'required_detection')]
assert len(selected) == 4
log_path = Path(evidence_arg) / f'weights-observed-{run_id}.jsonl'
with log_path.open('x', encoding='utf-8') as log:
    for asset in selected:
        destination = (repo / asset['path']).resolve()
        if not destination.is_relative_to(repo):
            raise ValueError('Weight destination escapes repository')
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + '.part')
        if partial.exists():
            raise FileExistsError(f'Preserve and inspect existing partial: {partial}')
        reused = destination.exists()
        if reused and destination.stat().st_size != asset['bytes']:
            raise ValueError(f'Wrong existing size; preserve and inspect: {destination}')
        if not reused:
            request = Request(asset['url'], headers={'User-Agent': 'SadTalker-EXP-001-recovery1'})
            with urlopen(request, timeout=60) as response, partial.open('xb') as handle:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
            checked_path = partial
        else:
            checked_path = destination
        digest = hashlib.sha256()
        with checked_path.open('rb') as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        observed_bytes = checked_path.stat().st_size
        observed_sha256 = digest.hexdigest()
        record = dict(asset, reused=reused, observed_bytes=observed_bytes,
                      observed_sha256=observed_sha256,
                      expected_size_matches=(observed_bytes == asset['bytes']),
                      upstream_sha256_verified=False)
        log.write(json.dumps(record, ensure_ascii=False) + '\n')
        log.flush()
        if observed_bytes != asset['bytes']:
            raise ValueError(f'Size mismatch; retained {checked_path}')
        if asset.get('sha256') and observed_sha256 != asset['sha256']:
            raise ValueError(f'SHA-256 mismatch; retained {checked_path}')
        if not reused:
            if destination.exists():
                raise FileExistsError(destination)
            partial.rename(destination)
        print('Reused' if reused else 'Downloaded', asset['path'], observed_bytes, observed_sha256)
PY

printf '%s\n' 'Recovery/install/video-I/O-check/weights finished. SadTalker portrait inference and visual acceptance have NOT run.'
printf 'Python: %s\nRepository: %s\nLog: %s\n' "$SAD_PY" "$SAD_REPO" "$SAD_LOG"
