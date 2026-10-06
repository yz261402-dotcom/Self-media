"""Explicit single-shot Wan attempts, durable task receipts and conditional updates."""
from __future__ import annotations

import copy
import re

from .. import jobs, storage as st
from ..contracts import now_iso
from ..digital_human import wan_provider
from ..digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected

SEND_LEASE_SECONDS = 120


def frozen(graph: dict) -> bool:
    if (graph.get("video_id") or graph.get("output") or graph.get("package_id") or graph.get("stitch_job_id")
            or graph.get("state") in ("stitch_queued", "stitching", "stitch_failed", "succeeded")):
        return True
    prefix = graph["id"] + ":stitch:"
    return bool(st.conn().execute("SELECT job_id FROM provider_jobs_idem WHERE starts_with(idem_key, ?) LIMIT 1", [prefix]).fetchone())


def sender_ended(shot: dict, job: dict) -> bool:
    return shot.get("video_state") == "sending" and (job.get("state") in ("unknown", "failed", "blocked", "succeeded")
        or (job.get("state") == "leased" and (job.get("lease") or {}).get("expires_at", float("inf")) <= jobs._now()))


def available(graph: dict, shot: dict) -> bool:
    job = jobs.get_job(shot.get("video_job_id") or "") or {}
    if job.get("state") == "waiting_provider" and (job.get("wan_submission") or {}).get("remote_task_id"):
        return False
    eligible = shot.get("video_state") in ("failed", "blocked", "unknown", "succeeded") or sender_ended(shot, job)
    return bool(eligible and shot.get("video_job_id") and shot.get("image_state") == "succeeded" and not frozen(graph))


def project_shot(shot: dict) -> None:
    job = jobs.get_job(shot.get("video_job_id") or "") or {}
    receipt = job.get("wan_submission") or {}
    if shot.get("video_state") in ("sending", "unknown", "failed", "blocked") and receipt.get("remote_task_id") and job.get("state") == "waiting_provider":
        shot.update(video_state="waiting_provider", remote_task_id=receipt["remote_task_id"], endpoint=receipt["endpoint"], error=None)
    elif sender_ended(shot, job):
        if job.get("state") in ("failed", "blocked"):
            shot.update(video_state=job["state"], error=job.get("error") or shot.get("error"))
        elif job.get("state") != "succeeded":
            shot.update(video_state="unknown", error={"code": "STORYBOARD_WAN_SEND_INTERRUPTED",
                "message": "上次视频请求未完成回写或已超过执行时限，结果待核对；可查询原作业或确认追加费用后重新生成。"})


def args(graph: dict, shot: dict, endpoint: str) -> dict:
    from . import service
    result = {"graph_id": graph["id"], "shot_index": shot["shot_index"], "task_graph_digest": service._task_digest(graph),
              "source_image_hash": shot["image"]["asset_hash"], "prompt": shot["wan_prompt"],
              "duration_seconds": shot["duration_seconds"], "resolution": graph["resolution"], "endpoint": endpoint}
    if shot.get("video_attempt"):
        result["video_attempt"] = shot["video_attempt"]
    return result


def restore(graph: dict, shot: dict, job: dict) -> bool:
    from . import service, media
    job = jobs.get_job(job.get("id") or "") or job
    receipt = job.get("wan_submission") or {}
    result = job.get("result") or {}
    if job.get("state") == "succeeded" and result.get("clip"):
        try:
            media.local_clip(result["clip"])
        except (OSError, ValueError, service.StoryboardError):
            return False
        updated = service._update_shot(graph["id"], shot["shot_index"], expected_video_job_id=job["id"], expected_video_job_state="succeeded", video_state="succeeded",
            clip=result["clip"], remote_task_id=result.get("remote_task_id") or receipt.get("remote_task_id"), error=None)
        return next(s for s in updated["shots"] if s["shot_index"] == shot["shot_index"]).get("video_job_id") == job["id"]
    if job.get("state") == "waiting_provider" and receipt.get("remote_task_id"):
        updated = service._update_shot(graph["id"], shot["shot_index"], expected_video_job_id=job["id"], expected_video_job_state="waiting_provider", video_state="waiting_provider",
            remote_task_id=receipt["remote_task_id"], endpoint=receipt["endpoint"], error=None)
        return next(s for s in updated["shots"] if s["shot_index"] == shot["shot_index"]).get("video_job_id") == job["id"]
    return False


def send(graph: dict, shot: dict, job: dict, *, transport=None) -> dict:
    from . import service
    worker = "storyboard:wan:" + job["id"]
    try:
        lease = jobs.acquire_lease(job["id"], worker, ttl_seconds=SEND_LEASE_SECONDS, require_queued=True)
    except jobs.JobRejected as error:
        if error.code != "JOB_NOT_QUEUED":
            raise
        restore(graph, shot, jobs.get_job(job["id"]) or {})
        return service.project(service.get_graph(graph["id"]))
    endpoint = wan_provider.endpoint(shot.get("endpoint") or wan_provider.endpoint())
    current = service._update_shot(graph["id"], shot["shot_index"], expected_video_job_id=shot.get("video_job_id"),
        video_state="sending", video_job_id=job["id"], endpoint=endpoint)
    bound = next(s for s in current["shots"] if s["shot_index"] == shot["shot_index"])
    if bound.get("video_job_id") != job["id"] or lease["expires_at"] <= jobs._now():
        return service.project(current)
    # Input preparation can fail before the billable POST; retain the leased intent
    # conservatively, and allow a separately approved new attempt.
    try:
        source = service.prepare_native_media(shot["image"])
        service.assert_current(service.get_graph(graph["id"]), paid=True)
        service._verify_images(service.get_graph(graph["id"]))
        if lease["expires_at"] <= jobs._now() or next(s for s in service.get_graph(graph["id"])["shots"]
                if s["shot_index"] == shot["shot_index"]).get("video_job_id") != job["id"]:
            return service.project(service.get_graph(graph["id"]))
        request = wan_provider.build_native_submit_request(image_url=source["image_url"], prompt=shot["wan_prompt"],
            duration_seconds=shot["duration_seconds"], resolution=graph["resolution"],
            api_key=wan_provider.credentials(), base_url=endpoint)
        parsed = wan_provider.parse_submit_response((transport or wan_provider.urllib_transport)(request))
    except (TalkingHeadBlocked, TalkingHeadRejected) as error:
        if error.code in ("WAN_AUTH_FAILED", "WAN_QUOTA_EXHAUSTED", "WAN_INPUT_REJECTED"):
            view = {"code": error.code, "message": error.message}
            try:
                jobs.fail(job["id"], worker, view)
            except jobs.JobRejected:
                jobs.mark_unknown(job["id"], reason=error.message)
            return service.project(service._update_shot(graph["id"], shot["shot_index"], expected_video_job_id=job["id"],
                video_state="failed", error=view))
        parsed = {"state": "unknown"}
    except Exception:
        parsed = {"state": "unknown"}
    if parsed["state"] != "submitted":
        jobs.mark_unknown(job["id"], worker, "Wan 提交响应未知，保留原请求；仅在用户确认追加费用后重新生成")
        return service.project(service._update_shot(graph["id"], shot["shot_index"], expected_video_job_id=job["id"],
            video_state="unknown", error={"code": "STORYBOARD_WAN_UNKNOWN", "message": "视频提交结果未知，可查询原作业或确认追加费用后重新生成。"}))
    # The original provider job owns its receipt even when a newer attempt has
    # replaced it. Persist this before changing the active shot.
    with st.transaction() as conn:
        saved = jobs.get_job(job["id"])
        saved.update(wan_submission={"remote_task_id": parsed["remote_job_id"], "endpoint": endpoint, "accepted_at": now_iso()},
            state="waiting_provider", lease=None, settlement_required=bool(saved.get("billable")), updated_at=now_iso())
        st.insert_row(conn, "provider_jobs", job["id"], saved, {"work_id": graph["work_id"]})
    return service.project(service._update_shot(graph["id"], shot["shot_index"], expected_video_job_id=job["id"],
        video_state="waiting_provider", remote_task_id=parsed["remote_job_id"], endpoint=endpoint, error=None))


def regenerate(graph_id: str, shot_index: int, *, actor_id: str, paid_ack: bool, duplicate_charge_ack: bool,
               task_graph_digest: str, expected_video_job_id: str, request_id: str, ceiling_fen: int, transport=None) -> dict:
    from . import service
    with service._paid_claim(graph_id):
        graph = service.get_graph(graph_id)
        service.assert_current(graph, paid=True)
        shot = next((s for s in graph["shots"] if s["shot_index"] == shot_index), None)
        if shot is None:
            raise service.StoryboardError("STORYBOARD_SHOT_NOT_FOUND", "镜头不存在", 404)
        price = next(p["ceiling_fen"] for p in graph["estimate"]["shot_prices"] if p["shot_index"] == shot_index)
        if (not isinstance(actor_id, str) or not actor_id.startswith("user") or paid_ack is not True or duplicate_charge_ack is not True
                or task_graph_digest != service._task_digest(graph) or not isinstance(expected_video_job_id, str) or not expected_video_job_id
                or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)
                or isinstance(ceiling_fen, bool) or not isinstance(ceiling_fen, int) or ceiling_fen != price):
            raise service.StoryboardError("STORYBOARD_VIDEO_RETRY_APPROVAL_REQUIRED", "请确认当前镜头、原作业和新增视频费用；重新生成可能再次计费")
        if (graph.get("video_approval") or {}).get("task_graph_digest") != task_graph_digest:
            raise service.StoryboardError("STORYBOARD_VIDEO_APPROVAL_REQUIRED", "请先审阅镜头图片并确认视频生成费用")
        key = f"{graph_id}:wan:{shot_index}:regenerate:{request_id}"
        row = st.conn().execute("SELECT job_id FROM provider_jobs_idem WHERE idem_key=?", [key]).fetchone()
        previous = jobs.get_job(row["job_id"]) if row else None
        approval = {"previous_job_id": expected_video_job_id, "task_graph_digest": task_graph_digest,
            "request_id": request_id, "actor_id": actor_id, "ceiling_fen": ceiling_fen, "paid_ack": True, "duplicate_charge_ack": True}
        if previous and previous.get("video_regeneration"):
            if any(previous["video_regeneration"].get(k) != v for k, v in approval.items()):
                raise service.StoryboardError("STORYBOARD_VIDEO_RETRY_CONFLICT", "本次请求号已用于其他视频重生成确认，请刷新状态")
            if (shot.get("video_job_id") == previous["id"] and shot.get("video_state") in ("sending", "unknown")
                    and restore(graph, shot, previous)):
                return service.project(service.get_graph(graph_id))
            if previous["state"] != "queued" or shot.get("video_job_id") != previous["id"]:
                return service.project(graph)
            if frozen(graph):
                raise service.StoryboardError("STORYBOARD_VIDEO_RETRY_FROZEN", "拼接或交付已开始，不能重新生成镜头视频")
            return send(graph, shot, previous, transport=transport)
        if frozen(graph):
            raise service.StoryboardError("STORYBOARD_VIDEO_RETRY_FROZEN", "拼接或交付已开始，不能重新生成镜头视频")
        if shot.get("video_job_id") != expected_video_job_id:
            raise service.StoryboardError("STORYBOARD_VIDEO_ATTEMPT_CHANGED", "镜头已有新的请求，请刷新后再确认")
        if not available(graph, shot):
            raise service.StoryboardError("STORYBOARD_VIDEO_RETRY_NOT_READY", "原视频仍在生成或镜头图片未就绪，请先查询原作业")
        original = jobs.get_job(expected_video_job_id) or {}
        endpoint = wan_provider.endpoint(shot.get("endpoint") or wan_provider.endpoint())
        if (original.get("work_id") != graph["work_id"] or original.get("capability") != "storyboard_wan"
                or original.get("args_digest") != jobs.args_digest(args(graph, shot, endpoint))):
            raise service.StoryboardError("STORYBOARD_VIDEO_ATTEMPT_CHANGED", "原视频作业与当前镜头不一致")
        service._assert_no_other_unresolved_graphs(graph)
        service._verify_images(graph)
        wan_provider.credentials()
        next_shot = dict(shot, video_attempt=int(shot.get("video_attempt") or 0) + 1)
        job = jobs.submit(work_id=graph["work_id"], capability="storyboard_wan", provider=wan_provider.PROVIDER, model=wan_provider.MODEL,
            args=args(graph, next_shot, endpoint), idempotency_key=key, billable=True, amount_fen=price, namespace="wan-storyboard")
        if job["args_digest"] != jobs.args_digest(args(graph, next_shot, endpoint)):
            raise service.StoryboardError("STORYBOARD_VIDEO_RETRY_CONFLICT", "本次请求号对应的视频尝试已变化")
        if job["state"] == "blocked":
            raise service.StoryboardError(job["block_code"], job["block_detail"])
        with st.transaction() as conn:
            current = service.get_graph(graph_id)
            active = next(s for s in current["shots"] if s["shot_index"] == shot_index)
            if active.get("video_job_id") != expected_video_job_id or frozen(current):
                raise service.StoryboardError("STORYBOARD_VIDEO_ATTEMPT_CHANGED", "提交期间原镜头或拼接状态已变化")
            history = {k: copy.deepcopy(active.get(k)) for k in ("video_job_id", "video_state", "remote_task_id", "endpoint", "clip", "error")}
            history.update(video_attempt=int(active.get("video_attempt") or 0), superseded_at=now_iso())
            active.setdefault("video_history", []).append(history)
            active.update(video_attempt=next_shot["video_attempt"], video_job_id=job["id"], video_state="sending",
                remote_task_id=None, clip=None, endpoint=endpoint, error=None)
            current.update(clip_approval=None, updated_at=now_iso())
            saved = jobs.get_job(job["id"])
            saved["video_regeneration"] = dict(approval, video_attempt=next_shot["video_attempt"], approved_at=now_iso())
            st.insert_row(conn, "provider_jobs", job["id"], saved, {"work_id": graph["work_id"]})
            st.insert_row(conn, "media_jobs", graph_id, current, {"work_id": graph["work_id"]})
        return send(current, active, job, transport=transport)
