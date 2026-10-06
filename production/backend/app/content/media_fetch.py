"""自动获取参考视频：RedFox 短视频下载器取临时直链 → 受控下载到本地媒体库。

- 独立付费调用，用户确认（route=MEDIA_FETCH）绑定作品、参考快照与作品页链接；没有确认不发任何请求。
- 外部调用只经 tasks.run_call；幂等键含任务 ID，发送后结果未知只进入 unknown，绝不自动重发。
- 直链是带签名的临时地址：只在账本调用结果里保留给同一任务续跑，MediaAsset 只记作品页链接。
- 下载后与手动上传走同一套大小/时长/类型校验，本模块不做视觉分析，也不动转写语义。
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Any, Dict, Optional

from .. import storage as st
from ..contracts import now_iso
from . import contracts as cc
from . import costs, media, repository as repo, tasks
from .errors import ContentRejected
from .providers import redfox

ROUTE = "MEDIA_FETCH"
KIND = "media_fetch"
STEP = "video_download"
_HOSTS = ("douyin.com", "iesdouyin.com")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def reserve_fen() -> int:
    return _int_env("AVATAR_STUDIO_REDFOX_RESERVE_FEN_VIDEO_DOWNLOAD", 50)


def price_fen() -> Optional[int]:
    raw = os.environ.get("AVATAR_STUDIO_REDFOX_PRICE_FEN_VIDEO_DOWNLOAD", "").strip()
    return int(raw) if raw.isdigit() else None


def _bound(work_id: str, reference_id: str) -> Dict[str, Any]:
    ref = repo.get_reference(reference_id or "")
    if ref is None or ref["work_id"] != work_id:
        raise ContentRejected("参考不存在或不属于该作品", "REFERENCE_NOT_FOUND")
    url = str(ref.get("source_url") or "")
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not url.startswith(("http://", "https://")) or not any(host == h or host.endswith("." + h) for h in _HOSTS):
        raise ContentRejected("该参考没有可用的抖音作品页链接，请手动上传视频", "REFERENCE_MEDIA_UNAVAILABLE")
    return ref


def quote(work_id: str, reference_id: str) -> Dict[str, Any]:
    ref = _bound(work_id, reference_id)
    listing = {"route": ROUTE, "work_id": work_id, "reference_snapshot_id": ref["id"],
               "source_url": ref["source_url"], "provider": "redfox", "capability": STEP}
    return {"work_id": work_id, "route": ROUTE, "call_list": listing, "request_digest": cc.digest(listing),
            "reserve_fen": reserve_fen(), "estimated_fen": price_fen(), "price_verified": price_fen() is not None,
            "max_ceiling_fen": costs.content_limits().per_flow_fen,
            "note": "自动获取视频是 RedFox 的独立付费调用；实际扣费以 RedFox 账单为准，视频版权由你确认"}


def create_confirmation(work_id: str, reference_id: str, *, ceiling_fen: int, actor_id: str = "user") -> Dict[str, Any]:
    if not actor_id.startswith("user"):
        raise ContentRejected("费用确认必须由用户给出", "COST_ACTOR_INVALID")
    q = quote(work_id, reference_id)
    if ceiling_fen <= 0 or ceiling_fen < q["reserve_fen"]:
        raise ContentRejected(f"费用确认金额必须大于 0、不低于预留额 {q['reserve_fen']} 分", "COST_AMOUNT_INVALID")
    row = {"id": st.new_id("cost"), "work_id": work_id, "route": ROUTE, "actor_id": actor_id,
           "confirmed_at": now_iso(), "request_digest": q["request_digest"], "call_list": q["call_list"],
           "estimated_fen": q["estimated_fen"], "ceiling_fen": int(ceiling_fen), "authorizes_payment": False}
    st.put("cost_confirmations", row["id"], row, {"work_id": work_id})
    return row


def has_user_confirmation(work_id: str, digest: str, min_fen: int) -> bool:
    return any(r.get("route") == ROUTE and r.get("request_digest") == digest
               and str(r.get("actor_id") or "").startswith("user") and int(r.get("ceiling_fen") or 0) >= min_fen
               for r in st.all_rows("cost_confirmations", "work_id=?", [work_id]))


def submit_media_fetch(work_id: str, reference_id: str, *, confirmation_id: str, request_id: str = "",
                       expected_revision: Optional[int] = None) -> Dict[str, Any]:
    row = st.get("cost_confirmations", confirmation_id) if confirmation_id else None
    if (not row or row.get("route") != ROUTE or row.get("work_id") != work_id
            or not str(row.get("actor_id") or "").startswith("user")):
        raise ContentRejected("请先由用户确认获取视频的费用", "MEDIA_FETCH_CONFIRMATION_REQUIRED")
    if row["request_digest"] != quote(work_id, reference_id)["request_digest"]:
        raise ContentRejected("参考已变化，请重新确认费用", "COST_LIST_CHANGED")
    payload = {"reference_snapshot_id": reference_id, "total_ceiling_fen": row["ceiling_fen"]}
    return tasks.submit_job(work_id, KIND, request_id or f"fetch-{reference_id}", payload,
                            expected_revision=expected_revision)


def handle_media_fetch(job: dict, *, transport=None, opener=None) -> None:
    p = job["payload"]
    work_id = job["work_id"]
    ref = _bound(work_id, p.get("reference_snapshot_id"))
    ceiling = int(p.get("total_ceiling_fen") or 0)
    if not has_user_confirmation(work_id, quote(work_id, ref["id"])["request_digest"], ceiling):
        raise ContentRejected("请先由用户确认获取视频的费用", "MEDIA_FETCH_CONFIRMATION_REQUIRED")
    if ceiling < reserve_fen():
        raise ContentRejected("已确认的费用上限低于当前预留额，请重新确认", "COST_AMOUNT_INVALID")

    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="resolve")

    def send() -> tasks.CallOutcome:
        redfox.api_key()  # 缺凭据抛 NotSent：请求确定没有离开本机
        out = redfox.download_video(ref["source_url"], transport=transport)
        return tasks.CallOutcome(response=out, cost_fen=price_fen())

    call = tasks.run_call(parent=job, provider="redfox", model="redfox-api", capability=STEP, step=STEP, round_no=0,
                          request={"reference": ref["id"], "platform_work_id": ref["platform_work_id"]},
                          send=send, billable=True, reserve_fen=reserve_fen(),
                          idempotency_key=f"media_fetch:{job['id']}")

    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="download")
    asset = media.download_video(work_id, ref["id"], call["result"]["video_url"], ref["source_url"], opener=opener)
    tasks.finish_job(job["id"], "succeeded", result_refs={
        "reference_id": ref["id"], "media_asset_id": asset.id, "duration_ms": asset.duration_ms,
        "byte_size": asset.byte_size})


def register_handlers() -> None:
    tasks.register(KIND, handle_media_fetch)
