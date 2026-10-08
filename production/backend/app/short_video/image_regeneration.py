"""User-approved replacement attempts; preserve unknown jobs and their charges."""
from __future__ import annotations

import copy
import re

from .. import jobs, storage as st
from ..avatar import person_image
from ..contracts import now_iso


def video_started(graph: dict) -> bool:
    if (graph.get("video_id") or graph.get("output") or graph.get("state") in
        ("stitch_queued", "stitching", "succeeded", "stitch_failed") or any(
            s.get("video_state") != "pending" or s.get("remote_task_id") or s.get("video_job_id") or s.get("clip")
            for s in graph["shots"])):
        return True
    # The video intent precedes its graph binding; a crash must not make its
    # already approved input images appear editable again.
    keys = [f"{graph['id']}:wan:{s['shot_index']}" for s in graph["shots"]]
    placeholders = ",".join("?" for _ in keys)
    return bool(st.conn().execute(f"SELECT job_id FROM provider_jobs_idem WHERE idem_key IN ({placeholders}) LIMIT 1", keys).fetchone())


def sender_ended(shot: dict, job: dict) -> bool:
    if shot.get("image_state") != "sending":
        return False
    if job.get("state") in ("succeeded", "unknown", "failed", "blocked"):
        return True
    lease = job.get("lease") or {}
    return job.get("state") == "leased" and bool(lease.get("expires_at")) and lease["expires_at"] <= jobs._now()


def available(graph: dict, shot: dict) -> bool:
    eligible = shot.get("image_state") in ("unknown", "failed", "blocked", "succeeded")
    if shot.get("image_state") == "sending":
        eligible = sender_ended(shot, jobs.get_job(shot.get("image_job_id") or "") or {})
    return bool(shot.get("image_job_id") and eligible and not video_started(graph))


def regenerate(graph_id: str, shot_index: int, *, actor_id: str, plan_digest: str,
               paid_ack: bool, duplicate_charge_ack: bool, expected_image_job_id: str,
               request_id: str, ceiling_fen: int, transport=None) -> dict:
    from . import service
    with service._paid_claim(graph_id):
        graph = service.get_graph(graph_id)
        service.assert_current(graph, paid=True)
        if (not isinstance(actor_id, str) or not actor_id.startswith("user") or paid_ack is not True
                or duplicate_charge_ack is not True or plan_digest != graph["plan_digest"]
                or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)
                or not isinstance(expected_image_job_id, str) or not expected_image_job_id
                or isinstance(ceiling_fen, bool) or not isinstance(ceiling_fen, int)
                or ceiling_fen != graph["image_ceiling_fen_per_shot"]):
            raise service.StoryboardError("STORYBOARD_IMAGE_RETRY_APPROVAL_REQUIRED",
                "请确认当前镜头、原作业及新增图片请求的费用；原请求可能已计费，重新生成可能再次计费")
        if (graph.get("image_approval") or {}).get("plan_digest") != graph["plan_digest"]:
            raise service.StoryboardError("STORYBOARD_IMAGE_APPROVAL_REQUIRED", "请先确认当前任务图的图片生成")
        shot = next((s for s in graph["shots"] if s["shot_index"] == shot_index), None)
        if shot is None:
            raise service.StoryboardError("STORYBOARD_SHOT_NOT_FOUND", "镜头不存在", 404)
        key = f"{graph_id}:image:{shot_index}:regenerate:{request_id}"
        found = st.conn().execute("SELECT job_id FROM provider_jobs_idem WHERE idem_key=?", [key]).fetchone()
        existing = jobs.get_job(found["job_id"]) if found else None
        approval = {"previous_job_id": expected_image_job_id, "plan_digest": plan_digest,
                    "request_id": request_id, "actor_id": actor_id, "ceiling_fen": ceiling_fen,
                    "paid_ack": True, "duplicate_charge_ack": True}
        if existing and existing.get("image_regeneration"):
            if any(existing["image_regeneration"].get(k) != v for k, v in approval.items()):
                raise service.StoryboardError("STORYBOARD_IMAGE_RETRY_CONFLICT", "本次请求号已用于另一份重新生成确认，请刷新镜头状态")
            if existing["state"] == "succeeded" and shot.get("image_job_id") == existing["id"]:
                if shot.get("image_state") != "succeeded":
                    service._restore_completed_image(graph, shot, existing)
                return service.project(service.get_graph(graph_id))
            # A queued intent has never acquired a send lease, so an explicit replay
            # can finish it after a crash. Every leased/unknown/terminal job only replays.
            if existing["state"] != "queued" or shot.get("image_job_id") != existing["id"]:
                return service.project(graph)
            if video_started(graph):
                raise service.StoryboardError("STORYBOARD_IMAGE_RETRY_VIDEO_STARTED", "视频制作已开始，不能重新生成镜头图片")
            _, reference_bytes = service._identity_reference(st.get("works", graph["work_id"]) or {})
            return service._generate_image_job(graph, shot, existing, reference_bytes, transport=transport)
        if video_started(graph):
            raise service.StoryboardError("STORYBOARD_IMAGE_RETRY_VIDEO_STARTED", "视频制作已开始，不能重新生成镜头图片")
        if shot.get("image_job_id") != expected_image_job_id:
            raise service.StoryboardError("STORYBOARD_IMAGE_ATTEMPT_CHANGED", "镜头已有新的图片请求，请刷新后再确认重新生成")
        if not available(graph, shot):
            raise service.StoryboardError("STORYBOARD_IMAGE_RETRY_NOT_READY", "镜头尚未生成或原请求仍在执行，请等待原请求返回")
        original = jobs.get_job(expected_image_job_id) or {}
        if (original.get("work_id") != graph["work_id"] or original.get("capability") != "storyboard_image"
                or (original.get("args_digest") != jobs.args_digest(service._image_args(graph, shot))
                    and not (shot.get("image_reuse") and service._verified_reusable_image(graph, shot)))):
            raise service.StoryboardError("STORYBOARD_IMAGE_ATTEMPT_CHANGED", "原图片作业与当前镜头不一致，请刷新")
        service._assert_no_other_unresolved_graphs(graph)
        _, reference_bytes = service._identity_reference(st.get("works", graph["work_id"]) or {})
        next_shot = copy.deepcopy(shot)
        next_shot["image_attempt"] = int(shot.get("image_attempt") or 0) + 1
        args = service._image_args(graph, next_shot)
        job = jobs.submit(work_id=graph["work_id"], capability="storyboard_image", provider=person_image.PROVIDER,
            model=graph["image_model"], args=args, idempotency_key=key, billable=True,
            amount_fen=ceiling_fen, namespace="wan-storyboard", note="用户确认追加费用，重新生成单个镜头图片")
        if job["args_digest"] != jobs.args_digest(args):
            raise service.StoryboardError("STORYBOARD_IMAGE_RETRY_CONFLICT", "本次请求号对应的图片尝试已变化，请刷新")
        if job["state"] == "blocked":
            raise service.StoryboardError(job["block_code"], job["block_detail"])
        # Bind the new attempt before any outbound request. The original job stays
        # untouched and its image, receipt and settlement remain available for audit.
        with st.transaction() as conn:
            current = service.get_graph(graph_id)
            current_shot = next(s for s in current["shots"] if s["shot_index"] == shot_index)
            if current_shot.get("image_job_id") != expected_image_job_id:
                raise service.StoryboardError("STORYBOARD_IMAGE_ATTEMPT_CHANGED", "镜头图片请求已变化，请刷新")
            history = {k: copy.deepcopy(current_shot.get(k)) for k in
                       ("image_job_id", "image_state", "image", "error")}
            for key in ("image_reuse", "image_reuse_history"):
                if key in current_shot:
                    history[key] = current_shot.pop(key)
            history.update(image_attempt=int(current_shot.get("image_attempt") or 0), superseded_at=now_iso())
            current_shot.setdefault("image_history", []).append(history)
            current_shot.update(image_attempt=next_shot["image_attempt"], image_job_id=job["id"], image=None,
                image_state="sending", error=None)
            current.update(video_approval=None, clip_approval=None, updated_at=now_iso())
            saved_job = st.get("provider_jobs", job["id"])
            saved_job["image_regeneration"] = dict(approval, approved_at=now_iso(), image_attempt=next_shot["image_attempt"])
            st.insert_row(conn, "provider_jobs", job["id"], saved_job, {"work_id": graph["work_id"]})
            st.insert_row(conn, "media_jobs", graph_id, current, {"work_id": graph["work_id"]})
        return service._generate_image_job(current, current_shot, job, reference_bytes, transport=transport)
