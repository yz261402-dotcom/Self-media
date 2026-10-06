#!/bin/bash
set -u
ROOT=/root/autodl-tmp/avatar-musetalk/MT-001
cd "$ROOT/source" || exit 2
(set -o noclobber; date -Ins > "$ROOT/logs/M234.started") || exit 3
export OMP_NUM_THREADS=8 MKL_NUM_THREADS=8 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TORCH_HOME="$ROOT/torch-cache"
nvidia-smi --query-gpu=timestamp,memory.used,utilization.gpu --format=csv -l 1 > "$ROOT/logs/M234-gpu.csv" &
MONITOR=$!
trap 'kill "$MONITOR" 2>/dev/null || true' EXIT
START=$(date +%s)
timeout 1200 "$ROOT/env/bin/python" -u -m scripts.realtime_inference_mt001 --inference_config configs/mt001/M234.yaml --unet_model_path models/musetalkV15/unet.pth --unet_config models/musetalkV15/musetalk.json --version v15 --fps 25 --batch_size 4 > "$ROOT/logs/M234-inference.log" 2>&1
RC=$?
END=$(date +%s)
printf '{"exit_code":%s,"wall_seconds":%s,"started_unix":%s,"ended_unix":%s}\n' "$RC" "$((END-START))" "$START" "$END" > "$ROOT/logs/M234-process.json"
exit "$RC"
