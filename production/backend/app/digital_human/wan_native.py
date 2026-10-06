"""Wan 图片提示词原生声画：不可变原片、人工确认、原声本地交付。

此路径不产生 TTS，不把提示词作为字幕，不混音或重新编码供应商原片。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from fractions import Fraction
from pathlib import Path

from .. import storage as st
from ..contracts import now_iso
from ..tts import audio as tts_audio
from ..video import ffmpeg
from .errors import TalkingHeadBlocked, TalkingHeadRejected
from .wan_audio import _artifact_path, _hash, _probe, _store_immutable, _stream_seconds

GENERATION_MODE = "image_prompt_native_audio"
SOUND_MODE = "native_audio"
DIMENSIONS = ("visual_quality", "audio_quality", "prompt_adherence")
_lock = threading.RLock()


def resolve_source(work: dict) -> dict:
    from .wan_inputs import resolve_source as resolve
    return resolve(work)


def is_native(video: dict) -> bool:
    return video.get("generation_mode") == GENERATION_MODE


def require_run(run: dict) -> tuple[dict, dict]:
    params = (run or {}).get("provider_params") or {}
    snapshot = params.get("input_snapshot") or {}
    if (not run or run.get("state") in ("cancelled", "failed") or run.get("is_mock") or run.get("route") != "WAN_3_0"
            or run.get("provider") != "dashscope_wan" or not run.get("remote_job_id")
            or params.get("generation_mode") != GENERATION_MODE
            or params.get("sound_mode") != SOUND_MODE
            or snapshot.get("work_id") != run.get("work_id")
            or not snapshot.get("source_asset_id") or not snapshot.get("source_asset_hash")
            or snapshot.get("audio_asset_id") or snapshot.get("audio_hash")):
        raise TalkingHeadRejected("WAN_NATIVE_SNAPSHOT_INVALID", "原生声画必须绑定真实 Wan 任务与图片快照，不能继承 TTS 输入。")
    return params, snapshot


def validate_registration(*, work_id: str, route: str, provider: str, provider_run_id: str | None,
                          source_asset_id: str | None, source_asset_hash: str | None,
                          identity_id: str | None, variant_id: str | None,
                          audio_asset_id: str | None, has_audio_track: bool, is_mock: bool) -> None:
    run = st.get("provider_runs", provider_run_id or "") or {}
    _, snapshot = require_run(run)
    if (is_mock or route != "WAN_3_0" or provider != "dashscope_wan" or not has_audio_track
            or run.get("work_id") != work_id or audio_asset_id
            or snapshot.get("source_asset_id") != source_asset_id
            or snapshot.get("source_asset_hash") != source_asset_hash
            or snapshot.get("identity_id") != identity_id or snapshot.get("variant_id") != variant_id):
        raise TalkingHeadRejected("WAN_NATIVE_BINDING_INVALID", "原生视频登记与供应商任务的图片、声音模式不一致。")


def _raw_path(run: dict) -> tuple[Path, Path]:
    rid = run.get("id")
    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", rid):
        raise TalkingHeadBlocked("WAN_RUN_PATH_INVALID", "Wan 运行标识不能作为安全产物路径。")
    runtime = Path(tts_audio.runtime_root()).resolve()
    base, directory = runtime / "wan3", runtime / "wan3" / rid
    if base.is_symlink() or directory.is_symlink() or not directory.resolve().is_relative_to(runtime):
        raise TalkingHeadBlocked("WAN_RUN_PATH_INVALID", "Wan 产物目录越界，停止写入。")
    directory.mkdir(parents=True, exist_ok=True)
    return runtime, _artifact_path(directory, "raw.mp4")


def prepare_original(run: dict, downloaded: dict) -> dict:
    """保留下载字节并实际探测/完整解码；不登记任何人工审核结论。"""
    params, _ = require_run(run)
    runtime, raw = _raw_path(run)
    data = downloaded.get("data")
    if not isinstance(data, bytes) or not data:
        raise TalkingHeadBlocked("WAN_RAW_EMPTY", "Wan 原片下载内容为空。")
    _store_immutable(raw, data)
    _store_immutable(raw.with_suffix(".sha256"), (_hash(data) + "\n").encode("ascii"))
    info = _probe(raw)
    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    audio = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    if not video:
        raise TalkingHeadBlocked("WAN_OUTPUT_VIDEO_MISSING", "Wan 原片没有视频轨；下载文件已保留。")
    if not audio:
        raise TalkingHeadBlocked("WAN_OUTPUT_AUDIO_MISSING", "Wan 原片没有原生音轨；下载文件已保留。")
    video_s, audio_s = _stream_seconds(video), _stream_seconds(audio)
    seconds = params.get("generated_seconds")
    if (isinstance(seconds, bool) or not isinstance(seconds, int) or not 2 <= seconds <= 30
            or abs(video_s - seconds) > .25 or abs(audio_s - seconds) > .25
            or audio_s > video_s + .25):
        raise TalkingHeadBlocked("WAN_NATIVE_DURATION_MISMATCH", "原片实测画面时长与已确认的 2–30 秒生成参数不符。")
    cp = ffmpeg.run_ffmpeg(["-v", "error", "-protocol_whitelist", "file", "-i", str(raw),
                           "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], timeout=120)
    if cp.returncode or cp.stderr.strip():
        raise TalkingHeadBlocked("WAN_NATIVE_DECODE_FAILED", "Wan 原片无法完整解码；文件已保留，不能进入交付。")
    try:
        fps = float(Fraction(video.get("avg_frame_rate") or "0"))
    except (ValueError, ZeroDivisionError):
        fps = None
    probe = {"container": "mp4", "has_video_track": True, "has_audio_track": True,
             "duration_ms": round(max(video_s, audio_s) * 1000), "video_duration_ms": round(video_s * 1000),
             "audio_duration_ms": round(audio_s * 1000), "width": video.get("width"),
             "height": video.get("height"), "fps": fps, "file_hash": _hash(data), "bytes": len(data),
             "probe_source": "ffprobe_and_full_decode", "truncated": False}
    evidence = {"generation_mode": GENERATION_MODE, "sound_mode": SOUND_MODE,
                "provider_run_id": run["id"], "remote_job_id": run["remote_job_id"],
                "raw_storage_uri": raw.relative_to(runtime).as_posix(), "raw_file_hash": _hash(data),
                "bytes_preserved": True, "audio_replaced": False, "probe": probe}
    _store_immutable(raw.with_name("native_audio.json"),
                     json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
    return {**downloaded, "data": data, "probe": probe, "native_audio": evidence}


def matches_source(run: dict, work: dict) -> bool:
    try:
        _, snapshot = require_run(run)
        source = resolve_source(work)
        return (work.get("id") == run.get("work_id") and work.get("route") == "WAN_3_0"
                and source.get("asset_id") == snapshot.get("source_asset_id")
                and source.get("asset_hash") == snapshot.get("source_asset_hash")
                and source.get("identity_id") == snapshot.get("identity_id")
                and source.get("variant_id") == snapshot.get("variant_id"))
    except (TalkingHeadBlocked, TalkingHeadRejected, OSError, ValueError):
        return False


def register_original(run_id: str, downloaded: dict) -> dict:
    from . import submission as sub, videos
    with _lock:
        run = sub.get_run(run_id) or {}
        _, snapshot = require_run(run)
        # 也允许直接调用登记接口；不能信任调用者传来的 probe/哈希。
        prepared = prepare_original(run, downloaded)
        probe, evidence = prepared["probe"], prepared["native_audio"]
        work = st.get("works", run["work_id"]) or {}
        existing = next((v for v in videos.list_videos(run["work_id"], "original")
                         if v.get("provider_run_id") == run_id and is_native(v)), None)
        if existing:
            if existing.get("file_hash") != probe["file_hash"]:
                raise TalkingHeadBlocked("WAN_ARTIFACT_CONFLICT", "同一原生运行返回不同原片；保留原始证据。")
            video = existing
        else:
            video = videos.register_video(
                work_id=run["work_id"], kind="original", route="WAN_3_0", provider="dashscope_wan",
                model=run.get("model") or "wan3.0-video", duration_ms=probe["duration_ms"],
                file_hash=probe["file_hash"], storage_uri=evidence["raw_storage_uri"],
                identity_id=snapshot.get("identity_id"), variant_id=snapshot.get("variant_id"),
                source_asset_id=snapshot["source_asset_id"], source_asset_hash=snapshot["source_asset_hash"],
                provider_run_id=run_id, cost_reservation_id=run.get("cost_reservation_id"),
                has_video_track=True, has_audio_track=True, width=probe["width"], height=probe["height"],
                    fps=probe.get("fps"), generation_mode=GENERATION_MODE,
                note="Wan 原生声画，供应商原片字节完整保留；未替换配音、未生成字幕，待用户观看确认。")
            video.update(sound_mode=SOUND_MODE, native_audio=evidence)
            st.put("videos", video["id"], video, {"work_id": run["work_id"]})
        if not matches_source(run, work) and video.get("approval_state") != "revoked":
            video = videos.revoke(video["id"], "原生任务返回时图片或路线已变化，仅保留历史原片。")
        sub._complete_result_job(run, video)
        updated = sub._update_run(run_id, state="succeeded", result_asset_ids=[video["id"]])
        return {"run": updated, "state": "succeeded", "video": video, "probe": probe, "reused": bool(existing)}


def require_current_video(video: dict, work: dict) -> dict:
    if (not is_native(video) or video.get("work_id") != work.get("id") or video.get("kind") != "original"
            or video.get("is_mock") or video.get("approval_state") in ("rejected", "revoked")):
        raise TalkingHeadRejected("WAN_NATIVE_VIDEO_INVALID", "请选择本作品当前可用的真实原生有声原片。")
    run = st.get("provider_runs", video.get("provider_run_id") or "") or {}
    if run.get("state") != "succeeded" or video.get("id") not in (run.get("result_asset_ids") or []):
        raise TalkingHeadRejected("WAN_NATIVE_RESULT_UNBOUND", "原片尚未绑定到已成功回收的供应商任务结果。")
    validate_registration(work_id=work["id"], route=video.get("route"), provider=video.get("provider"),
                          provider_run_id=run.get("id"), source_asset_id=video.get("source_asset_id"),
                          source_asset_hash=video.get("source_asset_hash"), identity_id=video.get("identity_id"),
                          variant_id=video.get("variant_id"), audio_asset_id=video.get("audio_asset_id"),
                          has_audio_track=video.get("has_audio_track") is True, is_mock=bool(video.get("is_mock")))
    if not matches_source(run, work):
        raise TalkingHeadRejected("WAN_NATIVE_INPUT_CHANGED", "原片对应的图片或路线已变化，请检查当前作品输入。")
    runtime, raw = _raw_path(run)
    evidence = video.get("native_audio") or {}
    if (video.get("storage_uri") != raw.relative_to(runtime).as_posix()
            or evidence.get("raw_file_hash") != video.get("file_hash")
            or not evidence.get("bytes_preserved") or evidence.get("audio_replaced") is not False
            or not raw.is_file() or _hash(raw.read_bytes()) != video.get("file_hash")):
        raise TalkingHeadRejected("WAN_NATIVE_FILE_CHANGED", "原片文件或原声音轨来源证据已变化，不能继续使用旧审核。")
    return run


def is_native_video(video: dict) -> bool:
    if not is_native(video):
        return False
    try:
        validate_registration(work_id=video.get("work_id"), route=video.get("route"), provider=video.get("provider"),
                              provider_run_id=video.get("provider_run_id"), source_asset_id=video.get("source_asset_id"),
                              source_asset_hash=video.get("source_asset_hash"), identity_id=video.get("identity_id"),
                              variant_id=video.get("variant_id"), audio_asset_id=video.get("audio_asset_id"),
                              has_audio_track=video.get("has_audio_track") is True, is_mock=bool(video.get("is_mock")))
        return True
    except (TalkingHeadBlocked, TalkingHeadRejected):
        return False


def matches_current(video: dict, work: dict) -> bool:
    try:
        require_current_video(video, work)
        return True
    except (TalkingHeadBlocked, TalkingHeadRejected, OSError, ValueError):
        return False


def native_gate(video: dict) -> dict:
    reasons, failed = [], False
    try:
        require_current_video(video, st.get("works", video.get("work_id") or "") or {})
    except (TalkingHeadBlocked, TalkingHeadRejected, OSError) as error:
        reasons.append(getattr(error, "message", str(error)))
        failed = True
    rows = st.all_rows("video_reviews", "video_id=?", [video["id"]])
    review = max(rows, key=lambda row: row.get("reviewed_at", ""), default={})
    approved = (review.get("rule_version") == "wan-native-1" and review.get("outcome") == "pass"
                and str(review.get("reviewer") or "").startswith("user")
                and review.get("video_hash") == video.get("file_hash")
                and all(review.get("dimensions", {}).get(d) == "pass" for d in DIMENSIONS))
    if not approved:
        reasons.append("请观看原片并确认画面、原生声音与提示词效果。")
    outcome = "fail" if failed or (review and not approved) else "pass" if approved else "not_run"
    return {"ok": not reasons, "outcome": outcome, "video_id": video["id"], "route": video.get("route"),
            "generation_mode": GENERATION_MODE, "review_state": "pass" if approved else "not_run",
            "dimensions": review.get("dimensions", {}), "review_id": review.get("id"), "reasons": reasons,
            "audio_track_state": "pass" if not failed else "fail", "audio_track": {
                "checked": not failed, "outcome": "pass" if not failed else "fail", "method": "provider_original_bytes",
                "sound_mode": SOUND_MODE, "reason": "原生音轨随供应商原片完整保留；未与 TTS 比对。"}}


def confirm_and_deliver(work_id: str, video_id: str, *, actor_id: str, review_ack: bool) -> dict:
    """一次用户观看确认同时适用于字节相同的原片与成片；封面单独提取。"""
    from .. import delivery
    from . import videos
    if review_ack is not True or not str(actor_id).startswith("user"):
        raise TalkingHeadRejected("WAN_NATIVE_REVIEW_REQUIRED", "请先观看原片，由用户确认画面、声音与提示词效果。")
    with _lock:
        work = st.get("works", work_id) or {}
        video = videos.get_video(video_id) or {}
        run = require_current_video(video, work)
        task = st.get("tasks", work.get("current_task_id") or "") or {}
        if not task or task.get("work_id") != work_id or task.get("state") in ("cancelled", "failed", "blocked"):
            raise TalkingHeadRejected("TASK_NOT_READY", "当前制作任务不存在或已取消，请重新选择路线。")
        if work.get("video_asset_id") == video_id and native_gate(video)["ok"]:
            package = delivery.current_package(work_id)
            if package:
                return {"ok": True, "video": video, "package": package, "reused": True}
        runtime, raw = _raw_path(run)
        cover = _artifact_path(raw.parent, "native-cover.jpg")
        if not cover.exists():
            cp = ffmpeg.run_ffmpeg(["-v", "error", "-protocol_whitelist", "file", "-i", str(raw),
                                   "-map", "0:v:0", "-frames:v", "1", "-q:v", "2", "-update", "1", str(cover)], timeout=60)
            if cp.returncode or not cover.is_file() or not cover.stat().st_size:
                raise TalkingHeadBlocked("WAN_NATIVE_COVER_FAILED", "原片已保留，但封面提取失败，尚未生成交付包。")
        inputs = {"generation_mode": GENERATION_MODE, "sound_mode": SOUND_MODE, "video_id": video_id,
                  "video_hash": video["file_hash"], "provider_run_id": run["id"],
                  "source_asset_id": video["source_asset_id"], "source_asset_hash": video["source_asset_hash"],
                  "request_digest": run.get("request_digest")}
        outputs = [{"kind": kind, "storage_uri": path.relative_to(runtime).as_posix(),
                    "sha256": delivery.digest_file(path), "bytes": path.stat().st_size}
                   for kind, path in (("master", raw), ("cover", cover))]
        execution = {"id": "native_" + video_id, "work_id": work_id, "task_id": task["id"],
                     "stage": "postprocess", "executor_kind": "worker", "worker_kind": "wan_native_delivery",
                     "state": "succeeded", "processing_outcome": "original_preserved", "simulated": False,
                     "generation_mode": GENERATION_MODE, "input_assets": inputs,
                     "input_digest": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
                     "output_files": outputs, "verify_log": video["native_audio"],
                     "subtitle_checks": {"outcome": "not_generated", "reason": "原生声画交付不生成字幕，提示词不作为字幕。"},
                     "created_at": now_iso()}
        # 提取封面期间用户可能换图/换任务；当前值仍匹配才记录本次用户审核。
        with st.transaction() as conn:
            current = st.get("works", work_id) or {}
            current_task = st.get("tasks", task["id"]) or {}
            require_current_video(st.get("videos", video_id) or {}, current)
            if (current.get("current_task_id") != task["id"]
                    or current_task.get("state") in (None, "cancelled", "failed", "blocked")):
                raise TalkingHeadRejected("TASK_NOT_READY", "确认期间作品任务已变化，请刷新预览。")
            review = {"id": st.new_id("vrev"), "video_id": video_id, "work_id": work_id,
                      "reviewer": actor_id, "outcome": "pass", "video_hash": video["file_hash"],
                      "dimensions": {d: "pass" for d in DIMENSIONS}, "rule_version": "wan-native-1",
                      "references": {d: {"asset_id": video_id, "start_ms": 0, "end_ms": video["duration_ms"]} for d in DIMENSIONS},
                      "note": "用户观看原片后明确确认画面、声音与提示词效果；成片保持相同字节。", "reviewed_at": now_iso()}
            for old in videos.list_videos(work_id, "original"):
                if old["id"] != video_id and old.get("confirmed"):
                    old.update(confirmed=False, approval_state="revoked", revoked_at=now_iso(), revoked_reason="被新确认原片替代")
                    st.insert_row(conn, "videos", old["id"], old, {"work_id": work_id})
            video.update(confirmed=True, approval_state="approved", confirmed_by=actor_id, confirmed_at=now_iso(), frozen_at=now_iso())
            current["video_asset_id"] = video_id
            current_task.update(stage="video_review", state="waiting_selection", block_reason=None, last_error=None)
            st.insert_row(conn, "video_reviews", review["id"], review, {"video_id": video_id})
            st.insert_row(conn, "videos", video_id, video, {"work_id": work_id})
            st.insert_row(conn, "works", work_id, current)
            st.insert_row(conn, "tasks", task["id"], current_task, {"work_id": work_id})
            st.insert_row(conn, "media_jobs", execution["id"], execution, {"work_id": work_id})
        delivery.confirm_review(work_id, actor_id=actor_id, review_ack=True, execution_id=execution["id"])
        result = delivery.build_delivery(current_task, current)
        with st.transaction() as conn:
            delivery.download_path(result["package_id"])
            current_task = st.get("tasks", task["id"]) or {}
            current_task.update(stage="publish_package", state="succeeded", pending_selection_ids=[], block_reason=None, last_error=None,
                                stage_rows=st.stage_rows("publish_package", "succeeded"))
            st.insert_row(conn, "tasks", task["id"], current_task, {"work_id": work_id})
        return {"ok": True, "video": video, "package": delivery.public_package(st.get("packages", result["package_id"]))}
