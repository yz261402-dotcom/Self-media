"""Private image receipts and recovery of already returned provider results."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .. import jobs, storage as st
from ..avatar import person_image
from ..contracts import now_iso


def _receipt_path(graph: dict, shot: dict) -> Path:
    from . import service
    return service._image_file(graph, shot).with_suffix(".receipt.json")


def save_receipt(graph: dict, shot: dict, job_id: str, request: dict, response: dict) -> None:
    from .service import StoryboardError
    body = response.get("body") or {}
    items = body.get("data") or []
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return
    item = {k: items[0][k] for k in ("b64_json", "url") if items[0].get(k)}
    if not item:
        return
    # Persist only the fields needed to recover; no request headers or credentials.
    original = {"http_status": 200, "body": {"data": [item],
        **{k: body[k] for k in ("id", "created", "usage") if k in body}}}
    document = {"job_id": job_id, "graph_id": graph["id"], "shot_index": shot["shot_index"],
                "request": request, "response": original}
    data = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path = _receipt_path(graph, shot)
    if path.is_symlink():
        raise StoryboardError("STORYBOARD_IMAGE_RECEIPT_INVALID", "图片回执路径异常，请核对原作业")
    if path.exists():
        if path.read_bytes() != data:
            raise StoryboardError("STORYBOARD_IMAGE_RECEIPT_INVALID", "原图片回执已变化，停止覆盖")
    else:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    receipt = {"storage_uri": path.relative_to(Path(person_image.workspace_root()).resolve()).as_posix(),
               "file_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
               "request_digest": person_image.request_digest(request),
               "provider_response_id": body.get("id") or body.get("created"),
               "saved_at": now_iso()}
    with st.transaction() as conn:
        job = st.get("provider_jobs", job_id) or {}
        if job.get("state") != "leased" or job.get("work_id") != graph["work_id"]:
            raise StoryboardError("STORYBOARD_IMAGE_JOB_CHANGED", "原图片作业状态已变化，请核对")
        job["image_receipt"] = receipt
        job["updated_at"] = now_iso()
        st.insert_row(conn, "provider_jobs", job_id, job, {"work_id": graph["work_id"]})


def error_view(error: Exception, job_id: str) -> dict:
    """Use known error fields, never arbitrary exception text or remote response bodies."""
    from .service import StoryboardError
    known = isinstance(error, (person_image.PersonImageBlocked, person_image.PersonImageRejected, StoryboardError))
    code = error.code if known else "STORYBOARD_IMAGE_UNKNOWN"
    detail = (getattr(error, "detail", None) or {}) if known else {}
    stage = detail.get("stage")
    phase = "图片结果下载" if stage == "download" else "图片生成请求" if stage == "generation" else "图片请求或结果回收"
    reason = {
        "IMAGE_PROVIDER_UNREACHABLE": phase + "连接异常",
        "IMAGE_PROVIDER_HTTP_ERROR": phase + "返回 HTTP 错误",
        "IMAGE_PROVIDER_NOT_CONFIGURED": "图片服务未配置",
        "IMAGE_PROVIDER_NO_IMAGE": "图片服务没有返回图片",
        "IMAGE_PROVIDER_IMAGE_INVALID": "返回图片数据无效",
        "IMAGE_PROVIDER_RESPONSE_INVALID": "图片服务响应格式无效",
        "IMAGE_PROVIDER_EMPTY_IMAGE": "返回图片为空",
    }.get(code, "图片请求或结果保存异常")
    kind = detail.get("exception_type") or type(error.__cause__ or error).__name__
    if isinstance(kind, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", kind):
        reason += f"（{kind}）"
    status = detail.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        reason += f"，HTTP {status}"
    return {"code": code, "message": f"{reason}。作业号：{job_id}；结果待核对，未自动重发。"}


def record_error(job_id: str, worker: str, error: Exception) -> dict:
    view = error_view(error, job_id)
    jobs.mark_unknown(job_id, worker, view["message"])
    with st.transaction() as conn:
        job = st.get("provider_jobs", job_id)
        job["error"] = view
        st.insert_row(conn, "provider_jobs", job_id, job, {"work_id": job["work_id"]})
    return view


def project_shot(shot: dict) -> None:
    if not shot.get("image_job_id"):
        shot["image_recovery_available"] = False
        return
    job = jobs.get_job(shot["image_job_id"]) or {}
    from .image_regeneration import sender_ended
    if sender_ended(shot, job):
        shot["image_state"] = "unknown"
        shot["error"] = {"code": "STORYBOARD_IMAGE_SEND_INTERRUPTED", "message":
            f"上次图片请求未完成镜头回写或已超过执行时限，结果待核对。作业号：{shot['image_job_id']}；可回收已有图片，或确认追加费用后重新生成。"}
    shot["image_recovery_available"] = bool(job.get("image_receipt")) and shot.get("image_state") in ("unknown", "sending")
    if shot.get("image_state") == "unknown" and (shot.get("error") or {}).get("message") == "图片生成结果未知，保留作业记录，请先核对":
        error = job.get("error")
        if not error:
            reason = "图片服务连接异常" if shot["error"].get("code") == "IMAGE_PROVIDER_UNREACHABLE" else "图片生成或回收异常"
            error = {"code": shot["error"]["code"], "message":
                f"{reason}。作业号：{shot['image_job_id']}；旧记录未保存具体原因和图片回执，请在供应商后台核对原请求。"}
        shot["error"] = error


def recover(graph_id: str, shot_index: int) -> dict:
    from . import service
    with service._paid_claim(graph_id):
        graph = service.get_graph(graph_id)
        service.assert_current(graph)
        shot = next((s for s in graph["shots"] if s["shot_index"] == shot_index), None)
        if shot is None:
            raise service.StoryboardError("STORYBOARD_SHOT_NOT_FOUND", "镜头不存在", 404)
        job = jobs.get_job(shot.get("image_job_id") or "") or {}
        if (job.get("state") not in ("unknown", "leased", "succeeded") or job.get("work_id") != graph["work_id"]
                or job.get("capability") != "storyboard_image" or job.get("provider") != person_image.PROVIDER
                or job.get("model") != graph["image_model"]
                or job.get("args_digest") != jobs.args_digest(service._image_args(graph, shot))):
            raise service.StoryboardError("STORYBOARD_IMAGE_JOB_CHANGED", "镜头缺少可回收的原图片作业")
        already_completed = job["state"] == "succeeded"
        if already_completed and service._restore_completed_image(graph, shot, job):
            return service.project(service.get_graph(graph_id))
        receipt = job.get("image_receipt") or {}
        if not receipt:
            raise service.StoryboardError("STORYBOARD_IMAGE_RECEIPT_MISSING", "原请求没有保存图片回执，请在供应商后台核对结果，或确认追加费用后重新生成")
        path = _receipt_path(graph, shot)
        expected_request = person_image.build_request(prompt=shot["image_prompt"], size=graph["image_size"],
            model=graph["image_model"], reference_sha256=(graph.get("identity_reference") or {}).get("asset_hash", ""),
            reference_uri=(graph.get("identity_reference") or {}).get("storage_uri", ""))
        try:
            expected_uri = path.relative_to(Path(person_image.workspace_root()).resolve()).as_posix()
            if receipt.get("storage_uri") != expected_uri or path.is_symlink():
                raise ValueError()
            data = path.read_bytes()
            document = json.loads(data)
            if ("sha256:" + hashlib.sha256(data).hexdigest() != receipt.get("file_hash")
                    or document.get("job_id") != job["id"] or document.get("graph_id") != graph_id
                    or document.get("shot_index") != shot_index or document.get("request") != expected_request
                    or person_image.request_digest(expected_request) != receipt.get("request_digest")):
                raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError):
            raise service.StoryboardError("STORYBOARD_IMAGE_RECEIPT_INVALID", "原图片回执缺失、变化或不属于当前镜头，请核对原作业") from None
        try:
            produced = person_image.parse_generation_response(expected_request, document["response"])
            if already_completed:
                original_image = (job.get("result") or {}).get("image") or {}
                if (original_image.get("asset_hash") != "sha256:" + hashlib.sha256(produced["image_bytes"]).hexdigest()
                        or original_image.get("request_digest") != produced["request_digest"]):
                    raise service.StoryboardError("STORYBOARD_ARTIFACT_CONFLICT", "原回执图片与已完成产物不一致，停止回收")
            image = service._save_image(graph, shot, produced)
            if already_completed:
                if any(image.get(k) != original_image.get(k) for k in ("asset_hash", "storage_uri", "width", "height", "identity_reference")):
                    raise service.StoryboardError("STORYBOARD_ARTIFACT_CONFLICT", "原回执与已完成图片来源不一致，请核对")
                image = original_image
        except Exception as error:
            view = error_view(error, job["id"]) if already_completed else record_error(job["id"], "storyboard:recover", error)
            return service.project(service._update_shot(graph_id, shot_index, expected_image_job_id=job["id"],
                                                       image_state="unknown", error=view))
        if already_completed:
            return service.project(service._update_shot(graph_id, shot_index, expected_image_job_id=job["id"],
                                                       image_state="succeeded", image=image, error=None))
        # Store result and reconciliation in one transaction; retain cost settlement.
        with st.transaction() as conn:
            current = st.get("provider_jobs", job["id"])
            if current.get("state") not in ("unknown", "leased"):
                raise service.StoryboardError("STORYBOARD_IMAGE_JOB_CHANGED", "回收期间原作业状态已变化，请核对")
            result = {"image": image}
            current.update(state="succeeded", result=result, result_digest=jobs.args_digest(result), lease=None,
                settlement_required=bool(current.get("billable")), updated_at=now_iso(),
                reconciliation={"at": now_iso(), "evidence": {"source": "original_image_receipt", "file_hash": receipt["file_hash"]}})
            st.insert_row(conn, "provider_jobs", job["id"], current, {"work_id": graph["work_id"]})
        return service.project(service._update_shot(graph_id, shot_index, expected_image_job_id=job["id"],
                                                   image_state="succeeded", image=image, error=None))
