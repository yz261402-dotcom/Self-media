"""Deterministic content entrypoint, independent of the agent orchestrator."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .. import config, storage as st
from ..contracts import Task, WorkBrief, now_iso
from . import context as cx, contracts as cc, repository as repo, service, tasks
from .errors import ContentBlocked, ContentRejected

router = APIRouter()


class MaterialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    source: str = ""


class DirectGenerationReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=200)
    expected_content_revision: StrictInt = Field(ge=0)
    topic: str = Field(min_length=1)
    materials: Optional[List[Union[str, MaterialInput]]] = None
    content_route: Optional[Literal["talking_script", "short_video_storyboard"]] = None


def _normalize(payload: Dict[str, Any], work: dict) -> dict:
    topic = payload.get("topic", (work.get("brief") or {}).get("topic"))
    if not isinstance(topic, str) or not topic.strip():
        raise ContentRejected("缺少主题，不能生成稿件", "TOPIC_REQUIRED")
    route = payload.get("content_route")
    if route not in (None, "talking_script", "short_video_storyboard"):
        raise ContentRejected("内容路线不合法", "VALIDATION_FAILED")
    raw = payload.get("materials")
    materials = None
    if raw is not None:
        if not isinstance(raw, list):
            raise ContentRejected("materials 必须是列表（字符串或 {text, source}）", "VALIDATION_FAILED")
        materials = []
        for item in raw:
            if isinstance(item, str):
                text, source = item.strip(), ""
            elif isinstance(item, dict) and isinstance(item.get("text"), str) and isinstance(item.get("source", ""), str):
                text, source = item["text"].strip(), item.get("source", "").strip()
            else:
                raise ContentRejected("参考资料必须是文本或 {text, source}", "VALIDATION_FAILED")
            if text:
                materials.append({"text": text, "source": source})
    return {"topic": topic.strip(), "materials": materials, "content_route": route,
            "expected_content_revision": payload.get("expected_content_revision")}


def _guard_new_request(c, work_id: str) -> None:
    for row in c.execute("SELECT data FROM content_jobs WHERE work_id=?", [work_id]).fetchall():
        job = json.loads(row["data"])
        if job.get("kind") not in tasks.MUTATING_KINDS:
            continue
        if job["state"] in tasks.ACTIVE_STATES:
            raise tasks.FlowActive("该作品已有进行中的创作任务，请等待完成或先取消", detail=job["id"])
        if job["state"] == "unknown":
            raise ContentBlocked("原创作请求的结果和费用尚未确认，请先核对原任务，不能自动重新写稿",
                                 "CONTENT_RESULT_UNKNOWN", detail=job["id"], retryable=False)
    for row in c.execute("SELECT data FROM provider_jobs WHERE work_id=?", [work_id]).fetchall():
        call = json.loads(row["data"])
        if (call.get("namespace") == "content-v2" and call.get("capability") == "chat"
                and call.get("state") in ("unknown", "leased", "waiting_external")):
            raise ContentBlocked("存在已发送但结果未确认的模型请求，请先核对，不能通过直接生成重复提交",
                                 "CONTENT_CALL_PENDING", detail=call["id"], retryable=False)


def _brief_fingerprint(brief: dict) -> str:
    return cc.digest({"brief": cx.brief_core(brief), "materials": cx.materials_for(brief)})


def _start_main_task(c, work: dict, *, create: bool) -> None:
    task = repo._row(c, "tasks", work.get("current_task_id") or "")
    if task is None and create:
        task = Task(id=st.new_id("task"), work_id=work["id"], mode=config.resolve_mode(),
                    route=work.get("route") or "M2").model_dump()
        selections = [json.loads(row["data"]) for row in c.execute(
            "SELECT data FROM selections WHERE work_id=? ORDER BY ins_seq", [work["id"]]).fetchall()]
        prior = [s for s in selections if s.get("kind") == "route" and s.get("selected_ref")]
        if prior:
            task.update(route_selection_id=prior[-1]["id"], route=prior[-1]["selected_ref"], route_confirmed=True)
        work["current_task_id"] = task["id"]
        st.insert_row(c, "works", work["id"], work)
    if task is None:
        return
    # The pending selection also keeps startup recovery out of the agent orchestrator.
    task.update(stage="script", state="waiting_selection", last_error=None, block_reason=None)
    task["pending_selection_ids"] = list(dict.fromkeys([*task.get("pending_selection_ids", []), "sel_script"]))
    task["stage_rows"] = st.stage_rows(task["stage"], task["state"])
    st.insert_row(c, "tasks", task["id"], task, {"work_id": work["id"]})
    st.add_event_tx(c, task["id"], {"type": "stage.started", "occurred_at": now_iso(), "stage": "script",
                                    "state": "waiting_selection", "message": "直接工作流已提交：写稿后进行独立审核"})


def submit(work_id: str, payload: Dict[str, Any], *, legacy: bool = False) -> dict:
    """Atomically prepare inputs and enqueue one generation, without a model call."""
    request_id = payload.get("request_id")
    if not legacy and (not isinstance(request_id, str) or not request_id.strip()):
        raise ContentRejected("写操作必须带 request_id", "VALIDATION_FAILED")
    with st.transaction() as c:
        work = repo._work(c, work_id)
        normalized = _normalize(payload, work)
        digest = cc.digest(normalized)
        if request_id:
            row = c.execute("SELECT data FROM content_jobs WHERE work_id=? AND kind='generation' AND request_id=?",
                            [work_id, request_id]).fetchone()
            if row:
                prior = json.loads(row["data"])
                if (prior.get("payload") or {}).get("direct_request_digest") != digest:
                    raise tasks.IdempotencyConflict("同一 request_id 对应了不同的直接生成请求", detail=request_id)
                return dict(prior, replayed=True)
        elif legacy:
            # Legacy clients do not send request IDs. Replays use the completed preparation revision.
            rows = c.execute("SELECT data FROM content_jobs WHERE work_id=? AND kind='generation' ORDER BY ins_seq DESC",
                             [work_id]).fetchall()
            for row in rows:
                prior = json.loads(row["data"])
                if (prior.get("content_revision") == int(work.get("content_revision") or 0)
                        and (prior.get("payload") or {}).get("direct_request_digest") == digest):
                    return dict(prior, replayed=True)
        expected = normalized["expected_content_revision"]
        revision = int(work.get("content_revision") or 0)
        if expected is not None and (type(expected) is not int or expected != revision):
            raise repo.StaleContentWrite(f"创作输入已变化（当前 {revision}，请求基于 {expected}）")
        _guard_new_request(c, work_id)
        requested_mode = config.get_mode()
        if requested_mode != "mock" and config.resolve_mode() != requested_mode:
            raise ContentBlocked("当前运行模式尚未授权真实调用，请检查运行模式与预算配置",
                                 "CONTENT_MODE_NOT_AUTHORIZED", retryable=False)
        brief = dict(work.get("brief") or {}, topic=normalized["topic"])
        if normalized["content_route"] is not None:
            brief["content_route"] = normalized["content_route"]
        if normalized["materials"] is not None:
            brief["materials"] = [dict(item, content_hash=hashlib.sha256(item["text"].encode("utf-8")).hexdigest(),
                                       added_at=now_iso()) for item in normalized["materials"]]
        WorkBrief(**brief)
        if _brief_fingerprint(brief) != _brief_fingerprint(work.get("brief") or {}):
            work = repo.bump_content_revision(c, work_id, brief=brief)
        prompt = service.ensure_prompt(work_id, _connection=c)
        work = repo._work(c, work_id)
        if not request_id:
            request_id = "legacy_" + cc.digest({"work": work_id, "revision": work["content_revision"], "input": normalized})[7:]
        job = tasks.submit_job(work_id, "generation", request_id,
                               {"prompt_id": prompt["id"], "topic": normalized["topic"],
                                "orchestration": "direct", "direct_request_digest": digest},
                               expected_revision=int(work.get("content_revision") or 0), _connection=c)
        _start_main_task(c, repo._work(c, work_id), create=True)
        return job


@router.post("/api/v1/works/{work_id}/content/direct-generations", status_code=202)
def post_direct_generation(work_id: str, req: DirectGenerationReq):
    from ..content_api import _map, receipt

    try:
        job = submit(work_id, req.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise _map(exc)
    tasks.get_scheduler().start()
    return JSONResponse(status_code=202, content=receipt(job, work_id))
