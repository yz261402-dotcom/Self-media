#!/usr/bin/env python3
"""MuseTalk 阶段 runner：动态底片 + 同一条最终音频 → 口型原片。

用法（由 worker 调用）：
    python runner_musetalk.py <job.json> <result.json>

job args = {"plate_url", "audio_url", "plate_hash", "audio_hash"}

关键约束（来自 experiments/MuseTalk部署/MT-001/使用说明.md 与「部署记录」）：
- audio 必须是**同一条最终音频**，本 runner 不接受任何替换音频的参数；
- 每个作业用独立 task_id 与独立 result_dir，避免覆盖已有证据；
- avatar 相同时 MuseTalk 有 parties 缓存，同类作业天然快一些，但这是它的行为，
  本 runner 不宣称缓存命中，也不据此跳过任何步骤。

本 runner 未在真实算力云实例上执行过（写完后仅作静态检查）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import runner_common as rc


def run(job_path: str, result_path: str) -> None:
    job = rc.read_job(job_path)
    args = job["args"]
    job_id = str(job.get("job_id") or Path(job_path).parent.name)
    work_dir = str(Path(job_path).parent)
    notes: list = [f"MuseTalk 阶段 runner v1；job_id={job_id}"]

    try:
        root = rc.env_required("M2_MT_ROOT")
        python = rc.env_required("M2_MT_PYTHON")
        source_dir = os.path.join(root, "source")

        plate = os.path.join(work_dir, "plate.mp4")
        audio = os.path.join(work_dir, "audio.wav")
        rc.fetch_and_verify(args["plate_url"], plate, args.get("plate_hash", ""), "动态底片")
        rc.fetch_and_verify(args["audio_url"], audio, args.get("audio_hash", ""), "最终音频")

        # MuseTalk 的配置用 OmegaConf.load 读取；JSON 是 YAML 子集，可直接写入 .yaml
        task_id = f"m2_{job_id}"
        cfg_path = os.path.join(work_dir, f"{task_id}.yaml")
        result_name = f"{task_id}.mp4"
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({task_id: {"video_path": plate, "audio_path": audio,
                                 "result_name": result_name, "bbox_shift": 0}},
                      f, ensure_ascii=False, indent=2)

        result_dir = os.path.join(root, "results", task_id)
        argv = [
            python, "-u", "-m", "scripts.inference_mt001",
            "--inference_config", cfg_path,
            "--result_dir", result_dir,
            "--unet_model_path", "models/musetalkV15/unet.pth",
            "--unet_config", "models/musetalkV15/musetalk.json",
            "--version", "v15", "--fps", "25", "--batch_size", "4",
            "--use_float16",
        ]
        # 与 MT-001 的 run-M1.sh 同一套离线/线程约定：权重已在本地，
        # 不设 HF_HUB_OFFLINE 会在无网时先去 HF 找一圈再失败，排查时很容易误判。
        env = rc.child_env({"OMP_NUM_THREADS": "8", "MKL_NUM_THREADS": "8",
                            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                            "TORCH_HOME": os.path.join(root, "torch-cache")})
        proc = rc.run_capture(argv, cwd=source_dir, env=env,
                              timeout=int(os.environ.get("M2_MT_TIMEOUT_SEC", "3600")))
        tail = proc.stdout.decode("utf-8", "replace")[-4000:]
        if proc.returncode != 0:
            rc.write_result(result_path, rc.fail_result({
                "code": "MUSETALK_FAILED",
                "error": f"inference 退出码 {proc.returncode}", "notes": notes + [tail]}))
            return

        produced = os.path.join(result_dir, "v15", result_name)
        if not os.path.isfile(produced):
            found = rc.find_file(result_dir, result_name)
            if not found:
                rc.write_result(result_path, rc.fail_result({
                    "code": "MUSETALK_OUTPUT_MISSING",
                    "error": f"未找到输出 {result_name}（退出码 0 不等于出了片）",
                    "notes": notes + [tail]}))
                return
            produced = found

        rc.write_result(result_path, {
            "state": "succeeded", "output_path": produced,
            "duration_ms": rc.probe_duration_ms(produced),
            "sha256": rc.sha256_file(produced),
            "notes": notes + ["原片须经四维门禁与音轨核对，并经人工确认后才算完成"],
        })
    except Exception as e:  # noqa: BLE001
        rc.write_result(result_path, rc.fail_result({
            "code": "MUSETALK_RUNNER_ERROR",
            "error": f"{type(e).__name__}: {e}", "notes": notes}))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法：runner_musetalk.py <job.json> <result.json>")
    run(sys.argv[1], sys.argv[2])
