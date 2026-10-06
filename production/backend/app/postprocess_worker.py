"""T07 代码后期 Worker：确定性执行，不创建 Agent、不走 MCP。

v2.4 边界不变：不把 mock 作业意图当作媒体产物；依赖/能力未就绪时登记
blocked 并保留证据；恢复相同输入复用记录，新输入保留独立执行证据。

T07 起 `postprocess` 阶段具备**真实执行路径**（mode=production 且依赖齐备）：
统一时间线（EditTimeline）→ 真实 ASR 时间轴（未配置即 blocked，绝不均分臆造）
→ 字幕检查与 ASS → FFmpeg 两遍响度/CFR/烧录/封面 → 规格/解码/响度实测验收。
失败与阻断一律留证，不伪造成功产物。`publish_package` 调用内部交付服务，
只有当前成片经用户确认后才生成真实归档。
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from . import storage as st
from .contracts import now_iso
from .video import asr as v_asr
from .video import ffmpeg as v_ff
from .video import render as v_render
from .video import subtitles as v_sub
from .video import timeline as v_tl
from .video.errors import VideoBlocked, VideoRejected

STAGES = frozenset({"postprocess", "publish_package"})
WORKER_VERSION = "mvp-media-2"


def _runtime_root() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _runtime_path(rel_uri: str) -> str:
    """storage_uri（如 audio/<work>/<id>.wav）→ 本机绝对路径。"""
    root = os.path.realpath(_runtime_root())
    path = os.path.realpath(os.path.join(root, rel_uri.replace("/", os.sep)))
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not rel_uri or not inside or os.path.isabs(rel_uri):
        raise VideoBlocked("MEDIA_PATH_INVALID", "媒体地址必须位于 runtime 内")
    return path


def _snapshot(task: dict, work: dict, stage: str) -> dict:
    audios = st.all_rows("audios", "work_id=?", [task["work_id"]])
    media = [r for r in st.all_rows("media_jobs", "work_id=?", [task["work_id"]])
             if r.get("executor_kind") != "worker"]
    videos = st.all_rows("videos", "work_id=?", [task["work_id"]])
    batch = st.get("script_batches", task["work_id"]) or {}
    selected_files = {}
    for row in audios + videos:
        if row["id"] not in (work.get("final_audio_id"), work.get("video_asset_id")):
            continue
        try:
            path = _runtime_path(row.get("storage_uri") or "")
            selected_files[row["id"]] = _sha256_file(path) if os.path.isfile(path) else "missing"
        except (VideoBlocked, OSError):
            selected_files[row["id"]] = "unreadable"
    snapshot = {
        "worker_version": WORKER_VERSION,
        "selected_file_hashes": selected_files,
        "task_id": task["id"], "mode": task.get("mode"), "stage": stage,
        "route": task.get("route"), "route_confirmed": task.get("route_confirmed"),
        "work": work, "script_batch": batch,
        # ASR provider 纳入摘要：配置从无到有/换 provider 属于新输入，
        # 恢复时不能复用"因缺 ASR 而 blocked"的旧记录。
        "asr_provider": v_asr.provider_id(),
        "asr_worker_url": os.environ.get(v_asr.ASR_WORKER_URL_ENV, ""),
        "audios": sorted(audios, key=lambda r: r["id"]),
        "videos": sorted(videos, key=lambda r: r["id"]),
        "media": sorted(media, key=lambda r: r["id"]),
        "quality": sorted(st.all_rows("quality_decisions", "work_id=?", [task["work_id"]]),
                          key=lambda r: r["id"]),
        "video_reviews": sorted([r for v in videos for r in
                                 st.all_rows("video_reviews", "video_id=?", [v["id"]])],
                                key=lambda r: r["id"]),
        "audio_track_checks": sorted([r for v in videos for r in
                                      st.all_rows("audio_track_checks", "video_id=?", [v["id"]])],
                                     key=lambda r: r["id"]),
    }
    digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    return snapshot, digest


def _selected_script(work: dict, batch: dict) -> Optional[dict]:
    sid = work.get("script_version_id")
    for c in batch.get("candidates", []):
        if c.get("id") == sid:
            return c
    return None


def _final_audio(work: dict, audios: List[dict]) -> Optional[dict]:
    return next((a for a in audios
                 if a.get("id") == work.get("final_audio_id")
                 and a.get("work_id") == work.get("id")
                 and a.get("kind") == "full" and a.get("approved") and a.get("confirmed")
                 and not a.get("is_mock")
                 and a.get("script_version_id") == work.get("script_version_id")
                 and a.get("voice_asset_id") == work.get("voice_asset_id")), None)


def _original_video(work: dict) -> Optional[dict]:
    video = st.get("videos", work.get("video_asset_id") or "")
    if (not video or video.get("work_id") != work.get("id")
            or video.get("kind") != "original" or video.get("is_mock")
            or not video.get("confirmed") or video.get("approval_state") != "approved"
            or video.get("audio_asset_id") != work.get("final_audio_id")
            or video.get("identity_id") != work.get("identity_id")
            or video.get("variant_id") != work.get("variant_id")
            or video.get("route") != work.get("route")):
        return None
    return video


def _asr_cues_cached(audio: dict, audio_path: str, script_text: str) -> Dict[str, Any]:
    """ASR 时间轴缓存：音频、正文、提供方及 Worker 版本共同确定键。

    音频变更 → 哈希不符 → 重新对齐（旧时序绝不沿用）；同音频复用旧结果。
    """
    adapter = v_asr.require_adapter()
    file_hash = _sha256_file(audio_path)
    script_hash = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
    cache_key = json.dumps([audio["id"], file_hash, script_hash, adapter.provider_id,
                            adapter.alignment_source, adapter.simulated, WORKER_VERSION])
    cache_id = "asr_" + hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:32]
    cached = st.get("alignment_records", cache_id)
    if cached and cached.get("file_hash") == file_hash:
        return {**cached, "reused": True}
    cues = adapter.transcribe(audio_path, script_text)
    if adapter.alignment_source not in v_asr.ALLOWED_ALIGNMENT_SOURCES:
        raise VideoBlocked("ASR_ALIGNMENT_SOURCE_INVALID",
                           f"对齐来源 {adapter.alignment_source} 不在白名单")
    rec = {
        "id": cache_id, "audio_id": audio["id"], "file_hash": file_hash,
        "script_hash": script_hash,
        "provider_id": adapter.provider_id, "alignment_source": adapter.alignment_source,
        "simulated": bool(adapter.simulated), "cues": cues, "created_at": now_iso(),
        # 识别文本单独留痕：它回答"音频里说了什么"，用于和稿件对照，
        # 与时间轴是两件事，不能互相替代。
        "transcript": str(getattr(adapter, "last_transcript", "") or ""),
    }
    st.put("alignment_records", cache_id, rec, {"audio_id": audio["id"]})
    return {**rec, "reused": False}


def _run_real_postprocess(task: dict, work: dict, snapshot: dict) -> Dict[str, Any]:
    """真实后期执行。返回记录的状态字段；VideoBlocked/VideoRejected 由外层落库。"""
    audio = _final_audio(work, snapshot["audios"])
    if audio is None:
        raise VideoBlocked("DEPENDENCY_NOT_MET",
                           "真实后期需要已审且用户确认的当前全文音频（voice 与作品一致）")
    video = _original_video(work)
    if video is None:
        raise VideoBlocked("MEDIA_NOT_READY",
                           "真实后期需要已确认且绑定当前人物、变体与最终音频的口播原片")
    from .digital_human.gates import video_gate
    original_gate = video_gate(video["id"])
    if not original_gate["ok"]:
        raise VideoBlocked("ORIGINAL_NOT_APPROVED", "原片审核或音轨保持尚未通过",
                           detail=original_gate)
    script = _selected_script(work, snapshot["script_batch"])
    if script is None:
        raise VideoBlocked("DEPENDENCY_NOT_MET", "真实后期需要当前已选稿件")
    script_text = script.get("text") or ""
    if not script_text.strip():
        raise VideoBlocked("DEPENDENCY_NOT_MET", "已选稿件正文为空")
    # ffmpeg 能力位（缺失即 blocked，不得以"跳过渲染"蒙混）
    v_ff.ffmpeg_bin()

    audio_path = _runtime_path(audio.get("storage_uri") or "")
    video_path = _runtime_path(video.get("storage_uri") or "")
    if not os.path.isfile(audio_path):
        raise VideoBlocked("MEDIA_FILE_MISSING", f"最终音频文件缺失: {audio_path}")
    if not os.path.isfile(video_path):
        raise VideoBlocked("MEDIA_FILE_MISSING", f"口播原片文件缺失: {video_path}")
    for asset, path in ((audio, audio_path), (video, video_path)):
        actual = _sha256_file(path)
        if str(asset.get("file_hash") or "").removeprefix("sha256:") != actual:
            raise VideoBlocked("MEDIA_HASH_MISMATCH", "输入文件与已审核资产哈希不一致",
                               detail={"asset_id": asset["id"]})

    # 1) 真实 ASR/对齐（未配置 provider → ASR_NOT_CONFIGURED）
    asr_meta = _asr_cues_cached(audio, audio_path, script_text)

    # 2) 字幕源时间线来自选定音频实测时长；原片不得截断该音频。
    vinfo = v_ff.probe(video_path)
    if not any(s["codec_type"] == "video" for s in vinfo["streams"]):
        raise VideoBlocked("STREAM_MISSING", "原片缺少视频轨")
    ainfo = v_ff.probe(audio_path)
    audio_stream = next((s for s in ainfo["streams"] if s["codec_type"] == "audio"), None)
    if audio_stream is None:
        raise VideoBlocked("STREAM_MISSING", "最终音频缺少音频轨")
    src_ms = int(round(float(audio_stream.get("duration") or ainfo["format"]["duration"]) * 1000))
    video_ms = int(round(float(vinfo["format"]["duration"]) * 1000))
    if video_ms + 1000 / v_render.FPS < src_ms:
        raise VideoBlocked("VIDEO_SHORTER_THAN_AUDIO", "原片短于选定音频，不能截断正文以完成后期")
    tl = v_tl.EditTimeline.single(src_ms)
    tl.validate()

    # 3) 字幕：映射到终时间轴 + 硬检查（覆盖/越界/重叠/末句截断）
    cues = v_sub.map_cues(asr_meta["cues"], tl)
    checks = v_sub.check_cues(cues, final_duration_ms=tl.final_duration_ms,
                              script_text=script_text)

    # 4) 渲染：两遍响度 + CFR + 烧录 + AI 标识 + 封面
    exec_tag = snapshot.get("execution_id") or st.new_id("postprocess")
    out_dir = _runtime_path(f"deliverables/{task['work_id']}/{exec_tag}")
    os.makedirs(out_dir, exist_ok=True)
    ass_path = os.path.join(out_dir, f"{exec_tag}.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(v_sub.build_ass(cues, title=script.get("title") or ""))
    srt_path = os.path.join(out_dir, f"{exec_tag}.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(v_sub.build_srt(cues))
    master_path = os.path.join(out_dir, f"{exec_tag}.mp4")
    render_log = v_render.render_master(video_path=video_path, audio_path=audio_path,
                                        out_path=master_path, ass_path=ass_path)
    cover_path = os.path.join(out_dir, f"{exec_tag}_cover.jpg")
    v_render.render_cover(video_path=master_path, out_path=cover_path,
                          title=script.get("title") or "")

    # 5) 验收：规格 probe / 完整解码 / 响度实测 / 黑帧冻结粗筛 / faststart
    verify_log = v_render.verify_master(master_path)
    final_ms = round(verify_log["probe"]["duration"] * 1000)
    if abs(final_ms - src_ms) > 100:
        raise VideoBlocked("FINAL_DURATION_MISMATCH", "成片实测时长与已确认音频不一致")
    v_sub.check_cues(cues, final_duration_ms=final_ms, script_text=script_text)

    outputs = []
    for kind, p in (("master", master_path), ("subtitles_ass", ass_path), ("subtitles_srt", srt_path),
                    ("cover", cover_path)):
        outputs.append({"kind": kind, "storage_uri":
                        os.path.relpath(p, _runtime_root())
                        .replace(os.sep, "/"),
                        "sha256": _sha256_file(p), "bytes": os.path.getsize(p)})

    return {"state": "succeeded", "processing_outcome": "rendered",
            "simulated": bool(asr_meta["simulated"]),
            "error": None,
            "input_assets": {"audio_id": audio["id"], "audio_hash": audio["file_hash"],
                             "video_id": video["id"], "video_hash": video["file_hash"],
                             "identity_id": work.get("identity_id"), "variant_id": work.get("variant_id"),
                             "script_version_id": script["id"],
                             "script_hash": hashlib.sha256(script_text.encode("utf-8")).hexdigest()},
            "original_gate": original_gate,
            "timeline": {"source_ms": src_ms, "final_ms": tl.final_duration_ms,
                         "segments": [s.__dict__ for s in tl.segments]},
            "asr": {"provider_id": asr_meta["provider_id"],
                    "alignment_source": asr_meta["alignment_source"],
                    "simulated": asr_meta["simulated"], "reused": asr_meta["reused"],
                    "audio_id": audio["id"],
                    "cue_count": len(asr_meta["cues"])},
            "subtitle_checks": checks,
            "render_log": render_log, "verify_log": verify_log,
            "output_files": outputs}


def run_stage(task: dict, work: dict, stage: str, *, retry: bool = False) -> dict:
    if stage not in STAGES:
        raise ValueError(f"Not a postprocess worker stage: {stage}")

    snapshot, digest = _snapshot(task, work, stage)
    execution_id = f"worker_{task['id']}_{stage}_{digest[:20]}"
    matches = [r for r in st.all_rows("media_jobs", "work_id=?", [task["work_id"]])
               if r.get("input_digest") == digest and r.get("stage") == stage
               and r.get("task_id") == task["id"] and r.get("executor_kind") == "worker"]
    existing = max(matches, key=lambda r: r.get("created_at", "")) if matches else None
    if existing is not None:
        if existing.get("state") == "succeeded" or not retry:
            return existing
        execution_id = st.new_id(execution_id + "_retry")
    snapshot["execution_id"] = execution_id

    record: Dict[str, Any] = {
        "id": execution_id, "task_id": task["id"], "work_id": task["work_id"],
        "kind": "stage_execution", "stage": stage,
        "executor_kind": "worker", "worker_kind": "postprocess",
        "worker_version": WORKER_VERSION, "mode": task.get("mode"),
        "simulated": task.get("mode") == "mock", "state": "blocked",
        "processing_outcome": "not_run", "input_digest": digest,
        "output_asset_ids": [], "output_files": [], "error": None,
        "created_at": now_iso(),
    }

    from .delivery import DeliveryBlocked, build_delivery
    try:
        if stage == "publish_package":
            record.update(build_delivery(task, work))
        elif task.get("mode") == "mock":
            record["error"] = _mock_block(snapshot, work)
        else:
            result = _run_real_postprocess(task, work, snapshot)
            record.update(result)
    except (VideoBlocked, VideoRejected, DeliveryBlocked) as e:
        record["error"] = {"code": e.code, "message": e.message,
                           "detail": getattr(e, "detail", {})}
    except Exception as e:
        record["state"] = "failed"
        record["error"] = {"code": "POSTPROCESS_EXECUTION_FAILED",
                           "message": "后期执行异常，未登记任何成功产物",
            "detail": {"exception_type": type(e).__name__}}

    st.put("media_jobs", execution_id, record, {"work_id": task["work_id"]})
    return record


def _mock_block(snapshot: dict, work: dict) -> Dict[str, str]:
    """Mock 模式与 T07 前一致：Mock 无真实媒体与后期能力，不能生成交付包。"""
    batch = snapshot["script_batch"]
    audios = snapshot["audios"]
    selected = next((c for c in batch.get("candidates", [])
                     if c.get("id") == work.get("script_version_id")), None)
    if selected is None:
        return {"code": "DEPENDENCY_NOT_MET", "message": "代码后期需要当前已选稿件"}
    if not any(a.get("kind") == "full" and a.get("approved") and a.get("confirmed")
               and a.get("voice_asset_id") == work.get("voice_asset_id")
               for a in audios):
        return {"code": "DEPENDENCY_NOT_MET",
                "message": "代码后期需要已审且用户确认的当前全文音频"}
    return {"code": "MEDIA_NOT_READY",
            "message": "Mock 仅有模拟记录；缺少经审核的真实媒体与后期能力，不能生成交付包"}
