"""Human storyboard review, immutable evidence and atomic adoption/rewrite requests."""
from __future__ import annotations

from typing import Any, Dict, Optional

from .. import storage as st
from ..contracts import now_iso
from . import context as cx, contracts as cc, repository as repo, tasks
from .errors import ContentRejected

CORE = ("hook", "momentum", "interaction", "empathy", "structure")
EXTENDED = CORE + ("value", "delivery", "execution")


def score_summary(rubric: str, scores: dict) -> dict:
    keys = CORE if rubric == "core5" else EXTENDED if rubric == "extended8" else ()
    if not keys or set(scores) != set(keys):
        raise ContentRejected("请完成所选评分表的全部项目", "HUMAN_SCORES_INCOMPLETE")
    if any(type(value) is not int or not 1 <= value <= 5 for value in scores.values()):
        raise ContentRejected("每项评分必须是 1 到 5 的整数", "HUMAN_SCORES_INVALID")
    total = sum(scores.values())
    return {"total": total, "count": len(keys), "average": total / len(keys), "passed": total > 4 * len(keys)}


def binding(candidate: dict, batch: dict, ctx: dict) -> dict:
    prompt = ctx.get("prompt") or {}
    return {"script_id": candidate["id"], "candidate_digest": cc.candidate_digest(candidate),
            "presentation_digest": cc.digest({key: candidate.get(key) for key in ("title", "description", "tags")}),
            "batch_id": batch["id"], "set_digest": batch.get("set_digest"),
            "content_revision": ctx["content_revision"], "digests": ctx["digests"],
            "prompt_id": prompt.get("id"), "prompt_version": prompt.get("version")}


def current_review(work_id: str, candidate: dict, batch: dict, ctx: dict) -> Optional[dict]:
    expected = binding(candidate, batch, ctx)
    rows = [row for row in st.all_rows("quality_decisions", "work_id=?", [work_id])
            if row.get("review_kind") == "human_storyboard" and row.get("mode") in ("scored_pass", "human_edit", "manual_takeover")
            and row.get("binding") == expected]
    from . import manual_creation as manual
    if manual.is_manual_batch(batch):
        imported = manual.current_import(work_id, candidate, batch, ctx)
        if not imported:
            return None
        rows = [row for row in rows if row.get("manual_import_id") == imported["id"]
                and row.get("creation_source") == "manual"
                and (row["mode"] == "human_edit" or row["mode"] == "manual_takeover"
                     and row.get("passed") is True and row.get("scored_pass") is True)]
    return sorted(rows, key=lambda row: row["created_at"])[-1] if rows else None


def _current(work_id: str, script_id: str, expected_revision=None, expected_batch_id=None):
    from . import selection

    ctx = cx.load_context(work_id)
    if ctx["work"].get("deleted_at"):
        raise ContentRejected("作品已删除", "WORK_DELETED")
    batch = st.get("script_batches", work_id)
    candidate = selection._find_candidate(batch, script_id)
    if not candidate:
        raise selection.SelectionRejected("稿件不存在或已被新版本取代", "STALE_SELECTION")
    if expected_revision is not None and ctx["content_revision"] != expected_revision:
        raise repo.StaleContentWrite("创作输入已变化，请刷新人工审核页")
    if expected_batch_id is not None and batch["id"] != expected_batch_id:
        raise selection.SelectionRejected("候选批次已变化，请刷新人工审核页", "STALE_SELECTION")
    if (candidate.get("content_route") != "short_video_storyboard"
            or ctx["brief_core"]["content_route"] != "short_video_storyboard"):
        raise ContentRejected("人工评分仅适用于分镜剧本", "HUMAN_REVIEW_ROUTE_REQUIRED")
    return ctx, batch, candidate


def get_review(work_id: str, script_id: str) -> dict:
    from . import manual_creation as manual, selection

    ctx, batch, candidate = _current(work_id, script_id)
    ev = selection.evaluate(work_id, candidate, batch, ctx)
    imported = manual.current_import(work_id, candidate, batch, ctx)
    source = "manual" if manual.is_manual_batch(batch) else "llm"
    ai_status = "not_performed" if source == "manual" else "passed" if ev["review_state"] == "passed" else "required"
    candidate = dict(candidate, creation_source=source, ai_review_status=ai_status,
                     takeover_reason=(imported or {}).get("takeover_reason"))
    return {"candidate": candidate, "content_revision": ctx["content_revision"], "batch_id": batch["id"],
            "eligible": ev["pickable"], "reasons": ev["reasons"],
            "creation_source": source, "ai_review_status": ai_status,
            "takeover_reason": (imported or {}).get("takeover_reason"),
            "review": current_review(work_id, candidate, batch, ctx)}


def _assert_idle(work_id: str) -> None:
    active = [job for job in tasks.list_jobs(work_id, active_only=True) if job["kind"] in tasks.MUTATING_KINDS]
    if active:
        raise tasks.FlowActive("该作品有进行中的创作或审核任务，请完成或取消后再确认人工审核", detail=active[0]["id"])


def submit(work_id: str, payload: Dict[str, Any]) -> dict:
    from . import manual_creation as manual, selection, service

    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ContentRejected("人工审核必须带 request_id", "VALIDATION_FAILED")
    key = "human_" + cc.digest([work_id, request_id])[7:]
    request_digest = cc.digest(payload)
    with st.transaction() as connection:
        prior = st.get("quality_decisions", key)
        if prior:
            if prior.get("request_digest") != request_digest:
                raise tasks.IdempotencyConflict("同一人工审核 request_id 的内容发生变化")
            result = dict(prior["result"])
            result["review"] = {key: value for key, value in prior.items() if key not in ("result", "request_digest")}
            if result.get("job_id"):
                result["job"] = tasks.get_job(result.pop("job_id"))
            return result
        ctx, batch, candidate = _current(work_id, payload["script_id"],
                                         payload["expected_content_revision"], payload["expected_batch_id"])
        _assert_idle(work_id)
        ev = selection.evaluate(work_id, candidate, batch, ctx)
        if not ev["pickable"]:
            raise selection.SelectionRejected("；".join(ev["reasons"]) or "请先完成智能体审核", ev["code"])
        summary = score_summary(payload["rubric"], payload["scores"])
        action = payload["action"]
        mode = {"adopt": "scored_pass", "edit_adopt": "human_edit", "rewrite": "rewrite"}.get(action)
        imported = manual.current_import(work_id, candidate, batch, ctx)
        if imported and action == "adopt":
            mode = "manual_takeover"
        if mode is None:
            raise ContentRejected("未知人工审核操作", "VALIDATION_FAILED")
        record = {"id": key, "work_id": work_id, "review_kind": "human_storyboard", "independent": False,
                  "request_id": request_id, "request_digest": request_digest, "source_script_id": candidate["id"],
                  "script_id": candidate["id"], "mode": mode, "rubric": payload["rubric"], "scores": payload["scores"],
                  **summary, "notes": str(payload.get("notes") or ""), "created_at": now_iso(),
                  "ai_review_id": ev["review_id"], "ai_set_review_id": ev["set_review_id"]}
        if imported:
            record.update(manual_import_id=imported["id"], creation_source="manual", ai_review_status="not_performed",
                          takeover_reason=imported["takeover_reason"], scored_pass=summary["passed"])
        result = {"ok": True, "action": action, "script_id": candidate["id"]}
        if action == "rewrite":
            if imported:
                unresolved = [job for job in tasks.list_jobs(work_id) if job.get("state") == "unknown"]
                if unresolved:
                    raise tasks.CallPending("原模型请求结果仍未确认，请先核对原请求后再提交收费重写",
                                            detail=unresolved[0]["id"])
                raise ContentRejected("人工稿没有原三稿写作批次，请返回内容创作使用直接生成新稿，再完成智能体和人工审核",
                                      "MANUAL_REWRITE_REQUIRES_GENERATION")
            feedback = str(payload.get("feedback") or "").strip()
            if not feedback:
                raise ContentRejected("请输入重写意见", "HUMAN_FEEDBACK_REQUIRED")
            if not repo.get_batch(batch["id"]):
                raise selection.SelectionRejected("历史批次无法定向返工，请重新生成", "LEGACY_BATCH")
            # Queue and review evidence commit together; the scheduler starts only after this transaction.
            job = tasks.submit_job(work_id, "repair", "human-review:" + request_id,
                                   {"batch_id": batch["id"], "candidate_ids": [candidate["id"]],
                                    "instruction": feedback, "human_review_id": key},
                                   expected_revision=ctx["content_revision"], _connection=connection)
            record["feedback"] = feedback
            result["job_id"] = job["id"]
        elif action == "adopt":
            if not summary["passed"]:
                raise ContentRejected("人工评分平均分必须大于 4 分，4 分不合格", "HUMAN_SCORE_NOT_PASSED")
            if any(payload.get(field) is not None for field in ("title", "description", "storyboard")):
                raise ContentRejected("采用原稿不能同时修改分镜，请选择修改后确认采用", "HUMAN_ADOPT_INPUT_INVALID")
        else:
            if payload.get("storyboard") is None:
                raise ContentRejected("修改后采用必须提交完整结构化分镜", "STORYBOARD_REQUIRED")
            revised = (manual.revise(connection, work_id, candidate, batch, ctx, payload) if imported else
                       service.revise_candidate(work_id, candidate["id"], storyboard=payload["storyboard"],
                                                title=payload.get("title"), description=payload.get("description"),
                                                _connection=connection))
            if (cc.candidate_digest(revised) == cc.candidate_digest(candidate)
                    and all(revised.get(field) == candidate.get(field) for field in ("title", "description"))):
                raise ContentRejected("分镜剧本没有修改，请修改后再确认采用", "HUMAN_EDIT_UNCHANGED")
            candidate = revised
            batch = st.get("script_batches", work_id)
            ctx = cx.load_context(work_id)
            if imported:
                updated_import = manual.current_import(work_id, candidate, batch, ctx)
                record.update(manual_import_id=updated_import["id"], facts_ack=True)
            record["script_id"] = candidate["id"]
            result["script_id"] = candidate["id"]
        record["binding"] = binding(candidate, batch, ctx)
        record["result"] = dict(result)
        repo._insert_immutable(connection, "quality_decisions", record, {"work_id": work_id})
        if action != "rewrite":
            selection.adopt(work_id, candidate["id"], expected_revision=ctx["content_revision"], _connection=connection)
        result["review"] = {key: value for key, value in record.items() if key not in ("result", "request_digest")}
        if action == "rewrite":
            result["job"] = tasks.get_job(result.pop("job_id"))
        return result
