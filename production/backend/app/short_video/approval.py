"""Review native masters and separately proven subtitle versions before delivery."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from .. import delivery, jobs, storage as st
from ..contracts import now_iso
from ..video import ffmpeg
from . import media, service

GENERATION_MODE = "storyboard_native_audio"
DIMENSIONS = ("visual_quality", "audio_quality", "storyboard_adherence", "shot_continuity")
_confirm_lock = threading.RLock()


def require_video(video: dict, work: dict) -> dict:
    graph = service.get_graph(video.get("graph_id") or "", work.get("id"))
    service.assert_current(graph)
    output = graph.get("output") or {}
    job = jobs.get_job(graph.get("stitch_job_id") or "") or {}
    result = job.get("result") or {}
    capability = "storyboard_stitch"
    if output.get("subtitles_burned"):
        from .subtitle_jobs import verify_output
        source = {"video_id": result.get("video_id"), "output": result.get("output") or {}}
        subtitle_job = jobs.get_job(video.get("subtitle_job_id") or "") or {}
        subtitle_result = subtitle_job.get("result") or {}
        if (job.get("state") != "succeeded" or job.get("capability") != "storyboard_stitch"
            or job.get("work_id") != work.get("id") or subtitle_result.get("source_video_id") != source["video_id"]
            or subtitle_result.get("source_output") != source["output"]
            or (graph.get("subtitles") or {}).get("job_id") != subtitle_job.get("id")
            or subtitle_job.get("subtitle_args", {}).get("graph_id") != graph["id"]
            or subtitle_job.get("subtitle_args", {}).get("source_hash") != source["output"].get("file_hash")):
            raise service.StoryboardError("STORYBOARD_SUBTITLE_BINDING_INVALID", "字幕成片缺少已完成原拼接任务与当前字幕作业证明")
        verify_output(output, source)
        media.local_clip(source["output"])
        job, result, capability = subtitle_job, subtitle_result, "storyboard_subtitles"
    if (video.get("kind") != "storyboard_final" or video.get("generation_mode") != GENERATION_MODE
            or graph.get("state") != "succeeded" or graph.get("is_mock") or video.get("is_mock")
            or graph.get("video_id") != video.get("id") or video.get("work_id") != work.get("id")
            or job.get("state") != "succeeded" or result.get("video_id") != video.get("id")
            or job.get("work_id") != work.get("id") or job.get("capability") != capability
            or video.get("script_version_id") != graph["script_version_id"] or video.get("script_digest") != graph["script_digest"]
            or video.get("approval_state") in ("revoked", "rejected")
            or (graph.get("clip_approval") or {}).get("clip_digest") != service._clip_digest(graph)):
        raise service.StoryboardError("STORYBOARD_FINAL_BINDING_INVALID", "成片缺少当前分镜、已完成拼接作业与原片审核绑定")
    for field in ("storage_uri", "file_hash", "duration_ms", "has_audio_track", "has_video_track", "native_audio", "input_clip_hashes"):
        if video.get(field) != output.get(field) or (result.get("output") or {}).get(field) != output.get(field):
            raise service.StoryboardError("STORYBOARD_FINAL_BINDING_INVALID", "成片记录与 Worker 已完成结果不一致")
    for field in ("subtitles_burned", "subtitle_source", "subtitle_files", "subtitle_checks"):
        if video.get(field) != output.get(field) or (result.get("output") or {}).get(field) != output.get(field):
            raise service.StoryboardError("STORYBOARD_SUBTITLE_BINDING_INVALID", "字幕记录与 Worker 已完成结果不一致")
    for shot in graph["shots"]:
        image_job = jobs.get_job(shot.get("image_job_id") or "") or {}
        video_job = jobs.get_job(shot.get("video_job_id") or "") or {}
        if (shot.get("image_state") != "succeeded" or shot.get("video_state") != "succeeded"
                or image_job.get("state") != "succeeded" or video_job.get("state") != "succeeded"
                or image_job.get("work_id") != work["id"] or video_job.get("work_id") != work["id"]
                or image_job.get("capability") != "storyboard_image" or video_job.get("capability") != "storyboard_wan"
                or (image_job.get("result") or {}).get("image") != shot.get("image")
                or (video_job.get("result") or {}).get("clip") != shot.get("clip")
                or (video_job.get("result") or {}).get("remote_task_id") != shot.get("remote_task_id")):
            raise service.StoryboardError("STORYBOARD_PROVIDER_BINDING_INVALID", "镜头资产未绑定已完成的原供应商作业")
    service._verify_images(graph)
    media.verify_clips(graph)
    media.local_clip(output)
    return graph


def video_gate(video: dict) -> dict:
    reasons = []
    try:
        graph = require_video(video, st.get("works", video.get("work_id") or "") or {})
    except Exception as error:
        return {"ok": False, "outcome": "fail", "video_id": video.get("id"),
                "generation_mode": GENERATION_MODE, "reasons": [getattr(error, "message", "成片绑定或文件已变化")]}
    reviews = st.all_rows("video_reviews", "video_id=?", [video["id"]])
    review = max(reviews, key=lambda r: r.get("reviewed_at", ""), default={})
    passed = (review.get("rule_version") == "wan-storyboard-1" and review.get("outcome") == "pass"
              and str(review.get("reviewer") or "").startswith("user") and review.get("video_hash") == video["file_hash"]
              and review.get("clip_digest") == service._clip_digest(graph)
              and all(review.get("dimensions", {}).get(d) == "pass" for d in DIMENSIONS))
    if not passed:
        reasons.append("请观看整部短片并确认画面、原生声音、分镜执行与镜头衔接")
    return {"ok": passed, "outcome": "pass" if passed else "not_run", "video_id": video["id"],
            "generation_mode": GENERATION_MODE, "review_id": review.get("id"),
            "dimensions": review.get("dimensions", {}), "reasons": reasons,
            "audio_track_state": "pass", "sound_mode": "native_audio"}


def matches_execution(record: dict, work: dict) -> bool:
    inputs = record.get("input_assets") or {}
    try:
        video = st.get("videos", work.get("video_asset_id") or "") or {}
        graph = require_video(video, work)
        return (video.get("confirmed") is True and video.get("approval_state") == "approved"
                and video_gate(video)["ok"] and inputs.get("graph_id") == graph["id"]
                and inputs.get("video_id") == video["id"] and inputs.get("video_hash") == video["file_hash"]
                and inputs.get("script_version_id") == graph["script_version_id"]
                and inputs.get("script_digest") == graph["script_digest"]
                and inputs.get("clip_digest") == service._clip_digest(graph)
                and bool(inputs.get("subtitles_burned")) == bool(video.get("subtitles_burned"))
                and inputs.get("subtitle_job_id") == video.get("subtitle_job_id")
                and inputs.get("sound_mode") == "native_audio")
    except Exception:
        return False


def package_info(record: dict, work: dict, info: dict) -> dict:
    graph = service.get_graph(record["input_assets"]["graph_id"])
    captioned = (graph.get("output") or {}).get("subtitles_burned") is True
    return {**info, "generation_mode": GENERATION_MODE, "sound_mode": "native_audio", "subtitles": "burned" if captioned else "not_generated",
            "recognized_dialogue": (graph.get("output") or {}).get("subtitle_checks", {}).get("transcript") if captioned else None,
            "voice_asset_id": None, "identity_id": None, "graph_id": graph["id"],
            "storyboard": [{k: s[k] for k in ("shot_index", "visual", "dialogue", "sound_effects", "duration_seconds")}
                           for s in graph["shots"]],
            "note": "按分镜拼接 Wan 原生声画；字幕来自实际对白识别和对齐，原声音轨保留。" if captioned else "按分镜拼接 Wan 原生声画；边界短音频淡入淡出，无外部配音、无自动字幕。"}


def _cover(graph: dict, video: dict) -> Path:
    path = media.local_clip(video)
    cover = media.artifact_dir(graph) / ("storyboard-cover-" + video["id"] + ".jpg")
    if cover.is_symlink():
        raise service.StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "封面路径越界")
    if not cover.exists():
        cp = ffmpeg.run_ffmpeg(["-v", "error", "-protocol_whitelist", "file", "-i", str(path),
                               "-map", "0:v:0", "-frames:v", "1", "-q:v", "2", "-update", "1", str(cover)], timeout=60)
        if cp.returncode or not cover.is_file() or not cover.stat().st_size:
            raise service.StoryboardError("STORYBOARD_COVER_FAILED", "成片已保留，但封面尚未成功提取")
    return cover


def confirm_and_deliver(graph_id: str, *, actor_id: str, review_ack: bool, video_id: str, output_digest: str) -> dict:
    if not str(actor_id).startswith("user") or review_ack is not True:
        raise service.StoryboardError("STORYBOARD_FINAL_REVIEW_REQUIRED", "请由用户观看并确认整部短片")
    with _confirm_lock:
        graph = service.get_graph(graph_id)
        if (graph.get("subtitles") or {}).get("state") in ("queued", "running", "unknown"):
            raise service.StoryboardError("STORYBOARD_SUBTITLE_IN_PROGRESS", "字幕尚未完成，请等待并观看带字幕新成片后确认")
        work = st.get("works", graph["work_id"]) or {}
        video = st.get("videos", video_id) or {}
        require_video(video, work)
        if graph.get("video_id") != video_id or video.get("file_hash") != output_digest:
            raise service.StoryboardError("STORYBOARD_FINAL_REVIEW_STALE", "确认的成片版本已变化，请重新观看")
        if video.get("confirmed") is True and video.get("approval_state") == "approved" and video_gate(video)["ok"]:
            package = delivery.current_package(work["id"])
            if package:
                return {"ok": True, "graph": service.project(graph), "video": video, "package": package, "reused": True}
        cover = _cover(graph, video)
        master = media.local_clip(video)
        runtime = delivery.RUNTIME_ROOT.resolve()
        if not master.is_relative_to(runtime) or not cover.is_relative_to(runtime):
            raise service.StoryboardError("STORYBOARD_DELIVERY_PATH_INVALID", "交付产物不在当前运行目录内")
        inputs = {"generation_mode": GENERATION_MODE, "sound_mode": "native_audio", "graph_id": graph_id,
                  "video_id": video_id, "video_hash": video["file_hash"], "script_version_id": graph["script_version_id"],
                  "script_digest": graph["script_digest"], "clip_digest": service._clip_digest(graph),
                  "subtitles_burned": bool(video.get("subtitles_burned")), "subtitle_job_id": video.get("subtitle_job_id")}
        execution_id = "storyboard_delivery_" + video_id
        files = [{"kind": kind, "storage_uri": path.relative_to(runtime).as_posix(),
                  "sha256": delivery.digest_file(path), "bytes": path.stat().st_size}
                 for kind, path in (("master", master), ("cover", cover))]
        for item in video.get("subtitle_files", []):
            path = media.local_clip(item)
            if not path.is_relative_to(runtime):
                raise service.StoryboardError("STORYBOARD_DELIVERY_PATH_INVALID", "字幕产物不在当前运行目录内")
            files.append({"kind": item["kind"], "storage_uri": path.relative_to(runtime).as_posix(),
                          "sha256": delivery.digest_file(path), "bytes": path.stat().st_size})
        execution = {"id": execution_id, "work_id": work["id"], "task_id": graph["task_id"],
                     "stage": "postprocess", "executor_kind": "worker", "worker_kind": "wan_storyboard_delivery",
                     "state": "succeeded", "processing_outcome": "native_storyboard_stitched", "simulated": False,
                     "input_assets": inputs, "input_digest": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
                     "output_files": files, "verify_log": graph["output"],
                     "subtitle_checks": video.get("subtitle_checks") or {"outcome": "not_generated", "reason": "尚未生成原生声轨字幕"},
                     "created_at": now_iso()}
        with st.transaction() as conn:
            current = st.get("works", work["id"]) or {}
            if (service.get_graph(graph_id).get("subtitles") or {}).get("state") in ("queued", "running", "unknown"):
                raise service.StoryboardError("STORYBOARD_SUBTITLE_IN_PROGRESS", "字幕尚未完成，请稍后重新观看确认")
            require_video(st.get("videos", video_id) or {}, current)
            task = st.get("tasks", graph["task_id"]) or {}
            if task.get("mode") == "mock" or task.get("state") in (None, "cancelled", "failed", "blocked"):
                raise service.StoryboardError("TASK_NOT_READY", "当前制作任务未就绪")
            review = {"id": st.new_id("vrev"), "video_id": video_id, "work_id": work["id"], "outcome": "pass",
                      "reviewer": actor_id, "video_hash": video["file_hash"], "clip_digest": service._clip_digest(graph),
                      "dimensions": {d: "pass" for d in DIMENSIONS}, "rule_version": "wan-storyboard-1",
                      "references": {d: {"asset_id": video_id, "start_ms": 0, "end_ms": video["duration_ms"]} for d in DIMENSIONS},
                      "reviewed_at": now_iso()}
            for old in st.all_rows("videos", "work_id=?", [work["id"]]):
                if old["id"] != video_id and old.get("confirmed"):
                    old.update(confirmed=False, approval_state="revoked", revoked_at=now_iso(), revoked_reason="被新确认成片替代")
                    st.insert_row(conn, "videos", old["id"], old, {"work_id": work["id"]})
            video.update(confirmed=True, approval_state="approved", confirmed_by=actor_id, confirmed_at=now_iso())
            current["video_asset_id"] = video_id
            task.update(stage="video_review", state="waiting_selection", block_reason=None, last_error=None)
            st.insert_row(conn, "videos", video_id, video, {"work_id": work["id"]})
            st.insert_row(conn, "video_reviews", review["id"], review, {"video_id": video_id})
            st.insert_row(conn, "works", work["id"], current)
            st.insert_row(conn, "tasks", task["id"], task, {"work_id": work["id"]})
            st.insert_row(conn, "media_jobs", execution_id, execution, {"work_id": work["id"]})
        delivery.confirm_review(work["id"], actor_id=actor_id, review_ack=True, execution_id=execution_id)
        result = delivery.build_delivery(task, current)
        with st.transaction() as conn:
            delivery.download_path(result["package_id"])
            task = st.get("tasks", task["id"]) or {}
            task.update(stage="publish_package", state="succeeded", pending_selection_ids=[],
                        stage_rows=st.stage_rows("publish_package", "succeeded"))
            st.insert_row(conn, "tasks", task["id"], task, {"work_id": work["id"]})
        return {"ok": True, "graph": service.project(service.get_graph(graph_id)), "video": video,
                "package": delivery.public_package(st.get("packages", result["package_id"]))}
