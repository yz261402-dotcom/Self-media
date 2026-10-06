#!/usr/bin/env python3
"""LivePortrait 阶段 runner：把已审人物图 + 已获准动作参考合成动态底片。

用法（由 worker 调用）：
    python runner_liveportrait.py <job.json> <result.json>

job args = {"source_image_url", "motion_ref_url", "output_seconds",
            "source_hash", "motion_hash"}

关键约束（来自 experiments/表情动作改善/EXP-001/LivePortrait部署核验.md）：
- `-s` 必须是同一张已审人物图，`-d` 只提供动作，两者角色不可互换；
- LivePortrait 没有 `--output_fps`，输出帧率 = int(驱动视频 fps)，
  所以驱动必须先归一到 **CFR 25**，否则输出时长会漂；
- 驱动短于目标时长时循环到目标帧数，**循环接缝必须记录**；
- 输出命名 `{source}--{driving}.mp4`；同目录的 `_concat.mp4` 是三联可视化，
  不能直接喂给 MuseTalk。

本 runner 未在真实算力云实例上执行过（写完后仅作静态检查），
不作任何"已跑通"的宣称。
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import runner_common as rc

FPS = 25


def _basename(path: str) -> str:
    return Path(path).stem


def prepare_driving(driving: str, target_seconds: float, work_dir: str) -> tuple:
    """把驱动视频归一到 CFR 25，并截到 ceil(seconds*25) 帧。

    返回 (实际路径, notes)。循环补齐是真实发生的处理，必须写进 notes，
    不能让下游以为这段动作本来就是连续的。
    """
    frames = max(1, int(math.ceil(float(target_seconds) * FPS)))
    # 目标时长按**帧数**算：ceil(秒×帧率)，不足一帧向上取整，
    # 宁可多一帧也不能让底片比音频短（短了就是截字）
    target_ms = frames * 1000.0 / FPS
    notes: list = []
    probe_ms = rc.probe_duration_ms(driving)
    need_loop = False
    if probe_ms is None or probe_ms <= 0:
        notes.append("无法探测驱动时长（ffprobe 不可用或失败），按 stream_loop 兜底处理")
        need_loop = True
    elif probe_ms < target_ms:
        need_loop = True
    trimmed = os.path.join(work_dir, "driving_cfr25.mp4")
    argv = ["ffmpeg", "-nostdin", "-y"]
    if need_loop:
        # 只重复一次会让 3 秒参考在 60 秒口播中仅产生 6 秒底片。
        # 循环输入直至后面的 -frames:v 达到目标，帧数仍是明确上限。
        argv += ["-stream_loop", "-1"]
        if probe_ms is not None and probe_ms > 0:
            seams = max(0, math.ceil(target_ms / probe_ms) - 1)
            notes.append(f"驱动素材约 {probe_ms / 1000:g}s，已按目标 {frames} 帧循环补齐；"
                         f"预计 {seams} 处循环接缝，需人工在底片审核时查看")
        else:
            notes.append(f"已循环至目标 {frames} 帧；源时长未知，无法估算循环接缝数量，"
                         "需人工在底片审核时查看")
    argv += ["-i", driving, "-map", "0:v:0", "-an", "-vf", f"fps={FPS}",
             "-frames:v", str(frames), "-c:v", "libx264", "-crf", "18",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", trimmed]
    proc = rc.run_capture(argv, cwd=work_dir, timeout=1800)
    if proc.returncode != 0 or not os.path.isfile(trimmed):
        raise RuntimeError("驱动视频归一化失败：" +
                           proc.stdout.decode("utf-8", "replace")[-1500:])
    return trimmed, notes


def run(job_path: str, result_path: str) -> None:
    job = rc.read_job(job_path)
    args = job["args"]
    work_dir = str(Path(job_path).parent)

    notes: list = [f"LivePortrait 阶段 runner v1；job_id={job.get('job_id')}"]
    try:
        root = rc.env_required("M2_LP_ROOT")
        python = rc.env_required("M2_LP_PYTHON")
        source_python_dir = os.path.join(root, "LivePortrait")

        source = os.path.join(work_dir, "source" + Path(args["source_image_url"]).suffix)
        driving_raw = os.path.join(work_dir, "driving_raw.mp4")
        rc.fetch_and_verify(args["source_image_url"], source,
                            args.get("source_hash", ""), "人物源图")
        rc.fetch_and_verify(args["motion_ref_url"], driving_raw,
                            args.get("motion_hash", ""), "动作参考")

        driving, prep_notes = prepare_driving(driving_raw, float(args["output_seconds"]),
                                              work_dir)
        notes += prep_notes
        outputs = os.path.join(work_dir, "outputs", "liveportrait")
        os.makedirs(outputs, exist_ok=True)

        # 输入 basename 在各作业中相同；输出必须留在作业私有目录，避免覆盖历史底片。
        # 参数是**连字符**（tyro 生成），写成下划线会被 argparse/tyro 判为未知参数。
        # 这点在算力云实例的 `inference.py --help` 上逐条核对过。
        argv = [python, "inference.py", "-s", source, "-d", driving, "-o", outputs,
                "--flag-crop-driving-video", "--animation-region", "all",
                "--driving-option", "expression-friendly", "--driving-multiplier", "1.0"]
        proc = rc.run_capture(argv, cwd=source_python_dir,
                              timeout=int(os.environ.get("M2_LP_TIMEOUT_SEC", "3600")))
        tail = proc.stdout.decode("utf-8", "replace")[-4000:]
        if proc.returncode != 0:
            rc.write_result(result_path, rc.fail_result({
                "code": "LIVEPORTRAIT_FAILED",
                "error": f"inference.py 退出码 {proc.returncode}", "notes": notes + [tail]}))
            return

        expected = f"{_basename(source)}--{_basename(driving)}.mp4"
        produced = os.path.join(outputs, expected)
        if not os.path.isfile(produced):
            # 命名规则变了就先找到再说，但不能把三联图当成结果交出去
            found = rc.find_file(outputs, expected)
            if not found:
                rc.write_result(result_path, rc.fail_result({
                    "code": "LIVEPORTRAIT_OUTPUT_MISSING",
                    "error": f"未找到预期输出 {expected}（退出码 0 不等于出了片）",
                    "notes": notes + [tail]}))
                return
            produced = found
        if produced.endswith("_concat.mp4") or "_concat" in produced:
            rc.write_result(result_path, rc.fail_result({
                "code": "LIVEPORTRAIT_OUTPUT_IS_CONCAT",
                "error": "产出是三联可视化图，不能当底片使用", "notes": notes}))
            return

        rc.write_result(result_path, {
            "state": "succeeded", "output_path": produced,
            "duration_ms": rc.probe_duration_ms(produced),
            "sha256": rc.sha256_file(produced),
            "notes": notes + ["底片为融合背景的完整画面，须经人工审核后才可对口型"],
        })
    except Exception as e:  # noqa: BLE001 - 任何异常都必须是 failed，不是崩溃后留空
        rc.write_result(result_path, rc.fail_result({
            "code": "LIVEPORTRAIT_RUNNER_ERROR",
            "error": f"{type(e).__name__}: {e}", "notes": notes}))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法：runner_liveportrait.py <job.json> <result.json>")
    run(sys.argv[1], sys.argv[2])
