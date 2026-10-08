"""口播稿升级 API：写操作返回 202 + 任务回执，读操作绝不触发外部请求。

所有业务都委托 content.service / references / tasks；这里只做请求契约、错误映射与回执。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from . import storage as st
from .contracts import ApiError
from .content import context as cx
from .content import media as media_lib
from .content import references, selection, service, tasks
from .content import repository as repo
from .content import contracts as cc
from .content.errors import ContentBlocked, ContentError, ContentRejected
from .content.providers import redfox, text as text_providers

router = APIRouter()


class Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchReq(Req):
    request_id: str
    query: str
    period_days: int = 7
    sort: str = "recommended"
    cursor: Optional[str] = None
    refresh: bool = False


class ImportReq(Req):
    request_id: str
    search_job_id: Optional[str] = None
    platform_work_id: Optional[str] = None
    source_url: Optional[str] = None
    expected_content_revision: int


class ReferenceSelectReq(Req):
    reference_snapshot_id: str
    expected_content_revision: int


class ReferenceRevisionReq(Req):
    request_id: str
    reference_snapshot_id: str
    transcript_text: str
    expected_content_revision: int


class VisualQuoteReq(Req):
    reference_snapshot_id: str
    media_asset_id: str


class VisualConfirmReq(Req):
    reference_snapshot_id: str
    media_asset_id: str
    ceiling_fen: int


class VisualAnalysisReq(Req):
    request_id: str
    reference_snapshot_id: str
    media_asset_id: str
    expected_content_revision: int
    paid_ack: bool = False
    total_ceiling_fen: int = 0
    confirmation_id: Optional[str] = None
    retry_unknown_call_id: str = ""
    retry_paid_ack: bool = False


class SettingsReq(Req):
    expected_content_revision: int
    brief: Optional[Dict[str, Any]] = None
    style_snapshot: Optional[Dict[str, Any]] = None
    style_profile_id: Optional[str] = None
    writer_profile_id: Optional[str] = None


class HumanReviewReq(Req):
    request_id: str = Field(min_length=1, max_length=200)
    script_id: str
    expected_content_revision: int
    expected_batch_id: str
    action: Literal["adopt", "edit_adopt", "rewrite"]
    rubric: Literal["core5", "extended8"]
    scores: Dict[str, StrictInt]
    notes: Optional[str] = None
    facts_ack: bool = False
    feedback: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    storyboard: Optional[List[Dict[str, Any]]] = None


class PromptReq(Req):
    request_id: str
    expected_content_revision: int


class PromptRevisionReq(Req):
    request_id: str
    prompt_id: str
    prompt_text: str
    expected_content_revision: int


class GenerationReq(Req):
    request_id: str
    prompt_id: str
    expected_content_revision: int
    content_route: Optional[Literal["talking_script", "short_video_storyboard"]] = None
    retry_unknown_call_id: str = ""
    retry_paid_ack: bool = False


class RepairReq(Req):
    request_id: str
    batch_id: str
    candidate_ids: List[str]
    expected_content_revision: int
    instruction: Optional[str] = None


class ReviewReq(Req):
    request_id: str
    batch_id: str
    expected_content_revision: int
    retry_unknown_call_id: str = ""
    retry_paid_ack: bool = False


class StyleReq(Req):
    id: Optional[str] = None
    name: str = ""
    persona: str = ""
    attitude: str = ""
    professionalism: str = ""
    humor: str = ""
    phrases: List[str] = Field(default_factory=list)
    banned_phrases: List[str] = Field(default_factory=list)
    own_experience: List[str] = Field(default_factory=list)
    sample_scripts: List[str] = Field(default_factory=list)
    expected_revision: Optional[int] = None


def _err(status: int, code: str, message: str, **extra) -> HTTPException:
    body = ApiError(code=code, message=message, retryable=False).model_dump()
    for key in ("job_id", "poll_url"):
        if body.get(key) is None:
            body.pop(key, None)
    body.update(extra)
    return HTTPException(status_code=status, detail={"error": body})


def _map(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "CONTENT_ERROR")
    message = getattr(exc, "message", str(exc))
    if isinstance(exc, selection.SelectionRejected):
        return _err(exc.http_status, code, message)
    if isinstance(exc, (tasks.IdempotencyConflict, repo.StaleContentWrite, tasks.FlowActive, repo.ImmutableRecordConflict)):
        return _err(409, code, message)
    if isinstance(exc, ContentBlocked):
        return _err(409, code, message)
    if isinstance(exc, ContentRejected):
        if code == "MEDIA_TOO_LARGE":
            return _err(413, code, message)
        if code in ("COST_LIST_CHANGED",):
            return _err(409, code, message)
        return _err(404 if code in ("WORK_NOT_FOUND", "REFERENCE_NOT_FOUND", "PROMPT_NOT_FOUND", "MEDIA_NOT_FOUND") else 422,
                    code, message)
    if isinstance(exc, repo.RepositoryError):
        return _err(404 if code == "WORK_NOT_FOUND" else 422, code, message)
    return _err(500, "CONTENT_INTERNAL_ERROR", type(exc).__name__)


def _work(work_id: str) -> dict:
    work = st.get("works", work_id)
    if work is None:
        raise _err(404, "WORK_NOT_FOUND", f"作品不存在：{work_id}")
    if work.get("deleted_at"):
        raise _err(404, "WORK_DELETED", "作品已删除，不能继续制作")
    return work


def receipt(job: dict, work_id: str) -> Dict[str, Any]:
    from .content.review_recovery import batch_id_for_job
    error = job.get("error")
    # 历史文本断连仅投影为安全诊断，不回显异常原文、不更改作业或费用台账。
    if job["state"] == "unknown" and error and error.get("code") == "CONTENT_CALL_PENDING":
        call = st.get("provider_jobs", error.get("detail") or "")
        if (call and call.get("work_id") == work_id and call.get("parent_content_job_id") == job["id"]
                and call.get("capability") == "chat" and call.get("state") == "unknown"):
            reason = str(call.get("unknown_reason") or "").partition(":")[0]
            if reason in ("RemoteDisconnected", "ConnectionResetError", "BrokenPipeError"):
                error = tasks.connection_closed_error(call["id"]).to_dict()
    # 旧任务仅记录了通用未确认错误；投影已知超时原因，不改台账或再次查询供应商。
    if job["state"] == "unknown" and job["kind"] == "visual_analysis" and error and error.get("code") == "CONTENT_CALL_PENDING":
        call = st.get("provider_jobs", error.get("detail") or "")
        if (call and call.get("work_id") == work_id and call.get("capability") == "visual_analysis" and call.get("state") == "unknown"
                and str(call.get("unknown_reason") or "").startswith("TimeoutError:")):
            if call.get("parent_content_job_id") == job["id"]:
                error = tasks.response_timeout_error(call["id"]).to_dict()
            else:
                from .content.visual import previous_unknown_error
                error = previous_unknown_error(call["id"]).to_dict()
    payload = job.get("payload") or {}
    from . import jobs as provider_jobs
    calls = provider_jobs.content_calls(job["id"])
    return {"job_id": job["id"], "kind": job["kind"], "state": job["state"], "stage": job.get("stage"),
            "replayed": bool(job.get("replayed")), "poll_url": f"/api/v1/works/{work_id}/content/jobs/{job['id']}",
            "result_refs": job.get("result_refs") or {}, "error": error,
            "reference_snapshot_id": payload.get("reference_snapshot_id"), "media_asset_id": payload.get("media_asset_id"),
            "content_revision": job.get("content_revision"), "prompt_id": payload.get("prompt_id"),
            "batch_id": batch_id_for_job(job),
            "calls": [{"id": c["id"], "step": c.get("step"), "provider": c["provider"], "model": c["model"],
                       "state": c["state"], "settlement_state": c.get("settlement_state"),
                       "reserved_fen": c.get("reserved_fen"), "actual_cost_fen": c.get("actual_cost_fen"),
                       "has_remote_handle": bool(c.get("remote_task_id"))} for c in calls]}


def _submit(work_id: str, kind: str, request_id: str, payload: Dict[str, Any], expected: Optional[int]):
    _work(work_id)
    try:
        job = tasks.submit_job(work_id, kind, request_id, payload, expected_revision=expected)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)
    tasks.get_scheduler().start()
    return JSONResponse(status_code=202, content=receipt(job, work_id))


P = "/api/v1/works/{work_id}/content"


@router.get(P + "/human-review/{script_id}")
def get_human_review(work_id: str, script_id: str):
    from .content import human_review

    _work(work_id)
    try:
        return human_review.get_review(work_id, script_id)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)


@router.post(P + "/human-review")
def post_human_review(work_id: str, req: HumanReviewReq):
    from .content import human_review

    _work(work_id)
    try:
        result = human_review.submit(work_id, req.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)
    if result.get("job") is not None:
        result["job"] = receipt(result["job"], work_id)
        tasks.get_scheduler().start()
    return result


@router.post(P + "/searches", status_code=202)
def post_search(work_id: str, req: SearchReq):
    try:
        norm = references.normalize_search_payload(req.model_dump(exclude={"request_id"}))
    except ContentError as exc:
        raise _map(exc)
    payload = {k: v for k, v in norm.items()}
    return _submit(work_id, "search", req.request_id, payload, None)


@router.post(P + "/imports", status_code=202)
def post_import(work_id: str, req: ImportReq):
    if not (req.platform_work_id or req.source_url or req.search_job_id):
        raise _err(422, "VALIDATION_FAILED", "需要作品 ID、抖音链接或搜索任务中的作品")
    payload = req.model_dump(exclude={"request_id", "expected_content_revision"})
    payload = {k: v for k, v in payload.items() if v}
    if req.search_job_id and not (req.platform_work_id or req.source_url):
        raise _err(422, "VALIDATION_FAILED", "从搜索结果导入时必须指明 platform_work_id")
    if req.platform_work_id and req.search_job_id:
        found = tasks.get_job(req.search_job_id)
        items = ((found or {}).get("result_refs") or {}).get("items") or []
        if found is None or found["work_id"] != work_id or not any(i["platform_work_id"] == req.platform_work_id for i in items):
            raise _err(422, "VALIDATION_FAILED", "该作品不在指定的搜索结果中")
    return _submit(work_id, "import", req.request_id, payload, req.expected_content_revision)


@router.put(P + "/reference")
def put_reference(work_id: str, req: ReferenceSelectReq):
    _work(work_id)
    try:
        work = references.select_reference(work_id, req.reference_snapshot_id, req.expected_content_revision)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)
    return {"ok": True, "content_revision": work["content_revision"], "reference_snapshot_id": work["reference_snapshot_id"]}


@router.get(P + "/references/{reference_id}")
def get_reference(work_id: str, reference_id: str):
    _work(work_id)
    ref = repo.get_reference(reference_id)
    if ref is None or ref["work_id"] != work_id:
        raise _err(404, "REFERENCE_NOT_FOUND", "参考不存在或不属于该作品")
    return dict(ref, transcript=cc.transcript_view(ref["transcript"]),
                media_asset_id=media_lib.reference_media_asset_id(work_id, reference_id) or None)


@router.post(P + "/reference-revisions", status_code=202)
def post_reference_revision(work_id: str, req: ReferenceRevisionReq):
    return _submit(work_id, "reference_revision", req.request_id,
                   {"reference_snapshot_id": req.reference_snapshot_id, "transcript_text": req.transcript_text},
                   req.expected_content_revision)


@router.post(P + "/media")
def post_media(work_id: str, reference_snapshot_id: str = Form(...), file: UploadFile = File(...)):
    """上传参考视频（同步：保存 + 探测，不调用任何付费模型）。"""
    from .content import media
    _work(work_id)
    try:
        asset = media.save_uploaded_video(work_id, reference_snapshot_id, file.file, file.filename or "",
                                          file.content_type or "")
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)
    finally:
        file.file.close()
    return {"asset": media.public_view(asset)}


class MediaFetchQuoteReq(Req):
    reference_snapshot_id: str


class MediaFetchReq(Req):
    request_id: str
    reference_snapshot_id: str
    expected_content_revision: int
    paid_ack: bool = False
    total_ceiling_fen: int = 0


@router.post(P + "/media-fetches", status_code=202)
def post_media_fetch(work_id: str, req: MediaFetchReq):
    """自动获取参考视频：RedFox 下载器 → 本地媒体库。独立付费调用，必须有用户确认与费用上限。"""
    from .content import media_fetch
    _work(work_id)
    if req.paid_ack is not True:
        raise _err(422, "MEDIA_FETCH_CONFIRMATION_REQUIRED", "请先确认获取视频的费用")
    if req.total_ceiling_fen <= 0:
        raise _err(422, "COST_AMOUNT_INVALID", "费用上限必须大于 0")
    try:
        conf = media_fetch.create_confirmation(work_id, req.reference_snapshot_id,
                                               ceiling_fen=req.total_ceiling_fen)["id"]
        job = media_fetch.submit_media_fetch(work_id, req.reference_snapshot_id, confirmation_id=conf,
                                             expected_revision=req.expected_content_revision,
                                             request_id=req.request_id)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)
    tasks.get_scheduler().start()
    return JSONResponse(status_code=202, content=receipt(job, work_id))


@router.post(P + "/media-fetch-quotes")
def post_media_fetch_quote(work_id: str, req: MediaFetchQuoteReq):
    from .content import media_fetch
    _work(work_id)
    try:
        return media_fetch.quote(work_id, req.reference_snapshot_id)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)


@router.post(P + "/visual-quotes")
def post_visual_quote(work_id: str, req: VisualQuoteReq):
    from .content import visual
    _work(work_id)
    try:
        return visual.quote(work_id, req.reference_snapshot_id, req.media_asset_id)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)


@router.post(P + "/visual-confirmations")
def post_visual_confirmation(work_id: str, req: VisualConfirmReq):
    """用户确认视觉分析费用；确认绑定参考、媒体哈希、模型与上限，任何一项变化都需重新确认。"""
    from .content import visual
    _work(work_id)
    try:
        row = visual.create_confirmation(work_id, req.reference_snapshot_id, req.media_asset_id,
                                         ceiling_fen=req.ceiling_fen)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)
    return {"confirmation_id": row["id"], "ceiling_fen": row["ceiling_fen"], "request_digest": row["request_digest"]}


@router.post(P + "/visual-analyses", status_code=202)
def post_visual_analysis(work_id: str, req: VisualAnalysisReq):
    from .content import visual
    _work(work_id)
    if req.paid_ack is not True:
        raise _err(422, "VISUAL_COST_CONFIRMATION_REQUIRED", "请先确认视觉分析费用")
    if req.total_ceiling_fen <= 0:
        raise _err(422, "COST_AMOUNT_INVALID", "费用上限必须大于 0")
    try:
        if bool(req.retry_unknown_call_id) != (req.retry_paid_ack is True):
            raise ContentRejected("请单独确认原请求可能已收费，并同意重新提交产生新的费用", "VISUAL_RETRY_CONFIRMATION_REQUIRED")
        prior = visual.replay_submission(work_id, req.request_id, req.reference_snapshot_id,
                                         req.media_asset_id, req.total_ceiling_fen, req.retry_unknown_call_id)
        if prior:
            return JSONResponse(status_code=202, content=receipt(prior, work_id))
        # 页面勾选 paid_ack 即用户确认：在此落一条绑定当前调用清单的确认记录，之后与 Agent 路径走同一条链
        conf = req.confirmation_id or visual.create_confirmation(
            work_id, req.reference_snapshot_id, req.media_asset_id, ceiling_fen=req.total_ceiling_fen,
            retry_unknown_call_id=req.retry_unknown_call_id, retry_paid_ack=req.retry_paid_ack)["id"]
        confirmed = visual.verify_confirmation(work_id, req.reference_snapshot_id, req.media_asset_id, conf)
        if confirmed.get("ceiling_fen") != req.total_ceiling_fen:
            raise ContentRejected("请求费用上限与已确认金额不同，请重新确认", "COST_AMOUNT_INVALID")
        if (confirmed.get("retry_unknown_call_id") or "") != req.retry_unknown_call_id:
            raise ContentRejected("费用确认不包含本次未知请求的重新提交授权", "VISUAL_RETRY_CONFIRMATION_REQUIRED")
        job = visual.submit_visual_analysis(work_id, req.reference_snapshot_id, req.media_asset_id,
                                            confirmation_id=conf, expected_revision=req.expected_content_revision,
                                            request_id=req.request_id)
    except Exception as exc:  # noqa: BLE001
        # 与首个请求并发时，原任务可能在新确认校验期间已建立或完成；精确同 RID 仍取回它。
        if isinstance(exc, tasks.IdempotencyConflict):
            raise _map(exc)
        try:
            prior = visual.replay_submission(work_id, req.request_id, req.reference_snapshot_id,
                                             req.media_asset_id, req.total_ceiling_fen, req.retry_unknown_call_id)
        except Exception as replay_exc:  # noqa: BLE001
            raise _map(replay_exc)
        if prior:
            return JSONResponse(status_code=202, content=receipt(prior, work_id))
        raise _map(exc)
    tasks.get_scheduler().start()
    return JSONResponse(status_code=202, content=receipt(job, work_id))


@router.put(P + "/settings")
def put_settings(work_id: str, req: SettingsReq):
    work = _work(work_id)
    fields: Dict[str, Any] = {}
    if req.brief is not None:
        from .contracts import WorkBrief
        allowed = {"topic", "audience", "content_type", "content_route", "keywords", "target_duration_ms", "materials"}
        bad = sorted(set(req.brief) - allowed)
        if bad:
            raise _err(422, "VALIDATION_FAILED", f"brief 不允许的字段：{'、'.join(bad)}")
        merged = dict(work.get("brief") or {}, **req.brief)
        try:
            fields["brief"] = WorkBrief(**merged).model_dump()
        except Exception as exc:  # noqa: BLE001
            raise _err(422, "VALIDATION_FAILED", "brief 不合法：" + type(exc).__name__)
    if req.style_profile_id:
        profile = repo.get_style_profile(req.style_profile_id)
        if profile is None:
            raise _err(404, "STYLE_NOT_FOUND", "风格模板不存在")
        fields["style_snapshot"] = repo.style_snapshot(profile)
    elif req.style_snapshot is not None:
        snap = dict(req.style_snapshot)
        snap["digest"] = cc.style_digest(snap)
        fields["style_snapshot"] = snap
    if req.writer_profile_id is not None:
        fields["writer_profile_id"] = req.writer_profile_id
    if not fields:
        raise _err(422, "VALIDATION_FAILED", "没有可更新的字段")
    try:
        with st.transaction() as c:
            current = _work(work_id)
            revision = int(current.get("content_revision") or 0)
            if req.expected_content_revision != revision:
                raise repo.StaleContentWrite(f"创作输入已变化（当前 {revision}，请求基于 {req.expected_content_revision}）")
            # Compare normalized briefs without rewriting legacy defaults on a no-op.
            existing = {key: WorkBrief(**(current.get("brief") or {})).model_dump()
                        if key == "brief" else current.get(key) for key in fields}
            if all(existing[key] == value for key, value in fields.items()):
                out = current
            else:
                fields["writing_prompt_id"] = None
                out = repo.bump_content_revision(c, work_id, expected_revision=req.expected_content_revision, **fields)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)
    return {"ok": True, "content_revision": int(out.get("content_revision") or 0)}


@router.post(P + "/prompts", status_code=202)
def post_prompt(work_id: str, req: PromptReq):
    return _submit(work_id, "prompt", req.request_id, {}, req.expected_content_revision)


@router.post(P + "/prompt-revisions", status_code=202)
def post_prompt_revision(work_id: str, req: PromptRevisionReq):
    return _submit(work_id, "prompt_revision", req.request_id,
                   {"prompt_id": req.prompt_id, "prompt_text": req.prompt_text}, req.expected_content_revision)


@router.post(P + "/generations", status_code=202)
def post_generation(work_id: str, req: GenerationReq):
    work = _work(work_id)
    route = (work.get("brief") or {}).get("content_route", "talking_script")
    if req.content_route is not None and req.content_route != route:
        raise _err(409, "CONTENT_ROUTE_STALE", "内容路线已变化，请刷新后重新生成")
    if bool(req.retry_unknown_call_id) != (req.retry_paid_ack is True):
        raise _err(422, "GENERATION_RETRY_CONFIRMATION_REQUIRED", "请单独确认原写稿请求可能已收费，并同意重新提交产生新的费用")
    payload = {"prompt_id": req.prompt_id}
    if req.retry_unknown_call_id:
        call = st.get("provider_jobs", req.retry_unknown_call_id)
        parent = tasks.get_job((call or {}).get("parent_content_job_id", ""))
        valid = bool(call and parent and call.get("work_id") == work_id
                     and call.get("state") == "unknown" and not call.get("remote_task_id")
                     and call.get("step") == "write" and int(call.get("round_no") or 0) == 0
                     and parent.get("work_id") == work_id and parent.get("kind") == "generation"
                     and parent.get("state") == "unknown"
                     and parent.get("content_revision") == req.expected_content_revision
                     and (parent.get("payload") or {}).get("prompt_id") == req.prompt_id)
        if not valid:
            raise _err(409, "GENERATION_RETRY_TARGET_INVALID", "原调用不属于当前写稿任务，或其状态不允许重新提交")
        payload["retry_unknown_call_id"] = req.retry_unknown_call_id
        payload["retry_paid_ack"] = True
    return _submit(work_id, "generation", req.request_id, payload, req.expected_content_revision)


@router.post(P + "/repairs", status_code=202)
def post_repair(work_id: str, req: RepairReq):
    payload = {"batch_id": req.batch_id, "candidate_ids": req.candidate_ids}
    if req.instruction:
        payload["instruction"] = req.instruction
    return _submit(work_id, "repair", req.request_id, payload, req.expected_content_revision)


@router.post(P + "/reviews", status_code=202)
def post_review(work_id: str, req: ReviewReq):
    from .content.review_recovery import pending_review_call

    work = _work(work_id)
    if bool(req.retry_unknown_call_id) != (req.retry_paid_ack is True):
        raise _err(422, "REVIEW_RETRY_CONFIRMATION_REQUIRED", "原审核请求可能已收费，请单独确认重新审核可能产生新的费用")
    payload = {"batch_id": req.batch_id}
    if req.retry_unknown_call_id:
        payload.update(retry_unknown_call_id=req.retry_unknown_call_id, retry_paid_ack=True)
    # 精确同 RID 先走已有幂等契约；旧请求状态、审核结论变化不应使回放变成新调用。
    if tasks.find_job(work_id, "review", req.request_id):
        return _submit(work_id, "review", req.request_id, payload, req.expected_content_revision)
    batch = st.get("script_batches", work_id)
    pending = pending_review_call(work_id, batch, int(work.get("content_revision") or 0))
    if req.retry_unknown_call_id:
        if (not pending or pending["state"] != "unknown" or pending.get("remote_task_id")
                or pending["id"] != req.retry_unknown_call_id or not batch or batch["id"] != req.batch_id
                or work.get("content_revision") != req.expected_content_revision):
            raise _err(409, "REVIEW_RETRY_TARGET_INVALID", "原调用不属于当前批次的未确认审核，或其状态不允许重新提交")
    elif pending and batch["id"] == req.batch_id:
        if pending.get("remote_task_id") or pending["state"] == "waiting_external":
            raise _err(409, "REVIEW_RESULT_PENDING", "原审核有可查询的远端任务，请先核对结果，不能重新提交")
        raise _err(422, "REVIEW_RETRY_CONFIRMATION_REQUIRED", "原审核结果和费用未确认，请确认可能重复计费后重新审核")
    return _submit(work_id, "review", req.request_id, payload, req.expected_content_revision)


def _job_of(work_id: str, job_id: str) -> dict:
    job = tasks.get_job(job_id)
    if job is None or job["work_id"] != work_id:
        raise _err(404, "JOB_NOT_FOUND", "内容任务不存在")
    return job


@router.get(P + "/jobs/{job_id}")
def get_job(work_id: str, job_id: str):
    """只读持久进度：不在 GET 里上传、生成或查远端。"""
    job = _job_of(work_id, job_id)
    from . import jobs as provider_jobs
    calls = provider_jobs.content_calls(job_id)
    out = receipt(job, work_id)
    out.update(cancel_requested=bool(job.get("cancel_requested")),
               archived_result=job.get("archived_result"),
               calls=[{"id": c["id"], "step": c.get("step"), "provider": c["provider"], "model": c["model"],
                       "state": c["state"], "settlement_state": c.get("settlement_state"),
                       "reserved_fen": c.get("reserved_fen"), "actual_cost_fen": c.get("actual_cost_fen"),
                       "has_remote_handle": bool(c.get("remote_task_id"))} for c in calls],
               actions=_actions(job, calls))
    return out


def _actions(job: dict, calls: List[dict]) -> List[str]:
    acts = []
    if job["state"] in tasks.ACTIVE_STATES:
        acts.append("cancel")
    if any(c["state"] == "waiting_external" and c.get("remote_task_id") for c in calls):
        acts.append("refresh")
    if job["state"] == "unknown":
        acts.append("verify")
    return acts


@router.post(P + "/jobs/{job_id}/refresh")
def refresh_job(work_id: str, job_id: str):
    """显式查询已知远端 taskId；未知且无查询句柄返回 needs_verification，不重新提交。"""
    job = _job_of(work_id, job_id)
    from . import jobs as provider_jobs
    calls = [c for c in provider_jobs.content_calls(job_id) if c["state"] in ("waiting_external", "unknown")]
    handles = [c for c in calls if c.get("remote_task_id")]
    if not handles:
        return {"job_id": job_id, "state": job["state"], "status": "needs_verification" if calls or job["state"] == "unknown" else "nothing_to_refresh",
                "message": "没有可查询的远端句柄：结果需要人工核对，系统不会重新提交请求"}
    if job["kind"] == "import":
        current = tasks.request_import_refresh(job_id)
        if current["state"] in tasks.ACTIVE_STATES:
            tasks.get_scheduler().start()
        return {"job_id": job_id, "state": current["state"],
                "status": "needs_verification" if current["state"] == "unknown" else "refreshed"}
    for call in handles:
        try:
            tasks.resume_remote(call["id"], lambda tid: _query_transcript(tid))
        except tasks.CallPending:
            pass
        except Exception as exc:  # noqa: BLE001
            raise _map(exc)
    tasks.get_scheduler().start()
    tasks.set_progress(job_id, state="queued") if job["state"] == "waiting_external" else None
    return {"job_id": job_id, "state": (tasks.get_job(job_id) or job)["state"], "status": "refreshed"}


def _query_transcript(task_id: str) -> tasks.CallOutcome:
    r = redfox.query_transcript(task_id)
    return tasks.CallOutcome(response=r, pending=r["state"] == "processing", cost_fen=references._cost_fen("transcript"))


@router.post(P + "/jobs/{job_id}/cancel")
def cancel_job(work_id: str, job_id: str):
    _job_of(work_id, job_id)
    job = tasks.cancel_job(job_id)
    return receipt(job, work_id)


@router.get(P + "/state")
def get_state(work_id: str):
    from .content.review_recovery import public_review_retry
    work = _work(work_id)
    ctx = cx.load_context(work_id)
    batch = selection.project_batch(work_id)
    jobs_now = tasks.list_jobs(work_id)
    ref = ctx["reference"]
    if ref is not None:
        ref = dict(ref, transcript=cc.transcript_view(ref["transcript"]),
                   media_asset_id=media_lib.reference_media_asset_id(work_id, ref["id"]) or None)
    limits = None
    from .content import costs
    try:
        limits = costs.content_limits().to_dict()
    except Exception:  # noqa: BLE001
        pass
    writer_runtime = None
    writer_error = None
    if not batch or batch.get("source_kind") != "manual":
        try:
            writer_runtime = text_providers.runtime_profile("writer")
        except ContentError as exc:
            writer_error = {"code": exc.code, "message": exc.message}
        except Exception:
            writer_error = {"code": "WRITER_CONFIGURATION_INVALID", "message": "写稿模型配置不可用，可以使用人工创作入口"}
    return {
        "work_id": work_id, "content_revision": ctx["content_revision"], "brief": ctx["brief"],
        "reference": ref, "style": ctx["style"], "prompt": ctx["prompt"], "batch": batch,
        "selected_script_id": work.get("script_version_id"),
        "writer_runtime": writer_runtime,
        "writer_error": writer_error,
        "review_retry": public_review_retry(work_id, st.get("script_batches", work_id), ctx["content_revision"]),
        "active_jobs": [receipt(j, work_id) for j in jobs_now if j["state"] in tasks.ACTIVE_STATES],
        "recent_jobs": [receipt(j, work_id) for j in jobs_now[-10:]],
        "references": [{"id": r["id"], "platform_work_id": r["platform_work_id"], "caption": r["caption"],
                        "author": r["author"], "transcript_state": cc.transcript_view(r["transcript"])["state"],
                        "parent_id": r.get("parent_id"),
                        "visual_state": cc.visual_state(r),
                        "media_asset_id": media_lib.reference_media_asset_id(work_id, r["id"]) or None,
                        "has_video_url": bool(r.get("video_url"))}
                       for r in repo.list_references(work_id)],
        "media_assets": [media_lib.public_view(cc.MediaAsset(**r))
                         for r in sorted(st.all_rows("content_media_assets", "work_id=?", [work_id]),
                                         key=lambda r: r["created_at"])],
        "limits": limits,
    }


# ---------------- 风格共享模板与能力页（不绑定作品）----------------

@router.get("/api/v1/content/style-profiles")
def list_styles():
    return repo.list_style_profiles()


@router.post("/api/v1/content/style-profiles", status_code=201)
def create_style(req: StyleReq):
    profile = req.model_dump(exclude={"expected_revision"})
    profile["id"] = st.new_id("style")
    try:
        with st.transaction() as c:
            return repo.save_style_profile(c, profile)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)


@router.put("/api/v1/content/style-profiles/{profile_id}")
def update_style(profile_id: str, req: StyleReq):
    profile = req.model_dump(exclude={"expected_revision"})
    profile["id"] = profile_id
    try:
        with st.transaction() as c:
            return repo.save_style_profile(c, profile, expected_revision=req.expected_revision)
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)


@router.get("/api/v1/content/capabilities")
def get_capabilities():
    """只显示配置、协议能力与价格日期，不显示任何凭据。"""
    from .content.providers import qwen_vision
    return {"roles": text_providers.capabilities(), "redfox": redfox.status(), "visual": qwen_vision.status()}
