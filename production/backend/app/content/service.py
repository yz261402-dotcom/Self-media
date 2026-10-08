"""口播稿升级的唯一业务入口：准备提示、保存提示修订、三稿生成/审核/有限返工、定向返工。

API、旧入口和 Agent 工具都调用这里；不在别处再造一份生成或审稿。
网络请求只经 providers.text.call_text（进台账），事务内不做网络请求。
"""
from __future__ import annotations

from contextlib import nullcontext

import difflib
import json
import math
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import storage as st
from ..contracts import Claim, QualityCheck, QualityReport, ScriptVersion, now_iso
from . import contracts as cc
from . import context as cx
from . import costs, prompts, semantic_review as sr, tasks
from . import repository as repo
from .dedup import dedup_report, text_similarity
from .errors import ContentBlocked, ContentError, ContentRejected
from .providers import text as tp
from .review import current_storyboard_quality_report, review_candidate, storyboard_review_texts

EST_CHARS_PER_SEC = 4.0
TARGET_COUNT = 3
SAME_DRAFT_THRESHOLD = 0.72
REUSE_MIN_CHARS = 18  # 与参考转写的连续相同字数达到此值即视为复用参考表达


def estimate_duration_ms(text: str) -> int:
    return int(round(len(re.sub(r"\s+", "", text or "")) / EST_CHARS_PER_SEC * 1000))


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _storyboard(raw: Any, target_duration_ms: int) -> Tuple[List[Dict[str, Any]], str, int]:
    if not isinstance(raw, list) or not raw:
        raise ContentRejected("分镜剧本必须包含至少一个镜头", "STORYBOARD_INVALID")
    shots = []
    for index, shot in enumerate(raw, 1):
        if not isinstance(shot, dict) or type(shot.get("shot_index")) is not int or shot["shot_index"] != index:
            raise ContentRejected("分镜镜头编号必须从 1 连续递增", "STORYBOARD_INVALID")
        duration = shot.get("duration_seconds")
        if type(duration) is float and math.isfinite(duration) and duration.is_integer():
            duration = int(duration)
        if type(duration) is not int or not 2 <= duration <= 30:
            raise ContentRejected("每个镜头时长必须是 2 到 30 秒的整数", "STORYBOARD_INVALID")
        fields = {}
        for key in ("visual", "dialogue", "sound_effects"):
            value = shot.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ContentRejected(f"镜头 {index} 缺少 {key}", "STORYBOARD_INVALID")
            fields[key] = value.strip()
        prompt = shot.get("prompt") or f"{fields['visual']}；{fields['dialogue']}；音效：{fields['sound_effects']}"
        if not isinstance(prompt, str) or not prompt.strip():
            raise ContentRejected(f"镜头 {index} 的 prompt 必须为文本", "STORYBOARD_INVALID")
        shots.append({"shot_index": index, **fields, "duration_seconds": duration, "prompt": prompt.strip()})
    total_ms = round(sum(s["duration_seconds"] for s in shots) * 1000)
    if target_duration_ms and abs(total_ms - target_duration_ms) > max(5000, target_duration_ms * 0.2):
        raise ContentRejected("分镜总时长与目标时长偏差超过 20%", "STORYBOARD_INVALID")
    text = "\n".join(f"镜头{s['shot_index']}：画面{s['visual']}；台词/旁白{s['dialogue']}；音效{s['sound_effects']}"
                     for s in shots)
    return shots, text, total_ms


# ---------------------------------------------------------------- 提示

def _validate_plan(data: Dict[str, Any], transcript: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str]:
    analysis = data.get("reference_analysis")
    plan = data.get("angle_plan")
    prompt_text = str(data.get("prompt_text") or "").strip()
    if not isinstance(analysis, dict) or not isinstance(plan, list) or not prompt_text:
        raise ContentRejected("规划结果缺少拆解、角度计划或写作提示", "PLAN_INVALID")
    slots = [p.get("slot") for p in plan if isinstance(p, dict)]
    angles = [str(p.get("angle") or "").strip() for p in plan if isinstance(p, dict)]
    if sorted(slots) != [1, 2, 3] or len(set(angles)) != 3 or not all(angles):
        raise ContentRejected("角度计划必须恰好是 slot 1/2/3 且角度互不相同", "PLAN_INVALID")
    body = _norm(transcript)
    kept = []
    for d in analysis.get("do_not_reuse") or []:
        frag = str(d.get("fragment") or "") if isinstance(d, dict) else str(d)
        if frag and _norm(frag) in body:  # 只保留确实出现在转写里的片段，不采信模型杜撰的“禁用片段”
            kept.append({"fragment": frag, "reason": str(d.get("reason") or "") if isinstance(d, dict) else ""})
    analysis = dict(analysis, do_not_reuse=kept)
    return analysis, [dict(p, slot=int(p["slot"])) for p in sorted(plan, key=lambda p: p["slot"])], prompt_text


def handle_prepare_prompt(job: dict, *, transport=None) -> None:
    ctx = cx.load_context(job["work_id"], require_reference=True)
    if not ctx["brief_core"]["topic"]:
        raise ContentRejected("缺少主题，不能生成写作提示", "TOPIC_REQUIRED")
    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="plan")
    system, user = prompts.planner_messages(brief=ctx["brief_core"], reference=ctx["reference"], style=ctx["style"],
                                            materials=ctx["materials"], identity=ctx.get("identity"))
    res = tp.call_text("planner", parent=job, step="plan", system=system, user=user, transport=transport)
    analysis, plan, prompt_text = _validate_plan(res["json"], ctx["reference"]["transcript"]["text"])
    prev = repo.list_prompts(job["work_id"])
    doc = {
        "id": "prm_" + job["id"], "work_id": job["work_id"], "reference_snapshot_id": ctx["reference"]["id"],
        "reference_digest": ctx["digests"]["reference"], "brief_digest": ctx["digests"]["brief"],
        "style_digest": ctx["digests"]["style"], "version": len(prev) + 1, "source": "generated",
        "identity_snapshot": ctx.get("identity") or {},
        "reference_analysis": analysis, "angle_plan": plan, "prompt_text": prompt_text,
        "template_version": prompts.TEMPLATE_VERSION, "content_revision": job["content_revision"],
        "created_at": now_iso(),
        "model_profile": {"provider": res["provider"], "model": res["model"], "job_id": res["job_id"],
                          "evidence_digest": ctx["digests"]["evidence"]}}

    def apply(c):
        saved = repo.save_prompt(c, doc)
        repo.bump_content_revision(c, job["work_id"], writing_prompt_id=saved["id"])

    with st.transaction() as c:  # 提示本身不可变，先存；绑定在 finish 的 apply 里做（仅当任务仍是当前）
        repo.save_prompt(c, doc)
    tasks.finish_job(job["id"], "succeeded", result_refs={"prompt_id": doc["id"], "version": doc["version"]}, apply=apply)


def handle_prompt_revision(job: dict, *, transport=None) -> None:
    p = job["payload"]
    base = repo.get_prompt(p.get("prompt_id") or "")
    text = str(p.get("prompt_text") or "").strip()
    if base is None or base["work_id"] != job["work_id"]:
        raise ContentRejected("要修改的提示不存在或不属于该作品", "PROMPT_NOT_FOUND")
    if not text:
        raise ContentRejected("写作提示不能为空", "VALIDATION_FAILED")
    # 直接创作的提示没有参考快照，编辑时仍需校验主题/风格/资料版本，
    # 但不能把“无参考”误当成缺少参考。参考创作提示则继续要求当前参考可用。
    base_reference_id = str(base.get("reference_snapshot_id") or "")
    ctx = cx.load_context(job["work_id"], require_reference=bool(base_reference_id))
    current_reference_id = ctx["reference"]["id"] if ctx["reference"] is not None else ""
    if base_reference_id != current_reference_id:
        raise ContentRejected("该提示基于另一个参考，不能再编辑；请为当前参考重新准备提示", "PROMPT_STALE")
    prev = repo.list_prompts(job["work_id"])
    doc = dict(base, id="prm_" + job["id"], parent_id=base["id"], version=len(prev) + 1, source="edited",
               prompt_text=text, content_revision=job["content_revision"], brief_digest=ctx["digests"]["brief"],
               style_digest=ctx["digests"]["style"],
               identity_snapshot=ctx.get("identity") or {},
               model_profile=dict(base.get("model_profile") or {}, evidence_digest=ctx["digests"]["evidence"]))
    doc["created_at"] = now_iso()

    def apply(c):
        saved = repo.save_prompt(c, doc)
        repo.bump_content_revision(c, job["work_id"], writing_prompt_id=saved["id"])

    with st.transaction() as c:
        repo.save_prompt(c, doc)
    tasks.finish_job(job["id"], "succeeded", result_refs={"prompt_id": doc["id"], "version": doc["version"]}, apply=apply)


# ---------------------------------------------------------------- 候选处理

def _reuse_findings(text: str, ctx: Dict[str, Any]) -> List[str]:
    ref_text = _norm(((ctx["reference"] or {}).get("transcript") or {}).get("text", ""))
    body = _norm(text)
    out = []
    if ref_text and body:
        m = difflib.SequenceMatcher(None, body, ref_text, autojunk=False).find_longest_match(0, len(body), 0, len(ref_text))
        if m.size >= REUSE_MIN_CHARS:
            out.append(f"与参考转写有 {m.size} 字连续相同：「{body[m.a:m.a + min(m.size, 30)]}…」")
    for d in ((ctx["prompt"] or {}).get("reference_analysis") or {}).get("do_not_reuse") or []:
        frag = _norm(d.get("fragment", ""))
        if frag and frag in body:
            out.append(f"出现了被禁用的参考片段：「{d['fragment'][:30]}」")
    return out


def _verify_claims(raw_claims: Any, text: str, materials: List[Dict[str, Any]]) -> Tuple[List[Claim], List[Dict[str, Any]], List[str]]:
    """逐条校验断言的依据：evidence_id 必须存在、source_quote 必须是该资料的精确原文、数字必须一致。
    返回 (Claim 列表, 证据留痕, 问题)。verdict 只由本函数按资料复算，模型自报无效。"""
    if raw_claims is None:
        raw_claims = []
    if not isinstance(raw_claims, list):
        raise ContentRejected("claims 必须是列表", "CANDIDATE_INCOMPLETE")
    by_id = {m["evidence_id"]: m for m in materials}
    claims: List[Claim] = []
    evidence: List[Dict[str, Any]] = []
    problems: List[str] = []
    body = _norm(text)
    for raw in raw_claims:
        if isinstance(raw, str):
            raw = {"text": raw}
        if not isinstance(raw, dict) or not str(raw.get("text") or "").strip():
            continue
        ctext = str(raw["text"]).strip()
        position = str(raw.get("position") or "").strip()
        quote = str(raw.get("source_quote") or "").strip()
        ev_id = raw.get("evidence_id")
        verified, reason = False, "没有给出资料依据"
        if ev_id:
            mat = by_id.get(ev_id)
            if mat is None:
                reason = f"引用了不存在的资料 {ev_id}"
            elif not quote or _norm(quote) not in _norm(mat["text"]):
                reason = "source_quote 不是该资料里的原文"
            else:
                nums = re.findall(r"\d+(?:\.\d+)?", ctext)
                if any(n not in quote and n not in mat["text"] for n in nums):
                    reason = "断言里的数字与资料不一致"
                else:
                    verified, reason = True, "资料原文支持"
        if position and _norm(position) not in body:
            problems.append(f"断言「{ctext[:20]}」标注的正文位置不在正文里")
            verified, reason = False, "正文位置不存在"
        display = ctext if not position or _norm(position) in _norm(ctext) else f"{ctext}（正文：{position}）"
        claims.append(Claim(id=st.new_id("claim"), text=display, verdict="supported" if verified else "unverified"))
        evidence.append({"claim": ctext, "evidence_id": ev_id, "source_quote": quote, "position": position,
                         "verified": verified, "reason": reason})
        if not verified:
            problems.append(f"断言「{ctext[:24]}」缺少有效依据：{reason}")
    return claims, evidence, problems


def _build_version(raw: Dict[str, Any], *, ctx: Dict[str, Any], job: dict, slot: int, parent_id: Optional[str],
                   round_no: int, writer: Dict[str, Any], angle: Optional[str] = None,
                   title: Optional[str] = None, description: Optional[str] = None,
                   tags: Optional[list] = None) -> Dict[str, Any]:
    route = ctx["brief_core"]["content_route"]
    if (route == "talking_script" and not isinstance(raw.get("text"), str)) or not (angle or isinstance(raw.get("angle"), str)) \
            or not (title or isinstance(raw.get("title"), str)):
        raise ContentRejected("候选的 angle/title/text 必须为文本", "CANDIDATE_INCOMPLETE")
    if route == "short_video_storyboard":
        storyboard, text, dur = _storyboard(raw.get("storyboard"), ctx["brief_core"]["target_duration_ms"])
    else:
        storyboard, text = [], raw["text"].strip()
        dur = estimate_duration_ms(text)
    angle = (angle or raw["angle"]).strip()
    title = (title or raw["title"]).strip()
    if not (text and angle and title):
        raise ContentRejected("候选缺字段（angle/title/text）", "CANDIDATE_INCOMPLETE")
    claims, evidence, problems = _verify_claims(raw.get("claims"), text, ctx["materials"])
    brief = ctx["brief_core"]
    dedup = dedup_report(job["work_id"], text, title, brief["topic"])
    structured_review = storyboard_review_texts(storyboard) if route == "short_video_storyboard" else {}
    report = review_candidate(text=text, title=title, claims=claims, dedup=dedup, materials=ctx["materials"],
                              target_duration_ms=brief["target_duration_ms"], duration_estimate_ms=dur,
                              require_hook=False, **structured_review)
    checks = list(report.checks)
    reuse = _reuse_findings(text, ctx)
    checks.append(QualityCheck(code="REFERENCE_REUSE", outcome="fail" if reuse else "pass", hard_gate=True,
                               message="；".join(reuse) if reuse else "未复用参考转写的连续表达"))
    bad_ev = [p for p in problems if "正文位置" in p or "不存在的资料" in p or "原文" in p or "数字" in p]
    checks.append(QualityCheck(code="EVIDENCE_LINK", outcome="fail" if bad_ev else "pass", hard_gate=True,
                               message="；".join(bad_ev) if bad_ev else "断言与资料引用均可复核"))
    hard_fail = any(c.hard_gate and c.outcome == "fail" for c in checks)
    review_required = any(c.outcome == "review" for c in checks) or any(c.outcome == "fail" for c in checks)
    overall = "fail" if hard_fail else ("review" if review_required else "pass")
    return {
        "id": st.new_id("script"), "work_id": job["work_id"], "slot": slot, "parent_id": parent_id,
        "content_route": route, "storyboard": storyboard,
        "angle": angle, "title": title, "description": (description if description is not None
                                                       else str(raw.get("description") or "").strip()) or f"{title}｜{angle}",
        "tags": [str(t) for t in (tags if tags is not None else raw.get("tags") or [])][:8], "text": text,
        "duration_estimate_ms": dur, "claims": [c.model_dump() for c in claims],
        "quality_report": QualityReport(checks=checks, overall=overall, review_required=review_required).model_dump(),
        "origin": {"job_id": job["id"], "round": round_no, "prompt_id": (ctx["prompt"] or {}).get("id"),
                   "identity_snapshot": ctx.get("identity") or {},
                   "writer": writer, "evidence": evidence, "evidence_problems": problems,
                   "created_at": now_iso()},
    }


def _rule_problems(version: Dict[str, Any]) -> List[str]:
    out = [f"[{c['code']}] {c['message']}" for c in version["quality_report"]["checks"]
           if (c["hard_gate"] or (version.get("content_route") != "short_video_storyboard"
               and c["code"] in ("READABILITY", "DURATION_FIT"))) and c["outcome"] != "pass"]
    return out


def _hard_ok(version: Dict[str, Any]) -> bool:
    return not any(c["hard_gate"] and c["outcome"] != "pass" for c in version["quality_report"]["checks"])


# ---------------------------------------------------------------- 三稿生成

def _assert_writer_runtime(job: dict) -> None:
    frozen = job.get("writer_runtime_profiles")
    current = {role: tp.runtime_profile(role)["profile_id"] for role in ("writer", "repair")}
    changed = frozen is not None and frozen != current
    if frozen is None and any(tp.profile(role)["execution_mode"] == "claude_code" for role in ("writer", "repair")):
        changed = any(c.get("provider") == "anthropic" and c.get("step") in ("write", "repair")
                      and c.get("attempts", 0) > 0 for c in tasks.jobs_calls(job["id"]))
    if changed:
        raise ContentBlocked("写稿线路已变化，原任务保持原记录。请核对后从当前提示新建一次写稿任务。",
                             "WRITER_PROFILE_CHANGED", retryable=False)


def _writer_call(job: dict, ctx: Dict[str, Any], system: str, user: str, round_no: int, role: str, transport):
    _assert_writer_runtime(job)
    payload = job.get("payload") or {}
    reuse_id = payload.get("reuse_writer_call_id")
    reuse_key = None
    if role == "writer" and "reuse_writer_call_id" in payload:
        call = st.get("provider_jobs", reuse_id) if isinstance(reuse_id, str) else None
        source = tasks.get_job(call.get("parent_content_job_id")) if call else None
        profile = tp.profile("writer")
        prompt_id = (ctx.get("prompt") or {}).get("id")
        work = st.get("works", job["work_id"]) or {}
        valid = (round_no == 0 and job.get("kind") == "generation" and call and source
                 and call.get("state") == "succeeded" and call.get("namespace") == "content-v2"
                 and call.get("capability") == "chat" and call.get("step") == "write" and call.get("round") == 0
                 and call.get("work_id") == source.get("work_id") == job["work_id"]
                 and call.get("provider") == profile["provider"] and call.get("model") == profile["model"]
                 and source.get("kind") == "generation" and source.get("state") == "failed"
                 and (source.get("error") or {}).get("code") == "NO_VALID_CANDIDATES"
                 and source.get("content_revision") == job.get("content_revision") == int(work.get("content_revision") or 0)
                 and prompt_id and (source.get("payload") or {}).get("prompt_id") == prompt_id
                 and job["payload"].get("prompt_id") == prompt_id
                 and isinstance(source.get("writer_runtime_profiles"), dict)
                 and source.get("writer_runtime_profiles") == job.get("writer_runtime_profiles"))
        if valid:
            reuse_key = call.get("idempotency_key")
            bound = st.conn().execute("SELECT job_id FROM provider_jobs_idem WHERE idem_key=?", [reuse_key]).fetchone()
            valid = bool(reuse_key and bound and bound["job_id"] == reuse_id)
        if not valid:
            raise ContentRejected("原写稿结果不符合复用条件，未重新发送写稿请求", "WRITER_REUSE_INVALID")
    res = tp.call_text(role, parent=job, step="write" if role == "writer" else "repair", round_no=round_no,
                       system=system, user=user, transport=transport, idempotency_key=reuse_key)
    return res, {"provider": res["provider"], "model": res["model"], "job_id": res["job_id"],
                 "provider_request_id": res["provider_request_id"]}


def _initial_versions(job: dict, ctx: Dict[str, Any], transport) -> Tuple[List[Dict[str, Any]], List[str]]:
    system, user = prompts.writer_messages(brief=ctx["brief_core"], prompt=ctx["prompt"], reference=ctx["reference"],
                                           style=ctx["style"], materials=ctx["materials"], identity=ctx.get("identity"))
    res, writer = _writer_call(job, ctx, system, user, 0, "writer", transport)
    raw_list = res["json"].get("candidates")
    if not isinstance(raw_list, list) or not raw_list:
        raise ContentRejected("模型未返回 candidates 列表", "MODEL_OUTPUT_INVALID")
    plan_slots = {int(p["slot"]) for p in ctx["prompt"].get("angle_plan") or []} or {1, 2, 3}
    notes: List[str] = []
    kept: List[Dict[str, Any]] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        slot = raw.get("slot")
        if not isinstance(slot, int) or slot not in plan_slots or any(k["slot"] == slot for k in kept):
            notes.append(f"候选的 slot 非法或重复（{slot}），已丢弃")
            continue
        try:
            v = _build_version(raw, ctx=ctx, job=job, slot=slot, parent_id=None, round_no=0, writer=writer)
        except ContentRejected as exc:
            notes.append(f"slot {slot} 被拒：{exc.message}")
            continue
        if any(v["angle"] == k["angle"] for k in kept):
            notes.append(f"角度「{v['angle']}」重复，已丢弃")
            continue
        if any(text_similarity(v["text"], k["text"]) >= SAME_DRAFT_THRESHOLD for k in kept):
            notes.append(f"slot {slot} 与已有候选正文过相似，视为同稿改写，已丢弃")
            continue
        kept.append(v)
    return sorted(kept, key=lambda v: v["slot"]), notes


def _save_versions(versions: Sequence[Dict[str, Any]]) -> None:
    with st.transaction() as c:
        for v in versions:
            repo.save_script_version(c, v)


def _problem_map(versions: Sequence[Dict[str, Any]], parsed: Dict[str, Any]) -> Dict[int, List[str]]:
    """slot → 待修问题：规则硬门禁 + 单稿语义未过项 + 批次差异要求重写。"""
    out: Dict[int, List[str]] = {}
    rewrite = (set(parsed["set"]["rewrite_slots"]) or {v["slot"] for v in versions}) \
        if not parsed["set"]["passed"] else set()
    for v in versions:
        probs = _rule_problems(v)
        single = parsed["singles"].get(v["id"])
        if single is not None and not single["passed"]:
            probs += [f"[审稿] {f['issue']}" + (f"（建议：{f['suggestion']}）" if f["suggestion"] else "")
                      + (f" 引文：「{f['quote']}」" if f["quote"] else "") for f in single["findings"]]
        if v["slot"] in rewrite:
            probs += [f"[批次差异] {f['issue']}" + (f"（建议：{f['suggestion']}）" if f.get("suggestion") else "")
                      for f in parsed["set"]["findings"] if f["issue"]] \
                or ["[批次差异] 与其他稿角度或观点过于接近，需要拉开差异"]
        if probs:
            out[v["slot"]] = probs
    return out


def _assert_displayed_batch(c, work_id: str, expected_batch_id: Optional[str]) -> Optional[dict]:
    row = c.execute("SELECT data FROM script_batches WHERE work_id=?", [work_id]).fetchone()
    current = json.loads(row["data"]) if row else None
    if expected_batch_id is not None and (current or {}).get("id") != expected_batch_id:
        raise tasks.JobSuperseded("已展示的稿件被用户修改或替换，本任务只保留为历史记录")
    return current


def _bind_batch_record(c, job: dict, rec: dict, expected_batch_id: Optional[str]) -> None:
    """同一内容集合的状态刷新保留选用；稿件改动才撤销原选用与下游。"""
    from . import selection
    current = _assert_displayed_batch(c, job["work_id"], expected_batch_id)
    if not tasks.is_current(c, job):
        raise tasks.JobSuperseded("创作输入或当前任务已变化，初稿不能覆盖当前稿件")
    old_members = sorted((v.get("slot"), v["id"]) for v in (current or {}).get("candidates", []))
    new_members = sorted((v["slot"], v["candidate_id"]) for v in rec["members"])
    repo.save_batch(c, rec)
    if old_members != new_members:
        selection.on_batch_replaced(c, job["work_id"])
    repo.bind_current_batch(c, job["work_id"], rec["id"], job["content_revision"], job["id"])


def _bind_batch(job: dict, ctx: Dict[str, Any], versions: Sequence[Dict[str, Any]], *, shortfall: Optional[str],
                repair_history: List[Dict[str, Any]], expected_batch_id: Optional[str] = None) -> str:
    members = [{"slot": v["slot"], "candidate_id": v["id"]} for v in versions]
    snapshot = cc.digest({"members": members, "shortfall": shortfall, "repair_history": repair_history})
    batch_id = "cbatch_" + job["id"] + "_draft_" + snapshot.split(":", 1)[-1][:16]
    rec = {"id": batch_id, "work_id": job["work_id"], "prompt_id": (ctx["prompt"] or {}).get("id", ""),
           "content_revision": job["content_revision"], "job_id": job["id"], "source_kind": "llm",
           "content_route": ctx["brief_core"]["content_route"],
           "members": members, "requested": TARGET_COUNT, "shortfall_note": shortfall,
           "repair_note": _repair_note(repair_history), "repair_history": repair_history,
           "materials_count": len(ctx["materials"])}
    with st.transaction() as c:
        row = c.execute("SELECT data FROM content_batches WHERE id=?", [batch_id]).fetchone()
        # 重启续跑同一个快照时保留首次 created_at，避免不可变记录冲突。
        if row:
            rec["created_at"] = json.loads(row["data"])["created_at"]
        _bind_batch_record(c, job, rec, expected_batch_id)
    return batch_id


def _repair_note(history: List[Dict[str, Any]]) -> Optional[str]:
    if not history:
        return None
    rounds = sorted({h["round"] for h in history})
    limit = costs.content_limits().max_repair_rounds
    if limit == 0:
        return f"定向返工 {len(rounds)} 轮，共处理 {len(history)} 稿次；自动返工已关闭"
    return f"自动返工 {len(rounds)} 轮（上限 {limit} 轮），共重写 {len(history)} 稿次"


def _run_rounds(job: dict, ctx: Dict[str, Any], versions: List[Dict[str, Any]], *, review_ids: List[str],
                notes: List[str], repair_history: List[Dict[str, Any]], transport, first_round: int = 0,
                instruction: str = "", max_rounds: Optional[int] = None) -> str:
    """审核 → 对未过的 slot 定向返工 → 重审，最多 max_rounds 轮；统一在此计数，不再有别的返工循环。
    返回最终 job 状态（succeeded/partial）。每轮审核前先绑定当前稿件，未审稿可读但不可选。"""
    limits = costs.content_limits()
    rounds = limits.max_repair_rounds if max_rounds is None else max_rounds
    current_batch_id = _bind_batch(job, ctx, versions, shortfall="；".join(notes) or None,
                                   repair_history=repair_history,
                                   expected_batch_id=job["payload"].get("batch_id") if job["kind"] == "repair" else None)
    try:
        round_no = first_round
        parsed = sr.review_batch(parent=job, ctx=ctx, versions=versions, review_ids=review_ids, round_no=round_no,
                                 transport=transport)
        for attempt in range(1, rounds + 1):
            problems = _problem_map(versions, parsed)
            if not problems:
                break
            tasks.checkpoint(job)
            with st.transaction() as c:
                _assert_displayed_batch(c, job["work_id"], current_batch_id)
            tasks.set_progress(job["id"], stage="repair")
            targets = [{"slot": v["slot"], "angle": v["angle"], "title": v["title"], "text": v["text"],
                        "storyboard": v.get("storyboard") or [],
                        "problems": problems[v["slot"]]} for v in versions if v["slot"] in problems]
            system, user = prompts.repair_messages(brief=ctx["brief_core"], prompt=ctx["prompt"], reference=ctx["reference"],
                                                   style=ctx["style"], materials=ctx["materials"], targets=targets,
                                                   instruction=instruction, identity=ctx.get("identity"),
                                                   other_candidates=[v for v in versions if v["slot"] not in problems]
                                                   if not parsed["set"]["passed"] else [])
            round_no = first_round + attempt
            res, writer = _writer_call(job, ctx, system, user, round_no, "repair", transport)
            raw_list = res["json"].get("candidates")
            if not isinstance(raw_list, list):
                raise ContentRejected("返工结果缺少 candidates 列表", "MODEL_OUTPUT_INVALID")
            changed: List[Dict[str, Any]] = []
            by_slot = {v["slot"]: v for v in versions}
            for raw in raw_list:
                slot = raw.get("slot") if isinstance(raw, dict) else None
                if slot not in problems or any(c["slot"] == slot for c in changed):
                    continue  # 只接受被要求返工的 slot；成功稿的版本不被顺手改动
                old = by_slot[slot]
                is_storyboard = old.get("content_route") == "short_video_storyboard"
                if (is_storyboard and not raw.get("storyboard")) or (not is_storyboard and
                        (not isinstance(raw.get("text"), str) or not raw["text"].strip())):
                    repair_history.append({"round": attempt, "slot": slot, "candidate_id": old["id"], "outcome": "rejected",
                                           "note": "返工结果缺少完整内容"})
                    continue
                if (raw.get("storyboard") == old.get("storyboard") if is_storyboard
                        else _norm(raw["text"]) == _norm(old["text"])):
                    repair_history.append({"round": attempt, "slot": slot, "candidate_id": old["id"], "outcome": "unchanged",
                                           "note": "模型交回了相同正文"})
                    continue
                try:
                    nv = _build_version(raw, ctx=ctx, job=job, slot=slot, parent_id=old["id"], round_no=attempt,
                                        writer=writer, angle=old["angle"], title=old["title"],
                                        description=old["description"], tags=old["tags"])
                except ContentRejected as exc:
                    repair_history.append({"round": attempt, "slot": slot, "candidate_id": old["id"],
                                           "outcome": "rejected", "note": exc.message})
                    continue
                repair_history.append({"round": attempt, "slot": slot, "candidate_id": old["id"], "new_id": nv["id"],
                                       "outcome": "repaired", "before": problems[slot][:6],
                                       "note": str(raw.get("repair_note") or "按问题重写")[:200]})
                changed.append(nv)
            if not changed:
                break  # 没有任何有效改动：不再空烧一次审稿
            _save_versions(changed)
            for nv in changed:
                versions[[v["slot"] for v in versions].index(nv["slot"])] = nv
            current_batch_id = _bind_batch(job, ctx, versions, shortfall="；".join(notes) or None,
                                           repair_history=repair_history, expected_batch_id=current_batch_id)
            review_ids = [nv["id"] for nv in changed]
            parsed = sr.review_batch(parent=job, ctx=ctx, versions=versions, review_ids=review_ids,
                                     round_no=round_no, transport=transport)
        return _final_state(job, ctx, versions, parsed, notes, repair_history, expected_batch_id=current_batch_id)
    except (tasks.CallPending, ContentError):
        # 初稿和每轮新版本都已绑定，不再用异常路径重绑，避免覆盖用户编辑或新审核。
        raise


def _passed_slots(versions, parsed) -> List[int]:
    if not parsed["set"]["passed"]:
        return []
    problems = _problem_map(versions, parsed)
    return [v["slot"] for v in versions if v["slot"] not in problems and _hard_ok(v)]


def _final_state(job, ctx, versions, parsed, notes, repair_history, *, expected_batch_id=None) -> str:
    passed = _passed_slots(versions, parsed)
    shortfall = None
    if len(versions) < TARGET_COUNT:
        shortfall = (f"仅得到 {len(versions)}/{TARGET_COUNT} 个候选；" + ("；".join(notes) if notes else "模型未给足不同角度")
                     + "。不补齐占位稿，不把旧稿改名充数")
    state = "succeeded" if len(passed) >= TARGET_COUNT else "partial"
    created = now_iso()

    def apply(c):
        batch_id = "cbatch_" + job["id"]
        rec = {"id": batch_id, "work_id": job["work_id"], "prompt_id": (ctx["prompt"] or {}).get("id", ""),
               "content_revision": job["content_revision"], "job_id": job["id"], "source_kind": "llm",
               "content_route": ctx["brief_core"]["content_route"],
               "members": [{"slot": v["slot"], "candidate_id": v["id"]} for v in versions], "requested": TARGET_COUNT,
               "shortfall_note": shortfall, "repair_note": _repair_note(repair_history),
               "repair_history": repair_history, "materials_count": len(ctx["materials"]),
               "created_at": created}
        _bind_batch_record(c, job, rec, expected_batch_id)

    tasks.finish_job(job["id"], state, result_refs={"batch_id": "cbatch_" + job["id"], "produced": len(versions),
                                                    "passed": len(passed), "passed_slots": passed}, apply=apply)
    return state


def handle_generation(job: dict, *, transport=None) -> None:
    _assert_writer_runtime(job)
    ctx = cx.load_context(job["work_id"], require_prompt=True)
    prompt = ctx["prompt"]
    if job["payload"].get("prompt_id") not in (None, prompt["id"]):
        raise tasks.JobSuperseded("请求基于的提示已不是当前提示")
    ref_id = ctx["reference"]["id"] if ctx["reference"] else ""
    if prompt["reference_snapshot_id"] != ref_id or prompt["reference_digest"] != ctx["digests"]["reference"]:
        raise ContentRejected("提示基于另一个参考，不能生成新稿；请为当前参考重新准备提示", "PROMPT_STALE")
    if prompt["brief_digest"] != ctx["digests"]["brief"] or prompt["style_digest"] != ctx["digests"]["style"] \
            or (prompt.get("model_profile") or {}).get("evidence_digest") not in (None, ctx["digests"]["evidence"]) \
            or cc.digest(prompt.get("identity_snapshot") or {}) != ctx["digests"]["identity"]:
        raise ContentRejected("主题/资料/风格在生成提示之后已修改，旧提示不能生成新稿；请重新准备或保存提示", "PROMPT_STALE")
    prompts.require_content_skills(ctx["brief_core"])
    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="write")
    versions, notes = _initial_versions(job, ctx, transport)
    if not versions:
        raise ContentRejected("模型没有交出可用候选：" + ("；".join(notes) or "输出为空"), "NO_VALID_CANDIDATES")
    _save_versions(versions)
    tasks.set_progress(job["id"], stage="review")
    _run_rounds(job, ctx, versions, review_ids=[v["id"] for v in versions], notes=notes, repair_history=[],
                transport=transport)


def handle_repair(job: dict, *, transport=None) -> None:
    """用户明确要求的定向返工：只有被点名的 slot 参与，其余稿的版本与单稿审核保持不变。"""
    _assert_writer_runtime(job)
    ctx = cx.load_context(job["work_id"], require_prompt=True)
    p = job["payload"]
    batch = repo.get_batch(p.get("batch_id") or "")
    work = ctx["work"]
    current = st.get("script_batches", job["work_id"]) or {}
    if batch is None or batch["work_id"] != job["work_id"] or current.get("id") != batch["id"]:
        raise ContentRejected("该批次不是当前批次，不能返工", "STALE_SELECTION")
    versions = [dict(v, quality_report=current_storyboard_quality_report(v, ctx["materials"]))
                for v in repo.batch_candidates(batch)]
    ids = list(p.get("candidate_ids") or [])
    targets = [v for v in versions if v["id"] in ids]
    if not ids or len(targets) != len(ids):
        raise ContentRejected("返工目标不在当前批次里", "STALE_SELECTION")
    singles = {}
    for version in versions:
        current_review = sr.current_single(job["work_id"], version, ctx)
        if current_review is not None:
            singles[version["id"]] = {"passed": current_review["recommendation"] == "pass",
                                       "findings": current_review["findings"]}
    members = [{"slot": v["slot"], "candidate_id": v["id"], "text_digest": cc.candidate_digest(v)} for v in versions]
    set_review = sr.current_set(job["work_id"], members, ctx)
    parsed = {"singles": singles, "set": {
        "passed": set_review is None or set_review["recommendation"] == "pass",
        "rewrite_slots": ((set_review or {}).get("reviewer") or {}).get("rewrite_slots") or [],
        "findings": (set_review or {}).get("findings") or []}}
    current_problems = _problem_map(versions, parsed)
    problems: Dict[int, List[str]] = {}
    for v in targets:
        probs = current_problems.get(v["slot"], [])
        if p.get("instruction"):
            probs.append(f"[用户要求] {p['instruction']}")
        problems[v["slot"]] = probs or ["用户要求改写这一稿"]
    prompts.require_content_skills(ctx["brief_core"])
    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="repair")
    hist: List[Dict[str, Any]] = []
    versions = [dict(v) for v in versions]
    tgt = [{"slot": v["slot"], "angle": v["angle"], "title": v["title"], "text": v["text"],
            "storyboard": v.get("storyboard") or [], "problems": problems[v["slot"]]}
           for v in versions if v["slot"] in problems]
    system, user = prompts.repair_messages(brief=ctx["brief_core"], prompt=ctx["prompt"], reference=ctx["reference"],
                                           style=ctx["style"], materials=ctx["materials"], targets=tgt,
                                           instruction=str(p.get("instruction") or ""), identity=ctx.get("identity"),
                                           other_candidates=[v for v in versions if v["slot"] not in problems]
                                           if any(p.startswith("[批次差异]") for ps in problems.values() for p in ps) else [])
    res, writer = _writer_call(job, ctx, system, user, 1, "repair", transport)
    changed = []
    by_slot = {v["slot"]: v for v in versions}
    for raw in res["json"].get("candidates") or []:
        slot = raw.get("slot") if isinstance(raw, dict) else None
        if slot not in problems or any(c["slot"] == slot for c in changed):
            continue
        old = by_slot[slot]
        is_storyboard = old.get("content_route") == "short_video_storyboard"
        if (is_storyboard and not raw.get("storyboard")) or (not is_storyboard and not str(raw.get("text") or "").strip()):
            continue
        if (raw.get("storyboard") == old.get("storyboard") if is_storyboard
                else _norm(raw["text"]) == _norm(old["text"])):
            hist.append({"round": 1, "slot": slot, "candidate_id": old["id"], "outcome": "unchanged", "note": "模型交回了相同正文"})
            continue
        try:
            nv = _build_version(raw, ctx=ctx, job=job, slot=slot, parent_id=old["id"], round_no=1, writer=writer,
                                angle=old["angle"], title=old["title"], description=old["description"], tags=old["tags"])
        except ContentRejected as exc:
            hist.append({"round": 1, "slot": slot, "candidate_id": old["id"], "outcome": "rejected", "note": exc.message})
            continue
        hist.append({"round": 1, "slot": slot, "candidate_id": old["id"], "new_id": nv["id"], "outcome": "repaired",
                     "before": problems[slot][:6], "note": str(raw.get("repair_note") or "按问题重写")[:200]})
        changed.append(nv)
    if not changed:
        raise ContentRejected("返工没有产出有效的新版本，原稿保持不变", "REPAIR_NO_CHANGE")
    _save_versions(changed)
    for nv in changed:
        versions[[v["slot"] for v in versions].index(nv["slot"])] = nv
    tasks.set_progress(job["id"], stage="review")
    # 显式返工的自动再返工轮数 = 上限 - 已用的这 1 轮
    _run_rounds(job, ctx, versions, review_ids=[nv["id"] for nv in changed], notes=[], repair_history=hist,
                transport=transport, first_round=1,
                max_rounds=max(0, costs.content_limits().max_repair_rounds - 1))


def handle_review(job: dict, *, transport=None) -> None:
    """重新审核当前批次：审缺少有效单稿审核或明确审核失败的稿；差异审核读取全部当前稿件。
    用于审核服务故障后的重试和用户改稿之后的复审。"""
    from . import selection
    ctx = cx.load_context(job["work_id"])
    batch = repo.get_batch(job["payload"].get("batch_id") or "")
    current = st.get("script_batches", job["work_id"]) or {}
    if batch is None or batch["work_id"] != job["work_id"] or current.get("id") != batch["id"]:
        raise ContentRejected("该批次不是当前批次，不能复审", "STALE_SELECTION")
    if ctx["prompt"] is None and ctx["reference"] is None and not ctx["materials"]:
        pass
    versions = repo.batch_candidates(batch)
    needs = []
    for version in versions:
        single = sr.current_single(job["work_id"], version, ctx)
        if single is None or single["recommendation"] != "pass":
            needs.append(version["id"])
    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="review")
    sr.review_batch(parent=job, ctx=ctx, versions=versions, review_ids=needs, round_no=0, transport=transport)
    with st.transaction() as c:
        _assert_displayed_batch(c, job["work_id"], batch["id"])
        view = selection.project_batch(job["work_id"])
    passed = view["passed"]
    state = "succeeded" if view["ready_to_present"] else "partial"
    tasks.finish_job(job["id"], state, result_refs={"batch_id": batch["id"], "produced": view["produced"],
                                                    "passed": passed},
                     apply=lambda c: _assert_displayed_batch(c, job["work_id"], batch["id"]))


def _carry_claims(parent: Dict[str, Any], new_text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """用户改稿：只保留“依据仍成立”的断言——其正文原话仍在新正文里。其余丢弃，交给 CLAIM_COVERAGE 兜底。"""
    body = _norm(new_text)
    ev = [e for e in (parent.get("origin") or {}).get("evidence", []) if e.get("position") and _norm(e["position"]) in body]
    kept_texts = {e["claim"] for e in ev}
    claims = [c for c in parent.get("claims", []) if any(t and t in c["text"] for t in kept_texts)]
    return claims, ev


def revise_candidate(work_id: str, script_id: str, text: str = "", *,
                     storyboard: Optional[List[Dict[str, Any]]] = None,
                     title: Optional[str] = None, description: Optional[str] = None,
                     _connection=None) -> Dict[str, Any]:
    """用户手改候选：正文或结构化分镜都生成新版本，规则重审，且不调用模型。

    分镜必须完整提交结构化镜头；后端重新派生正文和时长，避免客户端只改展示文本而
    任务图仍使用旧镜头。新稿没有有效的独立审核，需要走复审才可采用。
    """
    current = st.get("script_batches", work_id)
    if not current:
        raise ContentRejected("尚未生成候选稿件", "VALIDATION_FAILED")
    cur = next((c for c in current["candidates"] if c["id"] == script_id), None)
    if cur is None:
        raise ContentRejected("稿件不存在或已被新版本取代", "STALE_SELECTION")
    ctx = cx.load_context(work_id)
    slot = cur.get("slot")
    if cur.get("content_route") == "short_video_storyboard":
        if storyboard is None:
            raise ContentRejected("分镜修订必须提交结构化镜头", "STORYBOARD_REQUIRED")
        new_title = cur["title"] if title is None else str(title).strip()
        new_description = cur.get("description", "") if description is None else str(description).strip()
        if not new_title:
            raise ContentRejected("分镜标题不能为空", "CANDIDATE_INCOMPLETE")
        _, revised_text, _ = _storyboard(storyboard, ctx["brief_core"]["target_duration_ms"])
        retained_claims = [{"text": evidence["claim"], "evidence_id": evidence.get("evidence_id"),
                            "source_quote": evidence.get("source_quote"), "position": evidence.get("position")}
                           for evidence in (cur.get("origin") or {}).get("evidence", [])
                           if evidence.get("verified") and (evidence.get("position") or evidence.get("claim"))
                           and _norm(evidence.get("position") or evidence["claim"]) in _norm(revised_text)]
        raw = {"angle": cur["angle"], "title": new_title, "description": new_description,
               "tags": cur.get("tags", []), "storyboard": storyboard, "claims": retained_claims}
        new = _build_version(raw, ctx=ctx, job={"id": st.new_id("edit"), "work_id": work_id}, slot=slot,
                             parent_id=cur["id"], round_no=0, writer={"kind": "user_edit"}, angle=cur["angle"],
                             title=new_title, description=new_description, tags=cur.get("tags", []))
        new["origin"] = {**new["origin"], "kind": "user_edit", "from": cur["id"],
                          "created_at": now_iso()}
    else:
        text = (text or "").strip()
        if not text:
            raise ContentRejected("修订正文不能为空", "VALIDATION_FAILED")
        brief = ctx["brief_core"]
        dur = estimate_duration_ms(text)
        claims, ev = _carry_claims(cur, text)
        dedup = dedup_report(work_id, text, cur["title"], brief["topic"])
        report = review_candidate(text=text, title=cur["title"], claims=[Claim(**c) for c in claims], dedup=dedup,
                                  materials=ctx["materials"], target_duration_ms=brief["target_duration_ms"],
                                  duration_estimate_ms=dur, require_hook=False)
        checks = list(report.checks)
        reuse = _reuse_findings(text, ctx)
        checks.append(QualityCheck(code="REFERENCE_REUSE", outcome="fail" if reuse else "pass", hard_gate=True,
                                   message="；".join(reuse) if reuse else "未复用参考转写的连续表达"))
        hard_fail = any(c.hard_gate and c.outcome == "fail" for c in checks)
        review_required = any(c.outcome in ("review", "fail") for c in checks)
        new = {"id": st.new_id("script"), "work_id": work_id, "slot": slot, "parent_id": cur["id"], "angle": cur["angle"],
               "title": cur["title"], "description": cur["description"], "tags": cur.get("tags", []), "text": text,
               "duration_estimate_ms": dur, "claims": claims,
               "quality_report": QualityReport(checks=checks, overall="fail" if hard_fail else ("review" if review_required else "pass"),
                                               review_required=review_required).model_dump(),
               "origin": {"kind": "user_edit", "from": cur["id"], "evidence": ev, "created_at": now_iso()}}
    old_batch = repo.get_batch(current["id"]) if repo.get_batch(current["id"]) else None
    members = [{"slot": c.get("slot") if c.get("slot") is not None else i + 1,
                "candidate_id": new["id"] if c["id"] == script_id else c["id"]}
               for i, c in enumerate(current["candidates"])]
    batch_id = st.new_id("cbatch")
    from . import selection
    rec = {"id": batch_id, "work_id": work_id, "prompt_id": (old_batch or {}).get("prompt_id", ""),
           "content_revision": int((st.get("works", work_id) or {}).get("content_revision") or 0),
           "job_id": (old_batch or {}).get("job_id", ""), "source_kind": current.get("source_kind") or "llm",
           "content_route": current.get("content_route") or cur.get("content_route") or "talking_script",
           "members": members, "requested": (old_batch or {}).get("requested") or TARGET_COUNT,
           "shortfall_note": current.get("shortfall_note"), "repair_note": current.get("repair_note"),
           "repair_history": current.get("repair_history") or [], "materials_count": current.get("materials_count"),
           "created_at": now_iso()}
    if old_batch is None or current.get("source_kind") == "mock":
        # 历史/模拟批次没有不可变快照：直接就地替换该稿，仍新建版本 ID
        cands = [dict({k: v for k, v in new.items() if k != "work_id"}) if c["id"] == script_id else c
                 for c in current["candidates"]]
        with nullcontext(_connection) if _connection is not None else st.transaction() as c:
            work = json.loads(c.execute("SELECT data FROM works WHERE id=?", [work_id]).fetchone()["data"])
            selection.on_batch_replaced(c, work_id)
            if _connection is not None:
                for candidate in cands:
                    if repo.get_script_version(candidate["id"]) is None:
                        repo.save_script_version(c, dict(candidate, work_id=work_id))
                repo.save_batch(c, rec)
                repo.bind_current_batch(c, work_id, batch_id, rec["content_revision"], None)
            else:
                st.insert_row(c, "script_batches", work_id, dict(current, candidates=cands))
        return {k: v for k, v in new.items() if k != "work_id"}
    with nullcontext(_connection) if _connection is not None else st.transaction() as c:
        repo.save_script_version(c, new)
        repo.save_batch(c, rec)
        selection.on_batch_replaced(c, work_id)
        repo.bind_current_batch(c, work_id, batch_id, rec["content_revision"], None)
    return {k: v for k, v in new.items() if k != "work_id"}


def ensure_prompt(work_id: str, *, _connection=None) -> Dict[str, Any]:
    """当前作品的可用提示；没有（或已过期）就建一个“直接提示”：无参考，凭主题写作，不调用模型。"""
    if _connection is None:
        with st.transaction() as c:
            return ensure_prompt(work_id, _connection=c)
    ctx = cx.load_context(work_id)
    prompt = ctx["prompt"]
    ref = ctx["reference"]
    if prompt is not None:
        fresh = (prompt["reference_snapshot_id"] == (ref["id"] if ref else "")
                 and prompt.get("reference_digest", "") == ctx["digests"]["reference"]
                 and prompt["brief_digest"] == ctx["digests"]["brief"]
                 and prompt["style_digest"] == ctx["digests"]["style"]
                 and cc.digest(prompt.get("identity_snapshot") or {}) == ctx["digests"]["identity"]
                 and (prompt.get("model_profile") or {}).get("evidence_digest") in (None, ctx["digests"]["evidence"]))
        if fresh:
            return prompt
    if ref is not None:
        raise ContentRejected("已选参考但写作提示已过期，请先重新准备提示", "PROMPT_STALE")
    b = ctx["brief_core"]
    if not b["topic"]:
        raise ContentRejected("缺少主题，不能生成稿件", "TOPIC_REQUIRED")
    kind = "结构化短视频分镜剧本" if b["content_route"] == "short_video_storyboard" else "原创口播稿"
    doc = {"id": st.new_id("prm"), "work_id": work_id, "reference_snapshot_id": "", "reference_digest": "",
           "brief_digest": ctx["digests"]["brief"], "style_digest": ctx["digests"]["style"],
           "identity_snapshot": ctx.get("identity") or {},
           "version": len(repo.list_prompts(work_id)) + 1, "source": "generated", "reference_analysis": {}, "angle_plan": [],
           "prompt_text": f"围绕主题「{b['topic']}」，面向{b['audience'] or '对主题感兴趣的年轻受众'}，"
                          f"写三篇切入角度互不相同的{kind}：有自己的观点和叙事，不套模板。",
           "template_version": prompts.TEMPLATE_VERSION, "content_revision": ctx["content_revision"],
           "model_profile": {"direct": True, "evidence_digest": ctx["digests"]["evidence"],
                             "identity_digest": ctx["digests"]["identity"]}, "created_at": now_iso()}
    saved = repo.save_prompt(_connection, doc)
    repo.bump_content_revision(_connection, work_id, writing_prompt_id=saved["id"])
    return saved


LEGACY_WAIT_SECONDS = 110.0


def legacy_request_id(work_id: str, kind: str, payload: Dict[str, Any]) -> str:
    """旧入口无 request_id 时的确定性键：同作品/类型/修订号/规范化请求 → 同一个键，重复点击与超时重试取原操作。
    只有此前同键的任务都已“确定没有产出”（failed/blocked/cancelled/superseded）才顺延世代；
    unknown/进行中/成功都复用原任务，不会因重试多收一次费。"""
    rev = int((st.get("works", work_id) or {}).get("content_revision") or 0)
    base = cc.digest({"work": work_id, "kind": kind, "rev": rev, "req": payload})[7:]
    gen = 0
    while True:
        rid = f"legacy_{base}" + (f"_{gen}" if gen else "")
        prior = tasks.find_job(work_id, kind, rid)
        if prior is None or prior["state"] not in ("failed", "blocked", "cancelled", "superseded"):
            return rid
        gen += 1


def run_legacy(work_id: str, kind: str, payload: Dict[str, Any], *, request_id: Optional[str] = None,
               wait_seconds: Optional[float] = None) -> Dict[str, Any]:
    """旧同步入口：提交（或取回）持久任务并等待。返回终态任务；超时返回仍在进行的任务，由调用方报 504。"""
    rid = request_id or legacy_request_id(work_id, kind, payload)
    job = tasks.submit_job(work_id, kind, rid, payload)
    if job["state"] in tasks.TERMINAL_STATES:
        return job
    return tasks.run_inline(job["id"], wait_seconds=LEGACY_WAIT_SECONDS if wait_seconds is None else wait_seconds)


def register_handlers() -> None:
    tasks.register("prompt", handle_prepare_prompt)
    tasks.register("prompt_revision", handle_prompt_revision)
    tasks.register("generation", handle_generation)
    tasks.register("repair", handle_repair)
    tasks.register("review", handle_review)
