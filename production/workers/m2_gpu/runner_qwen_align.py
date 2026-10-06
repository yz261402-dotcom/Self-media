#!/usr/bin/env python3
"""Qwen3-ForcedAligner-0.6B 阶段 runner：确认稿 + 真实音频 → 字词时间轴。

用法（由 worker 调用）：
    python runner_qwen_align.py <job.json> <result.json>

job args = {"audio_url", "audio_hash", "text"}

约束：
- `text` 是**确认稿**，这一阶段真的会把它交给模型做强制对齐，
  所以稿件必须是与音频对应的那一版：换音频或改稿后旧时间轴不能继续用；
- 音频哈希不符即失败；
- 输出时间轴单位为秒，逐条带 text/start/end，交给后端换算成毫秒 cue。

强制对齐的性质要说清：它给出的是"这段文本在这条音频里的位置"，
**不能证明原文真的被完整读出**。口型与听感仍是人工验收的事。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import runner_common as rc

DEFAULT_ASR_ROOT = "/root/autodl-tmp/avatar-subtitles/ASR-001"


def run(job_path: str, result_path: str) -> None:
    job = rc.read_job(job_path)
    args = job["args"]
    job_id = str(job.get("job_id") or Path(job_path).parent.name)
    work_dir = str(Path(job_path).parent)
    notes: list = [f"Qwen3-ForcedAligner runner v1；job_id={job_id}"]

    try:
        root = os.environ.get("ASR_ROOT") or DEFAULT_ASR_ROOT
        if not os.path.isdir(root):
            rc.write_result(result_path, rc.fail_result({
                "code": "ASR_ROOT_MISSING",
                "error": f"云端推理目录不存在: {root}", "notes": notes}))
            return

        text = str(args.get("text") or "").strip()
        if not text:
            rc.write_result(result_path, rc.fail_result({
                "code": "ALIGN_TEXT_EMPTY",
                "error": "强制对齐需要确认稿正文；空稿无法产出时间轴", "notes": notes}))
            return

        audio = os.path.join(work_dir, "audio.wav")
        rc.fetch_and_verify(args["audio_url"], audio, args.get("audio_hash", ""), "待对齐音频")

        text_path = os.path.join(work_dir, "script.txt")
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(text)

        out_dir = os.path.join(work_dir, "align")
        argv = [sys.executable, "-u", os.path.join(root, "qwen_inference.py"),
                "--task", "align", "--audio", audio, "--text", text_path,
                "--models-root", "models", "--output-dir", out_dir]
        asr_job_id = str(args.get('asr_job_id') or '')
        if asr_job_id:
            if not re.fullmatch(r'[0-9a-f]{16}', asr_job_id):
                raise ValueError('ASR 作业标识非法')
            asr_result = Path(work_dir).parent / asr_job_id / 'asr' / '结果.json'
            if not asr_result.is_file():
                raise ValueError('ASR 作业结果不可用，无法生成长音频时间轴')
            argv.extend(['--asr-result', str(asr_result)])
        env = rc.child_env({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        proc = rc.run_capture(argv, cwd=root, env=env,
                              timeout=int(os.environ.get("ALIGN_TIMEOUT_SEC", "1800")))
        tail = proc.stdout.decode("utf-8", "replace")[-4000:]
        if proc.returncode != 0:
            rc.write_result(result_path, rc.fail_result({
                "code": "ALIGN_INFERENCE_FAILED",
                "error": f"云端推理.py 退出码 {proc.returncode}", "notes": notes + [tail]}))
            return

        produced = os.path.join(out_dir, "结果.json")
        if not os.path.isfile(produced):
            rc.write_result(result_path, rc.fail_result({
                "code": "ALIGN_OUTPUT_MISSING",
                "error": "未找到 结果.json（退出码 0 不等于跑完了）", "notes": notes + [tail]}))
            return

        with open(produced, encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("status") != "succeeded":
            rc.write_result(result_path, rc.fail_result({
                "code": "ALIGN_STATUS_NOT_SUCCEEDED",
                "error": f"结果 status={doc.get('status')!r}",
                "notes": notes + [json.dumps(doc, ensure_ascii=False)[:1500]]}))
            return

        items = doc.get("alignment") or []
        if not items:
            rc.write_result(result_path, rc.fail_result({
                "code": "ALIGN_EMPTY",
                "error": "对齐结果为空；空时间轴不得进入字幕", "notes": notes}))
            return
        if str(doc.get("time_unit") or "seconds") != "seconds":
            rc.write_result(result_path, rc.fail_result({
                "code": "ALIGN_UNIT_UNKNOWN",
                "error": f"时间单位未知: {doc.get('time_unit')!r}（只接受 seconds）",
                "notes": notes}))
            return

        rc.write_result(result_path, {
            "state": "succeeded", "output_path": produced,
            "sha256": rc.sha256_file(produced),
            "alignment": items,
            "alignment_text": doc.get("alignment_text") or "",
            "duration_seconds": (doc.get("audio") or {}).get("duration_seconds"),
            "notes": notes + ["时间轴来源=forced_alignment；均分时长不算对齐"],
        })
    except Exception as e:  # noqa: BLE001
        rc.write_result(result_path, rc.fail_result({
            "code": "ALIGN_RUNNER_ERROR",
            "error": f"{type(e).__name__}: {e}", "notes": notes}))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法：runner_qwen_align.py <job.json> <result.json>")
    run(sys.argv[1], sys.argv[2])
