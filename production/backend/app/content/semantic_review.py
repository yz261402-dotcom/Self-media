"""独立语义审核：结构化评价、正文引用校验、绑定摘要与“审核是否仍有效”的复算。

审核方是与写稿方独立的模型调用（reviewer 角色），结论存为 quality_decisions（independent=True）。
旧规则型/空 checks 结论没有 reviewer 与 binding，永远不满足 is_independent_review。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from .. import storage as st
from ..agents import contracts as ac
from . import contracts as cc
from . import prompts
from .errors import ContentRejected
from .providers import text as text_provider

_MARK = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _MARK.sub("", s or "")


def reviewer_profile() -> Dict[str, str]:
    p = text_provider.profile("reviewer")
    return {"provider": p["provider"], "model": p["model"]}


def single_binding(version: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    prompt = ctx["prompt"] or {}
    rv = reviewer_profile()
    return {
        "candidate_id": version["id"], "text_digest": cc.candidate_digest(version),
        "content_route": ctx["brief_core"]["content_route"],
        "reference_digest": ctx["digests"]["reference"], "evidence_digest": ctx["digests"]["evidence"],
        "brief_digest": ctx["digests"]["brief"], "prompt_id": prompt.get("id", ""),
        "prompt_revision": int(prompt.get("version") or 0), "style_digest": ctx["digests"]["style"],
        "identity_digest": ctx["digests"].get("identity", ""),
        "content_revision": ctx["content_revision"], "rubric_version": prompts.RUBRIC_VERSION,
        "reviewer_provider": rv["provider"], "reviewer_model": rv["model"],
    }


def set_binding(members: Sequence[Dict[str, Any]], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """members: [{slot, candidate_id, text_digest}]"""
    base = single_binding({"id": "", "text": ""}, ctx)
    for k in ("candidate_id", "text_digest"):
        base.pop(k)
    ordered = sorted(({"slot": int(m["slot"]), "candidate_id": m["candidate_id"], "text_digest": m["text_digest"]}
                      for m in members), key=lambda m: (m["slot"], m["candidate_id"]))
    base["set_digest"] = cc.set_digest(ordered)
    base["members"] = ordered
    return base


def decisions_for(work_id: str) -> List[Dict[str, Any]]:
    return [d for d in st.all_rows("quality_decisions", "work_id=?", [work_id])
            if d.get("independent") and d.get("review_kind") in ("semantic_single", "semantic_set")]


def _valid(decision: Dict[str, Any], expected: Dict[str, Any]) -> bool:
    return cc.is_independent_review(decision) and decision.get("binding") == expected


def current_single(work_id: str, version: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """当前绑定下最新的一条单稿独立审核（结论可 pass 可 fail）；绑定不符即视为不存在。"""
    expected = single_binding(version, ctx)
    found = [d for d in decisions_for(work_id) if d["review_kind"] == "semantic_single"
             and d["subject_id"] == version["id"] and _valid(d, expected)]
    return sorted(found, key=lambda d: d["created_at"])[-1] if found else None


def current_set(work_id: str, members: Sequence[Dict[str, Any]], ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    expected = set_binding(members, ctx)
    found = [d for d in decisions_for(work_id) if d["review_kind"] == "semantic_set" and _valid(d, expected)]
    return sorted(found, key=lambda d: d["created_at"])[-1] if found else None


def _decision(*, work_id: str, kind: str, subject_id: str, subject_digest: str, passed: bool, binding: Dict[str, Any],
              findings: List[Dict[str, Any]], reviewer: Dict[str, Any], key: str) -> Dict[str, Any]:
    d = ac.QualityDecision(
        id="qd_" + cc.digest([key, kind, subject_id, binding])[7:23], work_item_id="", subject_id=subject_id,
        subject_digest=subject_digest, recommendation="pass" if passed else "fail",
        ruleset_version=prompts.RUBRIC_VERSION, independent=True, review_kind=kind, reviewer=reviewer,
        binding=binding, findings=findings, work_id=work_id,
        issues=[{"code": f.get("criterion", "REVIEW"), "reason": f.get("issue", ""), "rework_target": "content"}
                for f in findings if not passed])
    d.decision_digest = ac.digest({"subject": subject_digest, "binding": binding, "recommendation": d.recommendation})
    return d.model_dump()


def parse_review(data: Dict[str, Any], *, review_ids: Sequence[str], versions: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """严格校验审稿输出：覆盖恰好 review_ids、六项判定齐全、引用真实存在、slot 合法。任何缺陷整体拒绝，不放行。"""
    by_id = {v["id"]: v for v in versions}
    raw = data.get("candidates")
    if not isinstance(raw, list):
        raise ContentRejected("审稿结果缺少 candidates 列表", "REVIEW_INVALID")
    seen: List[str] = []
    singles: Dict[str, Dict[str, Any]] = {}
    for item in raw:
        cid = item.get("candidate_id") if isinstance(item, dict) else None
        if cid not in review_ids:
            raise ContentRejected("审稿结果包含未请求或未知的 candidate_id", "REVIEW_INVALID")
        if cid in seen:
            raise ContentRejected("审稿结果重复评价了同一稿", "REVIEW_INVALID")
        seen.append(cid)
        crit = item.get("criteria")
        criteria = (prompts.STORYBOARD_CRITERIA if by_id[cid].get("content_route") == "short_video_storyboard"
                    else prompts.CRITERIA)
        if not isinstance(crit, dict) or set(crit) != set(criteria) \
                or any(v not in ("pass", "fail") for v in crit.values()):
            raise ContentRejected("审稿结果的判定项不完整或取值非法", "REVIEW_INVALID")
        findings = item.get("findings") or []
        if not isinstance(findings, list):
            raise ContentRejected("findings 必须是列表", "REVIEW_INVALID")
        body = _norm(by_id[cid]["text"])
        clean = []
        for f in findings:
            if not isinstance(f, dict):
                raise ContentRejected("finding 格式非法", "REVIEW_INVALID")
            quote = str(f.get("quote") or "")
            if quote and _norm(quote) not in body:
                raise ContentRejected("审稿引用了正文里不存在的文字，整份审稿作废", "REVIEW_QUOTE_INVALID")
            clean.append({"quote": quote, "issue": str(f.get("issue") or ""), "suggestion": str(f.get("suggestion") or ""),
                          "criterion": str(f.get("criterion") or "")})
        failed = [k for k, v in crit.items() if v == "fail"]
        if failed and not any(f["issue"] for f in clean):
            clean.append({"quote": "", "issue": "未通过：" + "、".join(failed), "suggestion": "按未通过项修改",
                          "criterion": failed[0]})
        singles[cid] = {"passed": not failed, "criteria": crit, "findings": clean, "failed": failed}
    if sorted(seen) != sorted(review_ids):
        raise ContentRejected("审稿结果没有覆盖全部待审稿件，缺失的稿不放行", "REVIEW_INVALID")
    s = data.get("set")
    if not isinstance(s, dict) or not isinstance(s.get("distinct"), bool):
        raise ContentRejected("审稿结果缺少批次差异结论", "REVIEW_INVALID")
    slots = {int(v["slot"]) for v in versions}
    rewrite = s.get("rewrite_slots") or []
    if not isinstance(rewrite, list) or any((not isinstance(x, int)) or x not in slots for x in rewrite):
        raise ContentRejected("rewrite_slots 含未知 slot", "REVIEW_INVALID")
    sfind = [{"issue": str(f.get("issue") or ""), "suggestion": str(f.get("suggestion") or "")}
             for f in (s.get("findings") or []) if isinstance(f, dict)]
    return {"singles": singles,
            "set": {"passed": bool(s["distinct"]) and not rewrite, "rewrite_slots": sorted(set(rewrite)),
                    "findings": sfind}}


def review_batch(*, parent: Dict[str, Any], ctx: Dict[str, Any], versions: Sequence[Dict[str, Any]],
                 review_ids: Sequence[str], round_no: int, transport=None) -> Dict[str, Any]:
    """一次 reviewer 调用：逐稿（review_ids）+ 批次差异。结果落 quality_decisions，返回解析结果。"""
    work_id = ctx["work"]["id"]
    evidence_by_version = {v["id"]: [
        {"claim": e["claim"], "evidence_id": e["evidence_id"], "source_quote": e["source_quote"]}
        for e in (v.get("origin") or {}).get("evidence", []) if e.get("verified")] for v in versions}
    cand_payload = [{"candidate_id": v["id"], "slot": v["slot"], "angle": v["angle"], "title": v["title"],
                     "text": v["text"], "storyboard": v.get("storyboard") or [], "evidence": evidence_by_version[v["id"]]}
                    for v in sorted(versions, key=lambda x: x["slot"])]
    system, user = prompts.reviewer_messages(
        brief=ctx["brief_core"], reference=ctx["reference"], style=ctx["style"], materials=ctx["materials"],
        candidates=cand_payload, review_ids=list(review_ids), identity=ctx.get("identity"))
    res = text_provider.call_text("reviewer", parent=parent, step="review", round_no=round_no,
                                  system=system, user=user, transport=transport)
    parsed = parse_review(res["json"], review_ids=list(review_ids), versions=versions)
    reviewer = {"provider": res["provider"], "model": res["model"], "job_id": res["job_id"],
                "provider_request_id": res["provider_request_id"], "rubric_version": prompts.RUBRIC_VERSION}
    by_id = {v["id"]: v for v in versions}
    key = f"{parent['id']}:{round_no}"
    with st.transaction() as c:
        for cid, r in parsed["singles"].items():
            d = _decision(work_id=work_id, kind="semantic_single", subject_id=cid,
                          subject_digest=cc.candidate_digest(by_id[cid]), passed=r["passed"],
                          binding=single_binding(by_id[cid], ctx),
                          findings=r["findings"], reviewer=dict(reviewer, criteria=r["criteria"]), key=key)
            st.insert_row(c, "quality_decisions", d["id"], d, {"work_id": work_id})
        members = [{"slot": v["slot"], "candidate_id": v["id"], "text_digest": cc.candidate_digest(v)} for v in versions]
        sb = set_binding(members, ctx)
        d = _decision(work_id=work_id, kind="semantic_set", subject_id=sb["set_digest"], subject_digest=sb["set_digest"],
                      passed=parsed["set"]["passed"], binding=sb,
                      findings=parsed["set"]["findings"] + [{"issue": f"建议重写 slot {s}", "suggestion": ""}
                                                            for s in parsed["set"]["rewrite_slots"]],
                      reviewer=dict(reviewer, rewrite_slots=parsed["set"]["rewrite_slots"]), key=key)
        st.insert_row(c, "quality_decisions", d["id"], d, {"work_id": work_id})
    return parsed
