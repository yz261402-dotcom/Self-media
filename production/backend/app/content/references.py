"""参考：搜索、导入（详情+转写）、快照创建、用户修正转写、绑定当前参考。

搜索/导入是父任务处理器（在调度线程内执行，网络请求在事务外，由 tasks.run_call 记账）。
本模块只绑定"已提取有效"的快照；导入但未选中的参考不会改变当前参考。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from .. import storage as st
from ..contracts import now_iso
from . import contracts as cc
from . import repository as repo
from . import tasks
from .errors import ContentRejected
from .providers import redfox

SEARCH_CACHE_SECONDS = 15 * 60
SORTS = ("likes", "recent", "recommended")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _reserve_fen(step: str) -> int:
    """上限预留额（分）。不是供应商单价：宣传价未经本账号核验，实际费用取自配置或保持待对账。"""
    return _env_int(f"AVATAR_STUDIO_REDFOX_RESERVE_FEN_{step.upper()}", {"search": 50, "detail": 50, "transcript": 100}[step])


def _cost_fen(step: str) -> Optional[int]:
    raw = os.environ.get(f"AVATAR_STUDIO_REDFOX_PRICE_FEN_{step.upper()}", "")
    return int(raw) if raw.strip().isdigit() else None


def _call(parent: dict, step: str, request: dict, send: Callable[[], tasks.CallOutcome], *,
          budget_step: str, round_no: int = 0) -> dict:
    return tasks.run_call(parent=parent, provider="redfox", model="redfox-api", capability=budget_step, step=step,
                          round_no=round_no, request=request, send=send, billable=True,
                          reserve_fen=_reserve_fen(budget_step))


def _today(job: dict) -> datetime:
    return datetime.fromisoformat(job["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------- 搜索

def normalize_search_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    query = str(payload.get("query") or "").strip()
    if not query:
        raise ContentRejected("搜索主题不能为空", "VALIDATION_FAILED")
    period = int(payload.get("period_days") or 7)
    sort = payload.get("sort") or "recommended"
    if period not in (7, 30) or sort not in SORTS:
        raise ContentRejected("period_days 只能是 7/30，sort 只能是 likes/recent/recommended", "VALIDATION_FAILED")
    return {"query": query, "period_days": period, "sort": sort, "cursor": payload.get("cursor") or None,
            "refresh": bool(payload.get("refresh"))}


def _cached_search(work_id: str, norm: Dict[str, Any], now: datetime) -> Optional[dict]:
    if norm["refresh"]:
        return None
    for row in reversed(st.all_rows("content_jobs", "work_id=?", [work_id])):
        if row["kind"] != "search" or row["state"] != "succeeded" or not row.get("result_refs"):
            continue
        p = row.get("payload") or {}
        same = all(p.get(k) == norm[k] for k in ("query", "period_days", "sort", "cursor"))
        age = (now - datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))).total_seconds()
        if same and age <= SEARCH_CACHE_SECONDS:
            return row
    return None


def handle_search(job: dict, *, transport=None) -> None:
    norm = normalize_search_payload(job["payload"])
    now = _today(job)
    cached = _cached_search(job["work_id"], norm, now)
    if cached is not None:
        refs = dict(cached["result_refs"], cached_from=cached["id"])
        tasks.finish_job(job["id"], "succeeded", result_refs=refs)
        return
    page = redfox.decode_cursor(norm["cursor"])
    start = (now - timedelta(days=norm["period_days"])).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    fetched_at = job.get("fetched_at") or now_iso()
    tasks.set_progress(job["id"], stage="search", fetched_at=fetched_at)

    def send():
        res = redfox.search_works(keyword=norm["query"], start_date=start, end_date=end, page=page,
                                  fetched_at=fetched_at, sort=norm["sort"], transport=transport)
        return tasks.CallOutcome(response=res, cost_fen=_cost_fen("search"))

    call = _call(job, "search", {"norm": {k: v for k, v in norm.items() if k != "refresh"}, "start": start,
                                 "end": end, "page": page}, send, budget_step="search")
    result = dict(call["result"])
    result["query"], result["period_days"], result["sort"] = norm["query"], norm["period_days"], norm["sort"]
    tasks.finish_job(job["id"], "succeeded", result_refs=result)


# ---------------------------------------------------------------- 导入

def _existing_valid_reference(work_id: str, platform_work_id: str) -> Optional[dict]:
    for ref in reversed(repo.list_references(work_id)):
        if (ref["platform_work_id"] == platform_work_id and ref["extraction_version"] == redfox.EXTRACTION_VERSION
                and cc.transcript_is_usable(ref["transcript"]) and ref.get("parent_id") is None):
            return ref
    return None


def _find_call(parent_id: str, step: str) -> Optional[dict]:
    for c in tasks.jobs_calls(parent_id):
        if c.get("step") == step:
            return c
    return None


def build_reference(*, ref_id: str, work_id: str, item: Dict[str, Any], transcript: Dict[str, Any],
                    fetched_at: str) -> Dict[str, Any]:
    return {
        "id": ref_id, "work_id": work_id, "provider": "redfox", "platform": "douyin",
        "platform_work_id": item["platform_work_id"], "source_url": item["source_url"],
        "title": item.get("title", ""), "caption": item.get("caption", ""), "author": item.get("author", ""),
        "cover_url": item.get("cover_url"), "video_url": item.get("video_url"),
        "metrics": item["metrics"], "published_at": item.get("published_at"),
        "fetched_at": fetched_at, "transcript": transcript, "extraction_version": redfox.EXTRACTION_VERSION,
        "evidence_kind": "inspiration",
        "unverified_fields": ["metrics.snapshot_time"] if any(v is not None for v in item["metrics"].values()) else [],
    }


def _poll_transcript(call: dict, transport) -> dict:
    """已有远端句柄：只用同一个 taskId 查询，有限轮询。"""
    interval = float(os.environ.get("AVATAR_STUDIO_REDFOX_POLL_INTERVAL", "2"))
    attempts = max(1, _env_int("AVATAR_STUDIO_REDFOX_POLL_MAX", 5))
    for i in range(attempts):
        def query(tid):
            r = redfox.query_transcript(tid, transport=transport)
            return tasks.CallOutcome(response=r, pending=r["state"] == "processing", cost_fen=0)
        out = tasks.resume_remote(call["id"], query)
        if out["state"] == "succeeded":
            return out
        if i < attempts - 1 and interval > 0:
            time.sleep(interval)
    raise tasks.CallPending("转写仍在处理中，将只用同一 taskId 再查询", detail=call["id"])


def handle_import(job: dict, *, transport=None) -> None:
    payload = job["payload"]
    vid = str(payload.get("platform_work_id") or "").strip() or redfox.video_id_from_url(payload.get("source_url") or "")
    if not vid:
        raise ContentRejected("需要作品 ID 或含作品 ID 的抖音链接", "VALIDATION_FAILED")
    reused = _existing_valid_reference(job["work_id"], vid)
    if reused is not None:
        tasks.finish_job(job["id"], "succeeded", result_refs={
            "reference_id": reused["id"], "transcript_state": "ready", "reused": True})
        return
    fetched_at = job.get("fetched_at") or now_iso()
    tasks.set_progress(job["id"], stage="detail", fetched_at=fetched_at)

    def send_detail():
        return tasks.CallOutcome(response=redfox.work_detail(vid, fetched_at, transport=transport),
                                 cost_fen=_cost_fen("detail"))

    item = _call(job, "detail", {"video_id": vid}, send_detail, budget_step="detail")["result"]
    transcript: Dict[str, Any] = {"state": "none"}
    if item.get("transcribable"):
        tasks.set_progress(job["id"], stage="transcript")
        existing = _find_call(job["id"], "transcript_submit")
        if existing is not None and existing.get("remote_task_id") and existing["state"] == "succeeded":
            done = existing  # 显式刷新已经用同一 taskId 取回结果：直接采用，不重提交
        elif existing is not None and existing["state"] in ("waiting_external", "unknown") and existing.get("remote_task_id"):
            done = _poll_transcript(existing, transport)
        else:
            def send_submit():
                task_id = redfox.submit_transcript(item["source_url"], transport=transport)
                return tasks.CallOutcome(pending=True, remote_task_id=task_id, cost_fen=_cost_fen("transcript"))
            try:
                _call(job, "transcript_submit", {"video_id": vid, "url": item["source_url"]}, send_submit,
                      budget_step="transcript")
            except tasks.CallPending:
                pending = _find_call(job["id"], "transcript_submit")
                if pending is None or pending["state"] != "waiting_external":
                    raise
                done = _poll_transcript(pending, transport)
            else:  # 同步返回（不应发生）：没有句柄就没有转写结果
                raise ContentRejected("转写提交未返回远端任务句柄", "TRANSCRIPT_NO_HANDLE")
        res = done["result"]
        if res["state"] == "succeeded" and res["text"].strip():
            transcript = cc.transcript_view({"state": "ready", "text": res["text"].strip(), "source": "redfox",
                                             "task_id": res["task_id"]})
        elif res["state"] == "succeeded":
            transcript = {"state": "failed", "source": "redfox", "task_id": res["task_id"],
                          "error": "转写成功但正文为空（可能是纯音乐/无口播）"}
        else:
            transcript = {"state": "failed", "source": "redfox", "task_id": res["task_id"],
                          "error": res.get("fail_reason") or "转写失败"}
    ref = build_reference(ref_id="ref_" + job["id"], work_id=job["work_id"], item=item, transcript=transcript,
                          fetched_at=fetched_at)
    with st.transaction() as c:
        saved = repo.save_reference(c, ref)
    tasks.finish_job(job["id"], "succeeded", result_refs={
        "reference_id": saved["id"], "transcript_state": saved["transcript"]["state"],
        "visual_state": cc.visual_state(saved), "content_digest": saved["content_digest"]})


# ---------------------------------------------------------------- 修正转写 / 绑定

def handle_reference_revision(job: dict) -> None:
    from . import media
    p = job["payload"]
    text = str(p.get("transcript_text") or "").strip()
    if not text:
        raise ContentRejected("修正后的转写不能为空", "VALIDATION_FAILED")
    base = repo.get_reference(p.get("reference_snapshot_id") or "")
    if base is None or base["work_id"] != job["work_id"]:
        raise ContentRejected("要修正的参考不存在或不属于该作品", "REFERENCE_NOT_FOUND")
    fetched_at = job.get("fetched_at") or now_iso()
    new = dict(base, id="ref_" + job["id"], parent_id=base["id"], fetched_at=fetched_at,
               original_transcript=base.get("original_transcript") or base["transcript"],
               transcript={"state": "ready", "text": text, "source": "user_edit", "task_id": None},
               media_asset_id=media.reference_media_asset_id(job["work_id"], base["id"]) or None)

    with st.transaction() as c:
        saved = repo.save_reference(c, new)
    current = st.get("works", job["work_id"]) or {}
    apply = None
    if current.get("reference_snapshot_id") == base["id"]:
        apply = lambda c: repo.bump_content_revision(c, job["work_id"], reference_snapshot_id=saved["id"],
                                                      writing_prompt_id=None)
    tasks.finish_job(job["id"], "succeeded", result_refs={"reference_id": saved["id"]},
                     apply=apply)


def usable_for_route(ref: Dict[str, Any], route: str) -> Optional[ContentRejected]:
    """参考能否作为该内容路线的写作依据。口播稿只认转写；分镜可用转写或画面分析。"""
    if cc.transcript_is_usable(ref["transcript"]):
        return None
    if route == "short_video_storyboard":
        if cc.visual_state(ref) == "ready":
            return None
        return ContentRejected("该参考既没有有效转写，也没有完成的画面分析；请上传视频做画面分析，或补全转写",
                               "REFERENCE_VISUAL_REQUIRED")
    return ContentRejected("口播稿需要有效转写：该参考没有有效口播，请提取或手工补全转写",
                           "REFERENCE_TRANSCRIPT_REQUIRED")


def select_reference(work_id: str, reference_id: str, expected_revision: int,
                     content_route: Optional[str] = None) -> Dict[str, Any]:
    """绑定当前参考：只接受本作品的、对当前内容路线可用的快照；换参考使旧提示失效。"""
    ref = repo.get_reference(reference_id)
    if ref is None or ref["work_id"] != work_id:
        raise ContentRejected("参考不存在或不属于该作品", "REFERENCE_NOT_FOUND")
    if content_route is None:
        work = st.get("works", work_id) or {}
        content_route = (work.get("brief") or {}).get("content_route") or "talking_script"
    blocked = usable_for_route(ref, content_route)
    if blocked is not None:
        raise blocked
    with st.transaction() as c:
        return repo.bump_content_revision(c, work_id, expected_revision=expected_revision,
                                          reference_snapshot_id=reference_id, writing_prompt_id=None)


def register_handlers() -> None:
    tasks.register("search", handle_search)
    tasks.register("import", handle_import)
    tasks.register("reference_revision", handle_reference_revision)
