#!/bin/bash
# 在已部署的算力云执行；四个阶段共用一个 GPU 串行队列。
set -euo pipefail
: "${M2_WORKER_TOKEN:?需要注入既有 Worker 令牌}"
worker_root=/root/autodl-tmp/avatar-m2worker
export M2_LP_ROOT=/root/autodl-tmp/avatar-motion/EXP-001
export M2_LP_PYTHON="$M2_LP_ROOT/env-liveportrait/bin/python"
export M2_MT_ROOT=/root/autodl-tmp/avatar-musetalk/MT-001
export M2_MT_PYTHON="$M2_MT_ROOT/env/bin/python"
export ASR_ROOT=/root/autodl-tmp/avatar-subtitles/ASR-001
subtitle_python="$ASR_ROOT/env/bin/python"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=4
exec "$M2_LP_PYTHON" "$worker_root/worker_server.py" \
  --host 127.0.0.1 --port 8080 --public-base http://127.0.0.1:18080 \
  --runner-motion "$M2_LP_PYTHON $worker_root/runner_liveportrait.py" \
  --runner-lipsync "$M2_MT_PYTHON $worker_root/runner_musetalk.py" \
  --runner-asr "$subtitle_python $worker_root/runner_qwen_asr.py" \
  --runner-align "$subtitle_python $worker_root/runner_qwen_align.py" \
  --work-root "$worker_root/jobs" --timeout 3600
