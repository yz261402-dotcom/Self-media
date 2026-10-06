"""稿件“能否被选用”与选用事务。服务端事实：UI 的 pickable 只是这里的投影。

selectable == 硬门禁通过 ∧ 当前单稿独立审核通过 ∧ 当前集合审核未否决该稿
所有“当前”都指绑定摘要与现状一致（正文、参考、资料、风格、提示、content_revision、审核方模型）。
adopt 在一个事务里完成：复验 → 撤旧选用与下游 → 写新选择 → 更新任务与事件；不嵌套事务、不隐式 commit。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .. import storage as st
from ..contracts import SelectionDecision, SelectionOption, SelectionRequest, now_iso
from . import context as cx
from . import contracts as cc
from . import repository as repo
from . import semantic_review as sr
from .errors import ContentError


class SelectionRejected(ContentError):
    """http_status 由 API 层映射；code 遵循交接文档：SCRIPT_REVIEW_REQUIRED / SCRIPT_QUALITY_FAILED / STALE_SELECTION。"""
    code = "SELECTION_REJECTED"

    def __init__(self, message: str, code: str, http_status: int = 409):
        super().__init__(message, code)
        self.http_status = http_status


def is_mock_batch(batch: Optional[dict]) -> bool:
    return bool(batch) and batch.get("source_kind") == "mock"


def _hard_fail(cand: dict) -> List[str]:
    return [f"{c['code']}: {c['message']}" for c in cand["quality_report"]["checks"]
            if c["hard_gate"] and c["outcome"] != "pass"]


def evaluate(work_id: str, cand: dict, batch: dict, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {review_state, pickable, reasons, review_id, set_review_id, code}。"""
    reasons: List[str] = []
    hard = _hard_fail(cand)
    if hard:
        reasons += [f"硬门禁未过：{h}" for h in hard]
    if is_mock_batch(batch):
        return {"review_state": "passed" if not hard else "failed", "pickable": not hard, "reasons": reasons,
                "review_id": None, "set_review_id": None, "code": "SCRIPT_QUALITY_FAILED" if hard else None}
    members = [{"slot": c["slot"], "candidate_id": c["id"], "text_digest": cc.candidate_digest(c)}
               for c in batch["candidates"] if c.get("slot") is not None]
    single = sr.current_single(work_id, cand, ctx)
    any_old = [d for d in sr.decisions_for(work_id) if d["subject_id"] == cand["id"]]
    code = None
    if single is None:
        state = "stale" if any_old else "needs_review"
        reasons.append("这一稿当前没有有效的独立审核（正文或创作输入已变化，或从未审核）：需要重新审核")
        code = "SCRIPT_REVIEW_REQUIRED"
    elif single["recommendation"] != "pass":
        state = "failed"
        reasons += [f"审稿未通过：{f['issue']}" for f in single["findings"] if f.get("issue")] or ["独立审稿未通过"]
        code = "SCRIPT_QUALITY_FAILED"
    else:
        state = "passed"
    set_dec = sr.current_set(work_id, members, ctx) if len(members) == len(batch["candidates"]) else None
    if set_dec is None:
        reasons.append("这一组候选没有有效的批次差异审核：需要重新审核")
        code = code or "SCRIPT_REVIEW_REQUIRED"
        if state == "passed":
            state = "needs_review"
    elif set_dec["recommendation"] != "pass":
        slots = (set_dec.get("reviewer") or {}).get("rewrite_slots") or []
        if not slots or cand.get("slot") in slots:
            reasons.append("批次差异审核未通过：" + ("；".join(f["issue"] for f in set_dec["findings"] if f.get("issue")) or "候选之间差异不足"))
            code = code or "SCRIPT_QUALITY_FAILED"
            if state == "passed":
                state = "failed"
    if hard and code is None:
        code = "SCRIPT_QUALITY_FAILED"
    if len(batch["candidates"]) != 3 or {c.get("slot") for c in batch["candidates"]} != {1, 2, 3}:
        reasons.append("必须凑齐三篇不同候选并完成审核")
        code = code or "SCRIPT_REVIEW_REQUIRED"
    elif not all(_candidate_review_passes(work_id, c, ctx) for c in batch["candidates"]) or \
            set_dec is None or set_dec["recommendation"] != "pass":
        reasons.append("三篇候选必须全部通过单稿审核和集合审核")
        code = code or "SCRIPT_REVIEW_REQUIRED"
    pickable = not reasons and state == "passed"
    return {"review_state": state, "pickable": pickable, "reasons": reasons,
            "review_id": single["id"] if single else None, "set_review_id": set_dec["id"] if set_dec else None,
            "code": None if pickable else (code or "SCRIPT_REVIEW_REQUIRED")}


def _candidate_review_passes(work_id: str, cand: dict, ctx: Dict[str, Any]) -> bool:
    dec = sr.current_single(work_id, cand, ctx)
    return not _hard_fail(cand) and dec is not None and dec["recommendation"] == "pass"


def project_batch(work_id: str) -> Optional[dict]:
    """GET scripts 的投影：在落库批次上叠加 review_state/pickable/unpickable_reasons/passed。"""
    batch = st.get("script_batches", work_id)
    if not batch:
        return None
    ctx = cx.load_context(work_id)
    passed = 0
    cands = []
    for cand in batch["candidates"]:
        ev = evaluate(work_id, cand, batch, ctx)
        cand = dict(cand, review_state=ev["review_state"], pickable=ev["pickable"], unpickable_reasons=ev["reasons"])
        passed += 1 if _candidate_review_passes(work_id, cand, ctx) else 0
        cands.append(cand)
    ready = (len(cands) == 3 and {c.get("slot") for c in cands} == {1, 2, 3}
             and all(c["pickable"] for c in cands))
    targets = [{"candidate_id": c["id"], "slot": c.get("slot"), "review_state": c["review_state"],
                "reason": ("单稿审核未通过，请定向返工" if c["review_state"] == "failed"
                           else "单稿尚未完成有效审核")}
               for c in cands if not _candidate_review_passes(work_id, c, ctx)]
    if not ready and not targets:
        targets = [{"candidate_id": c["id"], "slot": c.get("slot"), "review_state": c["review_state"],
                    "reason": "请补齐三篇候选并完成集合审核"} for c in cands]
    out = dict(batch, candidates=cands if ready or is_mock_batch(batch) else [],
               ready_to_present=ready, retry_targets=targets)
    if batch.get("requested") is not None:
        out["passed"] = passed
        out["produced"] = len(cands)
    return out


def _find_candidate(batch: Optional[dict], script_id: str) -> Optional[dict]:
    if not batch:
        return None
    return next((c for c in batch["candidates"] if c["id"] == script_id), None)


def current_selection_valid(work_id: str) -> bool:
    """当前是否有一份仍然有效的选用：历史 selected 记录不算，必须与 work 指针一致且当前仍可选。"""
    work = st.get("works", work_id) or {}
    sid = work.get("script_version_id")
    if not sid:
        return False
    sels = [s for s in st.all_rows("selections", "work_id=?", [work_id])
            if s.get("kind") == "script" and s.get("state") == "selected"]
    if not any(s.get("selected_ref") == sid for s in sels):
        return False
    batch = st.get("script_batches", work_id)
    cand = _find_candidate(batch, sid)
    if cand is None:
        return False
    if is_mock_batch(batch):
        return not _hard_fail(cand)
    return evaluate(work_id, cand, batch, cx.load_context(work_id))["pickable"]


def _selection_rows(c, work_id: str) -> List[dict]:
    return [json.loads(r["data"]) for r in c.execute(
        "SELECT data FROM selections WHERE work_id=?", [work_id]).fetchall()]


def _reselect_task(c, work: dict, message: str) -> None:
    task_row = c.execute("SELECT data FROM tasks WHERE id=?", [work.get("current_task_id") or ""]).fetchone()
    if not task_row:
        return
    task = json.loads(task_row["data"])
    if task.get("stage") in ("topic", "script"):
        return
    task.update(stage="content_review", state="waiting_selection")
    task["pending_selection_ids"] = list(dict.fromkeys([*task.get("pending_selection_ids", []), "sel_script"]))
    st.insert_row(c, "tasks", task["id"], task, {"work_id": work["id"]})
    st.add_event_tx(c, task["id"], {"type": "selection.revoked", "occurred_at": now_iso(), "stage": "content_review",
                                    "state": "waiting_selection", "message": message})


def revoke_adoption(c, work: dict, reason: str) -> bool:
    """事务内撤销当前选用：清指针、标记历史选择为 revoked、下游失效（保留文件与费用）、任务回到重新选稿。
    work 为调用方已读到的字典，本函数会写回。"""
    sid = work.get("script_version_id")
    if not sid:
        return False
    for s in _selection_rows(c, work["id"]):
        if s.get("kind") == "script" and s.get("state") == "selected":
            s.update(state="revoked", revoked_at=now_iso(), revoked_reason=reason)
            st.insert_row(c, "selections", s["id"], s, {"work_id": work["id"]})
    work["script_version_id"] = None
    from ..tts import gate
    gate._invalidate_inputs(work, reason, "script_text", c)
    _reselect_task(c, work, f"{reason}：已撤销原选用，需重新采用稿件")
    return True


def on_batch_replaced(c, work_id: str) -> None:
    """当前批次被新的候选集合替换：旧选用与其审核依据一并撤销。"""
    row = c.execute("SELECT data FROM works WHERE id=?", [work_id]).fetchone()
    if row:
        work = json.loads(row["data"])
        if work.get("script_version_id"):
            revoke_adoption(c, work, "候选稿件集合已更换")


def adopt(work_id: str, script_id: str, *, expected_revision: Optional[int] = None,
          review_id: Optional[str] = None, set_review_id: Optional[str] = None) -> Dict[str, Any]:
    with st.transaction() as c:
        wrow = c.execute("SELECT data FROM works WHERE id=?", [work_id]).fetchone()
        if not wrow:
            raise SelectionRejected("作品不存在", "WORK_NOT_FOUND", 404)
        work = json.loads(wrow["data"])
        if expected_revision is not None and int(work.get("content_revision") or 0) != int(expected_revision):
            raise SelectionRejected("创作输入已变化，请刷新后重新选择", "CONTENT_REVISION_STALE")
        batch = st.get("script_batches", work_id)
        if not batch:
            raise SelectionRejected("尚未生成候选稿件", "SELECTION_REQUIRED")
        cand = _find_candidate(batch, script_id)
        if cand is None:
            raise SelectionRejected("稿件不存在或已被新版本取代", "STALE_SELECTION")
        ev = evaluate(work_id, cand, batch, cx.load_context(work_id))
        if not ev["pickable"]:
            raise SelectionRejected("；".join(ev["reasons"]) or "该稿当前不可采用", ev["code"] or "SCRIPT_REVIEW_REQUIRED")
        if review_id and review_id != ev["review_id"]:
            raise SelectionRejected("提交的单稿审核已被新的审核或改稿取代", "STALE_SELECTION")
        if set_review_id and set_review_id != ev["set_review_id"]:
            raise SelectionRejected("提交的批次审核已被取代", "STALE_SELECTION")
        recorded = any(s.get("kind") == "script" and s.get("state") == "selected" and s.get("selected_ref") == script_id
                       for s in _selection_rows(c, work_id))
        if work.get("script_version_id") == script_id and recorded:
            return {"ok": True, "message": f"已采用：{cand['title']}", "title": cand["title"], "task_id": None,
                    "advance": False, "unchanged": True}
        if work.get("script_version_id") and work.get("script_version_id") != script_id:
            revoke_adoption(c, work, "改选其他稿件")
        for s in _selection_rows(c, work_id):
            if s.get("kind") == "script" and s.get("state") == "selected":
                s.update(state="revoked", revoked_at=now_iso(), revoked_reason="改选其他稿件")
                st.insert_row(c, "selections", s["id"], s, {"work_id": work_id})
        digest = cc.candidate_digest(cand)
        req = SelectionRequest(id=st.new_id("selreq"), work_id=work_id, kind="script", prompt="请选择script",
                               options=[SelectionOption(option_id=f"opt_{script_id}", kind="script", ref_id=script_id,
                                                        label=script_id, digest=digest)])
        st.insert_row(c, "selections", req.id, dict(req.model_dump(), state="decided"), {"work_id": work_id})
        dec = SelectionDecision(id=st.new_id("sel"), request_id=req.id, work_id=work_id, kind="script",
                                selected_option_id=f"opt_{script_id}", selected_ref=script_id,
                                selected_digest=digest, reason="")
        st.insert_row(c, "selections", dec.id, dict(dec.model_dump(), state="selected",
                                                    review_id=ev["review_id"], set_review_id=ev["set_review_id"],
                                                    content_revision=int(work.get("content_revision") or 0)),
                      {"work_id": work_id})
        work["script_version_id"] = script_id
        work["version"] = int(work.get("version") or 1) + 1
        st.insert_row(c, "works", work_id, work)
        advance = False
        task_id = work.get("current_task_id")
        trow = c.execute("SELECT data FROM tasks WHERE id=?", [task_id or ""]).fetchone()
        if trow:
            task = json.loads(trow["data"])
            task["pending_selection_ids"] = [s for s in task.get("pending_selection_ids", []) if s != "sel_script"]
            if task.get("stage") == "content_review":
                storyboard_route = (work.get("brief") or {}).get("content_route") == "short_video_storyboard"
                task.update(stage="content_review" if storyboard_route else "avatar_variant",
                            state="waiting_review" if storyboard_route else "running")
                advance = not storyboard_route
                st.add_event_tx(c, task["id"], {"type": "selection.decided", "occurred_at": now_iso(),
                                                "stage": task["stage"], "state": task["state"],
                                                "message": f"已采用稿件：{cand['title']}"})
            st.insert_row(c, "tasks", task["id"], task, {"work_id": work_id})
    return {"ok": True, "message": f"已采用：{cand['title']}", "title": cand["title"], "task_id": task_id if trow else None,
            "advance": advance, "unchanged": False}
