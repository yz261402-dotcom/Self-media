#!/usr/bin/env python3
"""Qwen3-ASR-1.7B 阶段 runner：真实音频 → 口播识别文本。

用法（由 worker 调用）：
    python runner_qwen_asr.py <job.json> <result.json>

job args = {"audio_url", "audio_hash", "text"}

约束（照搬 ASR-001 部署的既有纪律，不要在这里放宽）：
- 音频必须是**登记过的那一份**：哈希不符即失败，不许换文件继续跑；
- `--text` 只作为输入版本记录，**不送给 ASR 当提示词**——否则模型照抄原文，
  识别差异会被掩盖，ASR 就失去了"核对口播内容"的意义；
- 每次作业用独立输出目录：云端推理.py 拒绝覆盖已有目录，这是防止证据被悄悄改写；
- 长音频在推理入口按低能量边界分段，逐段识别后合并，不截断全文。

识别文本用来核对"音频里到底说了什么"，**不直接当字幕时间轴**——
时间轴只能来自强制对齐（runner_qwen_align.py）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import runner_common as rc

DEFAULT_ASR_ROOT = "/root/autodl-tmp/avatar-subtitles/ASR-001"


def run(job_path: str, result_path: str) -> None:
    job = rc.read_job(job_path)
    args = job["args"]
    job_id = str(job.get("job_id") or Path(job_path).parent.name)
    work_dir = str(Path(job_path).parent)
    notes: list = [f"Qwen3-ASR runner v1；job_id={job_id}"]

    try:
        root = os.environ.get("ASR_ROOT") or DEFAULT_ASR_ROOT
        if not os.path.isdir(root):
            rc.write_result(result_path, rc.fail_result({
                "code": "ASR_ROOT_MISSING",
                "error": f"云端推理目录不存在: {root}", "notes": notes}))
            return

        audio = os.path.join(work_dir, "audio.wav")
        rc.fetch_and_verify(args["audio_url"], audio, args.get("audio_hash", ""), "待识别音频")

        text_path = os.path.join(work_dir, "script.txt")
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(str(args.get("text") or ""))

        out_dir = os.path.join(work_dir, "asr")
        argv = [sys.executable, "-u", os.path.join(root, "qwen_inference.py"),
                "--task", "asr", "--audio", audio, "--text", text_path,
                "--models-root", "models", "--output-dir", out_dir]
        env = rc.child_env({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        proc = rc.run_capture(argv, cwd=root, env=env,
                              timeout=int(os.environ.get("ASR_TIMEOUT_SEC", "1800")))
        tail = proc.stdout.decode("utf-8", "replace")[-4000:]
        if proc.returncode != 0:
            rc.write_result(result_path, rc.fail_result({
                "code": "ASR_INFERENCE_FAILED",
                "error": f"云端推理.py 退出码 {proc.returncode}", "notes": notes + [tail]}))
            return

        produced = os.path.join(out_dir, "结果.json")
        if not os.path.isfile(produced):
            rc.write_result(result_path, rc.fail_result({
                "code": "ASR_OUTPUT_MISSING",
                "error": "未找到 结果.json（退出码 0 不等于跑完了）", "notes": notes + [tail]}))
            return

        with open(produced, encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("status") != "succeeded":
            rc.write_result(result_path, rc.fail_result({
                "code": "ASR_STATUS_NOT_SUCCEEDED",
                "error": f"结果 status={doc.get('status')!r}",
                "notes": notes + [json.dumps(doc, ensure_ascii=False)[:1500]]}))
            return

        rc.write_result(result_path, {
            "state": "succeeded", "output_path": produced,
            "sha256": rc.sha256_file(produced),
            "text": doc.get("text") or "",
            "language": doc.get("language") or "",
            "duration_seconds": (doc.get("audio") or {}).get("duration_seconds"),
            "notes": notes + ["识别文本只用于核对口播内容；字幕时间轴须由强制对齐给出"],
        })
    except Exception as e:  # noqa: BLE001
        rc.write_result(result_path, rc.fail_result({
            "code": "ASR_RUNNER_ERROR",
            "error": f"{type(e).__name__}: {e}", "notes": notes}))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法：runner_qwen_asr.py <job.json> <result.json>")
    run(sys.argv[1], sys.argv[2])
