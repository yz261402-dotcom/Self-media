"""Explicit human takeover: structured scripts and bound facts acknowledgement, without model calls."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import Field, StrictBool, StrictInt

from .. import storage as st
from ..contracts import StrictModel, Task, now_iso
from . import context as cx, contracts as cc, repository as repo, tasks
from .errors import ContentRejected

router = APIRouter()
MAX_SHOTS = 30


class ManualStoryboardShot(StrictModel):
    shot_index: StrictInt = Field(ge=1)
    visual: str = Field(min_length=1)
    dialogue: str = Field(min_length=1)
    sound_effects: str = Field(min_length=1)
    duration_seconds: StrictInt = Field(ge=2, le=30)
    prompt: str = ""


class ManualStoryboardRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=200)
    expected_content_revision: StrictInt = Field(ge=0)
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    storyboard: List[ManualStoryboardShot] = Field(min_length=1, max_length=MAX_SHOTS)
    takeover_reason: str = Field(min_length=1, max_length=4000)
    manual_ack: StrictBool
    facts_ack: StrictBool


def is_manual_batch(batch: dict | None) -> bool:
    return bool(batch) and batch.get("source_kind") == "manual"


def structural_report(candidate: dict, ctx: dict) -> dict:
    from . import service

    if not str(candidate.get("title") or "").strip():
        raise ContentRejected("分镜标题不能为空", "CANDIDATE_INCOMPLETE")
    if len(candidate.get("storyboard") or []) > MAX_SHOTS:
        raise ContentRejected(f"分镜数量须为 1 至 {MAX_SHOTS}", "STORYBOARD_INVALID")
    shots, text, duration = service._storyboard(candidate.get("storyboard"), ctx["brief_core"]["target_duration_ms"])
    if (shots != candidate.get("storyboard") or text != candidate.get("text")
            or duration != candidate.get("duration_estimate_ms")):
        raise ContentRejected("人工稿正文、时长与结构化分镜不一致", "STORYBOARD_INVALID")
    return {"checks": [
        {"code": "STORYBOARD_STRUCTURE", "outcome": "pass", "hard_gate": True,
         "message": "结构、连续编号、每镜时长及目标总时长校验通过"},
        {"code": "HUMAN_FACTS_ACK", "outcome": "pass", "hard_gate": True,
         "message": "用户已明确确认自行核对本稿事实与使用权；未产生模型事实核验结论"},
        {"code": "AI_REVIEW", "outcome": "not_run", "hard_gate": False,
         "message": "人工接管稿未执行智能体审核，采用须记录人工确认"},
    ], "overall": "review", "review_required": True}


def current_import(work_id: str, candidate: dict, batch: dict, ctx: dict) -> dict | None:
    """Only persisted immutable source and current input binding establish manual eligibility."""
    from . import human_review

    if not is_manual_batch(batch) or ctx["brief_core"]["content_route"] != "short_video_storyboard":
        return None
    original_batch = repo.get_batch(batch.get("id") or "")
    saved = repo.get_script_version(candidate.get("id") or "")
    if (not original_batch or not saved or original_batch.get("source_kind") != "manual"
            or original_batch.get("work_id") != work_id or saved.get("work_id") != work_id
            or original_batch.get("content_route") != "short_video_storyboard"
            or saved.get("content_route") != "short_video_storyboard"
            or original_batch.get("content_revision") != ctx["content_revision"]
            or batch.get("content_revision") != ctx["content_revision"]
            or len(original_batch.get("members") or []) != 1 or len(batch.get("candidates") or []) != 1
            or candidate.get("slot") != 1
            or original_batch["members"][0].get("candidate_id") != candidate.get("id")
            or original_batch["members"][0].get("text_digest") != cc.candidate_digest(candidate)
            or original_batch.get("set_digest") != batch.get("set_digest")
            or cc.candidate_digest(saved) != cc.candidate_digest(candidate)
            or any(saved.get(field) != candidate.get(field) for field in ("title", "description", "tags", "origin"))):
        return None
    origin = saved.get("origin") or {}
    if origin.get("kind") not in ("manual_import", "manual_edit"):
        return None
    decision = st.get("quality_decisions", origin.get("manual_import_id") or "") or {}
    if (decision.get("review_kind") != "manual_storyboard_import" or decision.get("independent") is not False
            or decision.get("work_id") != work_id or decision.get("script_id") != candidate.get("id")
            or decision.get("manual_ack") is not True or decision.get("facts_ack") is not True
            or decision.get("ai_review_status") != "not_performed"
            or not str(decision.get("takeover_reason") or "").strip()
            or decision.get("binding") != human_review.binding(candidate, batch, ctx)):
        return None
    try:
        structural_report(candidate, ctx)
    except ContentRejected:
        return None
    return decision


def quality_report(work_id: str, candidate: dict, batch: dict, ctx: dict) -> dict:
    if not current_import(work_id, candidate, batch, ctx):
        raise ContentRejected("人工接管依据已过期或缺失，请重新导入并确认当前稿件事实", "MANUAL_IMPORT_STALE")
    return structural_report(candidate, ctx)


def _assert_idle(work_id: str) -> None:
    active = tasks.list_jobs(work_id, active_only=True)
    mutation = next((job for job in active if job["kind"] in tasks.MUTATING_KINDS), None)
    if mutation:
        raise tasks.FlowActive("该作品有进行中的创作或审核任务，请完成或取消后再人工接管", detail=mutation["id"])


def _build(work_id: str, ctx: dict, payload: dict, audit_id: str, parent: dict | None = None) -> dict:
    from . import service

    if not str(payload.get("title") or "").strip():
        raise ContentRejected("分镜标题不能为空", "CANDIDATE_INCOMPLETE")
    if len(payload.get("storyboard") or []) > MAX_SHOTS:
        raise ContentRejected(f"分镜数量须为 1 至 {MAX_SHOTS}", "STORYBOARD_INVALID")
    raw_shots = payload.get("storyboard")
    if not isinstance(raw_shots, list) or any(not isinstance(shot, dict)
            or type(shot.get("shot_index")) is not int or type(shot.get("duration_seconds")) is not int
            for shot in raw_shots):
        raise ContentRejected("人工分镜编号和镜头时长必须为整数", "STORYBOARD_INVALID")
    version = service._build_version(
        {"title": payload["title"], "description": payload.get("description", ""), "angle": "人工创作",
         "tags": (parent or {}).get("tags", []), "storyboard": payload.get("storyboard"), "claims": []},
        ctx=ctx, job={"id": audit_id, "work_id": work_id}, slot=1,
        parent_id=(parent or {}).get("id"), round_no=0, writer={"kind": "human"})
    version["description"] = payload.get("description") or ""
    version["origin"] = {
        "kind": "manual_edit" if parent else "manual_import", "manual_import_id": audit_id,
        "from": (parent or {}).get("id"), "writer": {"kind": "human"},
        "identity_snapshot": ctx.get("identity") or {}, "created_at": now_iso(),
        "automated_rule_report": version["quality_report"], "ai_review_status": "not_performed",
    }
    version["quality_report"] = structural_report(version, ctx)
    return version


def _handoff_main_task(connection, work_id: str, audit_id: str) -> None:
    work = repo._work(connection, work_id)
    old = st.get("tasks", work.get("current_task_id") or "")
    if not old or old.get("work_id") != work_id:
        return
    old["state"] = "cancelled"
    old["stage_rows"] = st.stage_rows(old["stage"], old["state"])
    st.insert_row(connection, "tasks", old["id"], old, {"work_id": work_id})
    st.add_event_tx(connection, old["id"], {
        "type": "manual.takeover", "occurred_at": now_iso(), "stage": old["stage"], "state": "cancelled",
        "message": "用户人工接管内容创作，停止旧编排的后续工具调用；已发送作业和费用记录保留",
    })
    new = Task(id=st.new_id("task"), work_id=work_id, mode=old.get("mode", "mock"),
               route=old.get("route", "M2"), route_confirmed=old.get("route_confirmed", False),
               route_selection_id=old.get("route_selection_id"), stage="content_review",
               state="waiting_selection", pending_selection_ids=["sel_script"],
               stage_rows=st.stage_rows("content_review", "waiting_selection")).model_dump()
    st.insert_row(connection, "tasks", new["id"], new, {"work_id": work_id})
    st.add_event_tx(connection, new["id"], {
        "type": "selection.requested", "occurred_at": now_iso(), "stage": "content_review",
        "state": "waiting_selection", "message": f"人工接管稿 {audit_id} 等待人工评分与确认采用",
    })
    work.update(current_task_id=new["id"], version=int(work.get("version") or 1) + 1)
    st.insert_row(connection, "works", work_id, work)


def _persist(connection, work_id: str, version: dict, ctx: dict, audit: dict) -> dict:
    from . import human_review, selection

    batch_id = st.new_id("cbatch_manual")
    repo.save_script_version(connection, version)
    repo.save_batch(connection, {
        "id": batch_id, "work_id": work_id, "content_route": "short_video_storyboard",
        "source_kind": "manual", "content_revision": ctx["content_revision"], "requested": 1,
        "members": [{"slot": 1, "candidate_id": version["id"]}], "materials_count": len(ctx["materials"]),
    })
    selection.on_batch_replaced(connection, work_id)
    # A new task identity keeps delayed DeepAgents delegates stopped after human adoption resumes media work.
    _handoff_main_task(connection, work_id, audit["id"])
    batch = repo.bind_current_batch(connection, work_id, batch_id, ctx["content_revision"], None)
    candidate = batch["candidates"][0]
    audit.update(script_id=version["id"], binding=human_review.binding(candidate, batch, ctx))
    result = {"ok": True, "script_id": version["id"], "batch_id": batch_id,
              "content_revision": ctx["content_revision"],
              "candidate": dict(candidate, creation_source="manual", ai_review_status="not_performed",
                                takeover_reason=audit["takeover_reason"], human_review_state="pending",
                                review_state="needs_review", pickable=True, unpickable_reasons=[])}
    audit["result"] = result
    repo._insert_immutable(connection, "quality_decisions", audit, {"work_id": work_id})
    return result


def create(work_id: str, payload: Dict[str, Any]) -> dict:
    parsed = ManualStoryboardRequest(**payload).model_dump()
    if parsed["manual_ack"] is not True or parsed["facts_ack"] is not True:
        raise ContentRejected("请明确确认人工接管，并确认已自行核对本稿事实与使用权", "MANUAL_ACK_REQUIRED")
    if not parsed["request_id"].strip() or not parsed["takeover_reason"].strip():
        raise ContentRejected("请填写人工接管原因和有效 request_id", "VALIDATION_FAILED")
    key = "manual_" + cc.digest([work_id, parsed["request_id"]])[7:]
    request_digest = cc.digest(parsed)
    with st.transaction() as connection:
        ctx = cx.load_context(work_id)
        prior = st.get("quality_decisions", key)
        if prior:
            if prior.get("request_digest") != request_digest:
                raise tasks.IdempotencyConflict("同一人工接管 request_id 的内容发生变化")
            return prior["result"]
        if ctx["brief_core"]["content_route"] != "short_video_storyboard":
            raise ContentRejected("人工创作仅适用于分镜剧本，请先切换为分镜路线", "HUMAN_REVIEW_ROUTE_REQUIRED")
        if ctx["content_revision"] != parsed["expected_content_revision"]:
            raise repo.StaleContentWrite("创作输入已变化，请刷新人工创作页面")
        _assert_idle(work_id)
        version = _build(work_id, ctx, parsed, key)
        unresolved = [job["id"] for job in tasks.list_jobs(work_id) if job.get("state") == "unknown"]
        audit = {"id": key, "work_id": work_id, "review_kind": "manual_storyboard_import", "independent": False,
                 "request_id": parsed["request_id"], "request_digest": request_digest,
                 "manual_ack": True, "facts_ack": True, "ai_review_status": "not_performed",
                 "takeover_reason": parsed["takeover_reason"].strip(), "unresolved_job_ids": unresolved,
                 "created_at": now_iso()}
        return _persist(connection, work_id, version, ctx, audit)


def revise(connection, work_id: str, candidate: dict, batch: dict, ctx: dict, payload: dict) -> dict:
    original = current_import(work_id, candidate, batch, ctx)
    if not original:
        raise ContentRejected("人工接管依据已过期，请重新导入并确认当前稿件事实", "MANUAL_IMPORT_STALE")
    if payload.get("facts_ack") is not True:
        raise ContentRejected("请确认已经重新核对修改后的稿件事实与使用权", "MANUAL_EDIT_FACTS_ACK_REQUIRED")
    key = st.new_id("manual_edit")
    new_payload = {"title": candidate["title"] if payload.get("title") is None else payload["title"],
                   "description": candidate.get("description", "") if payload.get("description") is None
                                  else payload["description"], "storyboard": payload.get("storyboard")}
    version = _build(work_id, ctx, new_payload, key, parent=candidate)
    if (cc.candidate_digest(version) == cc.candidate_digest(candidate)
            and all(version.get(field) == candidate.get(field) for field in ("title", "description"))):
        raise ContentRejected("分镜剧本没有修改，请修改后再确认采用", "HUMAN_EDIT_UNCHANGED")
    audit = {"id": key, "work_id": work_id, "review_kind": "manual_storyboard_import", "independent": False,
             "source_import_id": original["id"], "source_script_id": candidate["id"],
             "manual_ack": True, "facts_ack": True, "ai_review_status": "not_performed",
             "takeover_reason": original["takeover_reason"],
             "unresolved_job_ids": original.get("unresolved_job_ids") or [], "created_at": now_iso()}
    return _persist(connection, work_id, version, ctx, audit)["candidate"]


@router.post("/api/v1/works/{work_id}/content/manual-storyboards")
def post_manual_storyboard(work_id: str, req: ManualStoryboardRequest):
    try:
        return create(work_id, req.model_dump())
    except Exception as exc:
        from ..content_api import _map
        raise _map(exc)
