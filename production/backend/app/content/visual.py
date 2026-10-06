"""视觉分析领域服务：费用确认、提交任务、执行任务。UI API 与主控 Agent 工具共用这里，只有一条业务链和一份台账。

- 费用确认由用户创建（route=VISUAL_ANALYSIS），绑定作品、参考快照、媒体 SHA、模型、分析版本与费用上限。
- Agent 只能带 confirmation_id 调用 submit_visual_analysis，不能自填 paid_ack 或金额。
- 外部调用只经 tasks.run_call；发送后未知只进入 unknown，绝不自动重发。
"""
from __future__ import annotations

import os
import hashlib
from typing import Any, Dict, Optional

from .. import storage as st
from ..contracts import now_iso
from . import contracts as cc
from . import costs, media, repository as repo, tasks
from .errors import ContentRejected
from .providers import qwen_vision as qv

ROUTE = "VISUAL_ANALYSIS"
KIND = "visual_analysis"
EXTRACTION_SUFFIX = "+" + qv.ANALYSIS_VERSION


def request_id_for(reference_id: str, media_sha: str) -> str:
    profile = cc.digest({"model": qv.model(), "analysis_version": qv.ANALYSIS_VERSION})
    return f"visual-{reference_id}-{media_sha}-{profile}"


def _bound(work_id: str, reference_id: str, media_asset_id: str):
    ref = repo.get_reference(reference_id or "")
    if ref is None or ref["work_id"] != work_id:
        raise ContentRejected("参考不存在或不属于该作品", "REFERENCE_NOT_FOUND")
    if not media_asset_id:
        raise ContentRejected("该参考还没有可分析的视频文件：请上传原视频（不会把抖音页面链接当作视频）",
                              "REFERENCE_MEDIA_UNAVAILABLE")
    return ref, media.asset_for_work(work_id, media_asset_id)


def quote(work_id: str, reference_id: str, media_asset_id: str) -> Dict[str, Any]:
    """给用户确认的调用清单：摘要变化（换参考/换媒体/换模型/换版本）即需重新确认。"""
    ref, asset = _bound(work_id, reference_id, media_asset_id)
    lim = costs.visual_limits()
    listing = {"route": ROUTE, "work_id": work_id, "reference_snapshot_id": ref["id"], "media_asset_id": asset.id,
               "media_sha256": asset.sha256, "model": qv.model(), "analysis_version": qv.ANALYSIS_VERSION}
    out = {"work_id": work_id, "route": ROUTE, "call_list": listing, "request_digest": cc.digest(listing),
            "reserve_fen": lim.reserve_fen, "estimated_fen": lim.price_fen, "price_verified": lim.price_fen is not None,
            "timeout_seconds": qv.timeout_seconds(),
            "max_ceiling_fen": costs.content_limits().per_flow_fen,
            "note": "画面分析是独立付费调用；实际费用以供应商账单为准，未配置单价时保持待对账"}
    rows = _matching_calls(out)
    source = rows[-1] if rows else None
    out["retry_unknown_call"] = ({"call_id": source["id"], "sent_at": source["sent_at"],
                                  "reserved_fen": int(source.get("reserved_fen") or 0)}
                                 if source and _sent_unknown(source)
                                 and not any(r.get("state") in ("queued", "leased", "waiting_external") for r in rows)
                                 and not _has_usable_response(rows, asset.duration_ms) else None)
    return out


def _base_key(call: dict) -> str:
    return str(call.get("idempotency_key") or "").split(":attempt:", 1)[0]


def _matching_calls(q: dict) -> list:
    listing = q["call_list"]
    prefix = (f"visual:{listing['reference_snapshot_id']}:{listing['media_sha256']}:"
              f"{listing['model']}:{listing['analysis_version']}:")
    out = []
    for call in st.all_rows("provider_jobs", "work_id=?", [q["work_id"]]):
        if (call.get("provider") != "qwen" or call.get("capability") != KIND
                or call.get("model") != listing["model"] or not _base_key(call).startswith(prefix)):
            continue
        parent = tasks.get_job(call.get("parent_content_job_id") or "") or {}
        p = parent.get("payload") or {}
        if (parent.get("work_id") == q["work_id"] and parent.get("kind") == KIND
                and p.get("reference_snapshot_id") == listing["reference_snapshot_id"]
                and p.get("media_asset_id") == listing["media_asset_id"]
                and p.get("model") == listing["model"] and p.get("analysis_version") == listing["analysis_version"]
                and p.get("request_digest") == q["request_digest"]):
            out.append(call)
    return sorted(out, key=lambda r: (r.get("created_at") or r.get("sent_at") or "", r["id"]))


def _sent_unknown(call: dict) -> bool:
    return bool(call.get("state") == "unknown" and call.get("sent_at") and int(call.get("attempts") or 0) > 0)


def _has_usable_response(rows: list, duration_ms: int) -> bool:
    for row in rows:
        if row.get("state") != "succeeded":
            continue
        try:
            qv.parse_analysis(row.get("result") or {}, duration_ms=duration_ms)
        except ContentRejected:
            continue
        return True
    return False


def _retry_source(q: dict, source_id: str, *, own_job_id: str = "") -> dict:
    rows = [r for r in _matching_calls(q) if r.get("parent_content_job_id") != own_job_id]
    source = next((r for r in rows if r["id"] == source_id), None)
    if not source or not _sent_unknown(source):
        raise ContentRejected("原调用已变化或不属于本次视频解析，请刷新后核对", "VISUAL_RETRY_SOURCE_INVALID")
    asset = media.asset_for_work(q["work_id"], q["call_list"]["media_asset_id"])
    if _has_usable_response(rows, asset.duration_ms):
        raise ContentRejected("该视频已有可用画面分析，不会再次收费解析", "VISUAL_ALREADY_ANALYZED")
    if rows[-1]["id"] != source_id or any(r.get("state") in ("queued", "leased", "waiting_external") for r in rows):
        raise ContentRejected("原调用已变化或已有更新的解析请求，请刷新后核对", "VISUAL_RETRY_SOURCE_INVALID")
    return source


def previous_unknown_error(call_id: str) -> tasks.CallPending:
    return tasks.CallPending("该视频原请求结果及费用尚未确认，本次未发送。重新提交须另行确认可能重复产生的费用。",
                             code="VISUAL_PREVIOUS_CALL_UNKNOWN", detail=call_id)


def create_confirmation(work_id: str, reference_id: str, media_asset_id: str, *, ceiling_fen: int,
                        actor_id: str = "user", retry_unknown_call_id: str = "",
                        retry_paid_ack: bool = False) -> Dict[str, Any]:
    """只有页面（用户）入口调用；Agent 无权创建。"""
    if not actor_id.startswith("user"):
        raise ContentRejected("费用确认必须由用户给出", "COST_ACTOR_INVALID")
    q = quote(work_id, reference_id, media_asset_id)
    if bool(retry_unknown_call_id) != (retry_paid_ack is True):
        raise ContentRejected("请单独确认原请求可能已收费，并同意重新提交产生新的费用", "VISUAL_RETRY_CONFIRMATION_REQUIRED")
    if ceiling_fen <= 0 or ceiling_fen < q["reserve_fen"]:
        raise ContentRejected(f"费用确认金额必须大于 0、不低于预留额 {q['reserve_fen']} 分", "COST_AMOUNT_INVALID")
    row = {"id": st.new_id("cost"), "work_id": work_id, "route": ROUTE, "actor_id": actor_id,
           "confirmed_at": now_iso(), "request_digest": q["request_digest"], "call_list": q["call_list"],
           "estimated_fen": q["estimated_fen"], "ceiling_fen": int(ceiling_fen), "authorizes_payment": False}
    with st.transaction() as c:
        if retry_unknown_call_id:
            source = _retry_source(q, retry_unknown_call_id)
            parent = tasks.get_job(source["parent_content_job_id"]) or {}
            previous = st.get("cost_confirmations", (parent.get("payload") or {}).get("retry_confirmation_id") or "") or {}
            ancestry = (previous.get("retry_unknown_call_ids") or []) if (
                previous.get("request_digest") == q["request_digest"]
                and previous.get("retry_base_key") == _base_key(source)) else []
            row.update(retry_paid_ack=True, retry_unknown_call_id=source["id"],
                       retry_unknown_call_ids=list(dict.fromkeys([*ancestry, source["id"]])),
                       retry_base_key=_base_key(source), retry_source_sent_at=source["sent_at"])
        st.insert_row(c, "cost_confirmations", row["id"], row, {"work_id": work_id})
    return row


def verify_confirmation(work_id: str, reference_id: str, media_asset_id: str, confirmation_id: str) -> Dict[str, Any]:
    row = st.get("cost_confirmations", confirmation_id) if confirmation_id else None
    if (not row or row.get("route") != ROUTE or row.get("work_id") != work_id
            or not str(row.get("actor_id") or "").startswith("user")):
        raise ContentRejected("请先由用户确认视觉分析费用", "VISUAL_COST_CONFIRMATION_REQUIRED")
    if row["request_digest"] != quote(work_id, reference_id, media_asset_id)["request_digest"]:
        raise ContentRejected("参考、视频或模型已变化，请重新确认费用", "COST_LIST_CHANGED")
    return row


def submit_visual_analysis(work_id: str, reference_id: str, media_asset_id: str, *, confirmation_id: str,
                           expected_revision: Optional[int] = None, request_id: str = "") -> Dict[str, Any]:
    """提交（或按同一 request_id 取回）视觉分析任务。UI 与 Agent 工具的唯一入口。

    payload 不含 confirmation_id：同一请求重放必须得到原任务；处理器按调用清单摘要重新核验用户确认。
    """
    row = verify_confirmation(work_id, reference_id, media_asset_id, confirmation_id)
    asset = media.asset_for_work(work_id, media_asset_id)
    payload = {"reference_snapshot_id": reference_id, "media_asset_id": asset.id,
               "total_ceiling_fen": row["ceiling_fen"], "model": row["call_list"]["model"],
               "analysis_version": row["call_list"]["analysis_version"], "request_digest": row["request_digest"]}
    rid = request_id or request_id_for(reference_id, asset.sha256)
    if not row.get("retry_unknown_call_id"):
        return tasks.submit_job(work_id, KIND, rid, payload, expected_revision=expected_revision)
    payload.update(retry_unknown_call_id=row["retry_unknown_call_id"], retry_confirmation_id=row["id"])
    prior = replay_submission(work_id, rid, reference_id, asset.id, row["ceiling_fen"], row["retry_unknown_call_id"])
    if prior:
        return prior
    # storage 不支持嵌套事务。先占用授权；失败且没有父任务时释放。中断后同 request_id 可继续建原任务。
    with st.transaction() as c:
        current = st.get("cost_confirmations", row["id"]) or {}
        if current.get("consumed_request_id") not in (None, rid):
            raise ContentRejected("本次重新提交授权已被使用，请重新确认费用", "VISUAL_RETRY_AUTHORIZATION_USED")
        _retry_source(quote(work_id, reference_id, asset.id), row["retry_unknown_call_id"])
        current.update(consumed_request_id=rid)
        st.insert_row(c, "cost_confirmations", row["id"], current, {"work_id": work_id})
    try:
        return tasks.submit_job(work_id, KIND, rid, payload, expected_revision=expected_revision)
    except tasks.IdempotencyConflict:
        # 同 RID 的并发页面提交可能各建一条随机确认；原任务的精确输入相同即重放，不重新收费。
        prior = replay_submission(work_id, rid, reference_id, asset.id, row["ceiling_fen"], row["retry_unknown_call_id"])
        if prior:
            return prior
        raise
    except Exception:
        with st.transaction() as c:
            current = st.get("cost_confirmations", row["id"]) or {}
            if current.get("consumed_request_id") == rid and not tasks.find_job(work_id, KIND, rid):
                current.pop("consumed_request_id", None)
                st.insert_row(c, "cost_confirmations", row["id"], current, {"work_id": work_id})
        raise


def replay_submission(work_id: str, request_id: str, reference_id: str, media_asset_id: str,
                      ceiling_fen: int, retry_unknown_call_id: str = "") -> Optional[dict]:
    """创建新的随机确认前重放，费用/输入变化仍按请求冲突处理。"""
    prior = tasks.find_job(work_id, KIND, request_id)
    if prior is None:
        return None
    p = prior.get("payload") or {}
    if (p.get("reference_snapshot_id") != reference_id or p.get("media_asset_id") != media_asset_id
            or p.get("total_ceiling_fen") != ceiling_fen
            or (p.get("retry_unknown_call_id") or "") != retry_unknown_call_id):
        raise tasks.IdempotencyConflict("同一 request_id 对应了不同的请求内容", detail=request_id)
    return dict(prior, replayed=True)


def has_user_confirmation(work_id: str, digest: str, min_fen: int) -> bool:
    return any(r.get("route") == ROUTE and r.get("request_digest") == digest
               and str(r.get("actor_id") or "").startswith("user") and int(r.get("ceiling_fen") or 0) >= min_fen
               for r in st.all_rows("cost_confirmations", "work_id=?", [work_id]))


def _call_key(job: dict, base_key: str, duration_ms: int) -> str:
    """同任务续跑复用台账；用户新建任务可重试明确失败，任何未确认调用仍禁止重发。"""
    rows = [row for row in st.all_rows("provider_jobs", "work_id=?", [job["work_id"]])
            if row.get("idempotency_key") == base_key
            or str(row.get("idempotency_key") or "").startswith(base_key + ":attempt:")]
    own = next((r for r in rows if r.get("parent_content_job_id") == job["id"]), None)
    if own:
        return own["idempotency_key"]
    for row in rows:
        if row.get("state") in ("queued", "leased", "waiting_external"):
            raise tasks.CallPending("该视频已有进行中的视觉请求，本次未发送，请等待原任务完成", detail=row["id"])
    for row in reversed(rows):
        if row.get("state") == "succeeded":
            try:
                qv.parse_analysis(row.get("result") or {}, duration_ms=duration_ms)
            except ContentRejected:
                continue
            return row["idempotency_key"]  # 可用响应已存在，旧 unknown 保留但无需再发付费请求。
    p = job.get("payload") or {}
    allowed = set()
    if p.get("retry_unknown_call_id"):
        conf = verify_confirmation(job["work_id"], p["reference_snapshot_id"], p["media_asset_id"],
                                   p.get("retry_confirmation_id") or "")
        if (conf.get("retry_paid_ack") is not True or conf.get("retry_unknown_call_id") != p["retry_unknown_call_id"]
                or conf.get("consumed_request_id") != job["request_id"] or conf.get("retry_base_key") != base_key):
            raise ContentRejected("重新提交授权不属于本次解析，请重新确认", "VISUAL_RETRY_SOURCE_INVALID")
        _retry_source(quote(job["work_id"], p["reference_snapshot_id"], p["media_asset_id"]),
                      p["retry_unknown_call_id"], own_job_id=job["id"])
        allowed = set(conf.get("retry_unknown_call_ids") or [])
    for row in rows:
        if row.get("state") in ("queued", "leased", "waiting_external", "unknown"):
            if row.get("parent_content_job_id") != job["id"]:
                if row.get("state") == "unknown" and row["id"] in allowed:
                    continue
                raise previous_unknown_error(row["id"])
    return base_key + ":attempt:" + job["id"] if rows else base_key


# ---------------------------------------------------------------- 任务处理器

def _visual_record(*, job: dict, asset: cc.MediaAsset, analysis: Dict[str, Any], audio: Dict[str, Any],
                   frames: list, env: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    timestamps = sorted(int(f["timestamp_ms"]) for f in frames)
    boundaries = [0, *timestamps, asset.duration_ms]
    span = {"start_ms": 0, "end_ms": asset.duration_ms, "frame_count": len(frames),
            "first_frame_ms": timestamps[0], "last_frame_ms": timestamps[-1],
            "max_frame_gap_ms": max(b - a for a, b in zip(boundaries, boundaries[1:]))}
    audio_view = {"tempo_bpm": audio.get("tempo_bpm"), "beat_times_ms": audio.get("beat_times_ms") or [],
                  "confidence": audio.get("confidence")}
    speech = analysis["speech_presence"]
    if audio.get("speech_presence") == "none" and speech == "uncertain":
        speech = "none"  # 本地音频能量为静音时，采信本地信号而不是模型猜测
    return {"state": "ready", "provider": "qwen", "model": env.get("model") or model_id,
            "analysis_version": qv.ANALYSIS_VERSION, "media_asset_id": asset.id, "media_sha256": asset.sha256,
            "duration_ms": asset.duration_ms, "coverage": [span], "summary": analysis["summary"],
            "speech_presence": speech, "music_presence": analysis["music_presence"], "audio": audio_view,
            "scenes": analysis["scenes"], "warnings": analysis["warnings"], "created_at": job["created_at"]}


def handle_visual_analysis(job: dict, *, transport=None) -> None:
    from . import visual_pipeline as vp
    p = job["payload"]
    work_id = job["work_id"]
    ref, asset = _bound(work_id, p.get("reference_snapshot_id"), p.get("media_asset_id"))
    q = quote(work_id, ref["id"], asset.id)
    listing = q["call_list"]
    if (p.get("model") != listing["model"] or p.get("analysis_version") != listing["analysis_version"]
            or p.get("request_digest") != q["request_digest"]):
        raise ContentRejected("解析模型或版本已变化，请重新确认本次解析费用", "COST_LIST_CHANGED")
    if not has_user_confirmation(work_id, q["request_digest"], int(p.get("total_ceiling_fen") or 0)):
        raise ContentRejected("请先由用户确认视觉分析费用", "VISUAL_COST_CONFIRMATION_REQUIRED")
    model_id = p.get("model") or qv.model()
    response_wait = qv.timeout_seconds()  # 配置错误须在台账进入 sending 之前被拒绝。
    api_key = os.environ.get(qv.KEY_ENV, "")
    lim = costs.visual_limits()
    ceiling = int(p.get("total_ceiling_fen") or 0)
    if ceiling < lim.reserve_fen:
        raise ContentRejected("已确认的费用上限低于当前预留额，请重新确认", "COST_AMOUNT_INVALID")

    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="media")
    path = str(media.resolve_path(asset))
    # 历史资产可能把音轨尾部计入时长；读取真实视频轨，不改写原资产或旧解析。
    asset = asset.model_copy(update={"duration_ms": int(media.probe_video(path)["duration_ms"])})

    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="sample")
    frames = vp.sample_frames(path, asset.duration_ms)

    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="audio")
    audio = vp.analyze_audio(path)

    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="vision")
    frames_digest = cc.digest([f["sha256"] for f in frames])
    request = {"reference": ref["id"], "media_sha256": asset.sha256, "model": model_id,
               "analysis_version": qv.ANALYSIS_VERSION, "frames": frames_digest}

    def send() -> tasks.CallOutcome:
        if not api_key:
            raise tasks.NotSent(f"未配置 {qv.KEY_ENV}，不发起视觉分析请求", "PROVIDER_NOT_CONFIGURED")
        env = qv.call_frames(model=model_id, frames=frames, audio_summary=audio, duration_ms=asset.duration_ms,
                             api_key=api_key, transport=transport, timeout=response_wait)
        return tasks.CallOutcome(response=env, usage=env["usage"], cost_fen=lim.price_fen,
                                 provider_request_id=env["provider_request_id"])

    base_key = f"visual:{ref['id']}:{asset.sha256}:{model_id}:{qv.ANALYSIS_VERSION}:{frames_digest}"
    call = tasks.run_call(parent=job, provider="qwen", model=model_id, capability="visual_analysis",
                          step="visual_analysis", round_no=0, request=request, send=send, billable=True,
                          reserve_fen=lim.reserve_fen,
                          lease_seconds=response_wait + 60,
                          idempotency_key=_call_key(job, base_key, asset.duration_ms))
    env = call["result"]
    analysis = qv.parse_analysis(env, duration_ms=asset.duration_ms)

    tasks.checkpoint(job)
    tasks.set_progress(job["id"], stage="persist")
    visual = _visual_record(job=job, asset=asset, analysis=analysis, audio=audio, frames=frames, env=env,
                            model_id=model_id)
    snapshot = dict(ref, id="ref_" + job["id"], parent_id=ref["id"], fetched_at=job["created_at"], visual=visual,
                    media_asset_id=asset.id, video_url=ref.get("video_url"), content_digest="",
                    extraction_version=str(ref["extraction_version"]).split("+")[0] + EXTRACTION_SUFFIX)
    with st.transaction() as c:
        saved = repo.save_reference(c, snapshot)
    tasks.finish_job(job["id"], "succeeded", result_refs={
        "reference_id": saved["id"], "visual_state": "ready", "media_asset_id": asset.id,
        "visual_digest": cc.digest(visual)})


def register_handlers() -> None:
    tasks.register(KIND, handle_visual_analysis)


def recover_cached_truncation(work_id: str, job_id: str) -> Dict[str, Any]:
    """只恢复已收到的完整画面字段，生成新参考；不发送模型请求、不改原终态或费用。

    用于旧版把注意事项尾部截断一律拒绝的任务。核心字段或全片时间线不完整时仍拒绝。
    恢复结果保留原模型/分析版本，用户自行选择为主参考。
    """
    from . import visual_pipeline as vp

    def rejected():
        return ContentRejected("没有可按原输入恢复的完整画面结果", "VISUAL_CACHED_RECOVERY_UNAVAILABLE")

    job = tasks.get_job(job_id)
    if (not job or job.get("work_id") != work_id or job.get("kind") != KIND
            or job.get("state") != "failed" or job.get("cancel_requested")
            or (job.get("error") or {}).get("code") != "OUTPUT_TRUNCATED"):
        raise rejected()
    calls = tasks.jobs_calls(job_id)
    if len(calls) != 1:
        raise rejected()
    call = calls[0]
    p = job["payload"]
    env = call.get("result") or {}
    if (call.get("state") != "succeeded" or call.get("work_id") != work_id
            or call.get("provider") != "qwen" or call.get("capability") != KIND
            or call.get("model") != p.get("model") or env.get("finish_reason") != "length"):
        raise rejected()
    ref, asset = _bound(work_id, p.get("reference_snapshot_id"), p.get("media_asset_id"))
    listing = {"route": ROUTE, "work_id": work_id, "reference_snapshot_id": ref["id"],
               "media_asset_id": asset.id, "media_sha256": asset.sha256,
               "model": p.get("model"), "analysis_version": p.get("analysis_version")}
    if not listing["analysis_version"] or p.get("request_digest") != cc.digest(listing):
        raise rejected()
    path = media.resolve_path(asset)
    digest = hashlib.sha256()
    with path.open("rb") as video:
        for chunk in iter(lambda: video.read(1024 * 1024), b""):
            digest.update(chunk)
    if "sha256:" + digest.hexdigest() != asset.sha256:
        raise rejected()
    asset = asset.model_copy(update={"duration_ms": int(media.probe_video(str(path))["duration_ms"])})
    analysis = qv.parse_analysis(env, duration_ms=asset.duration_ms)
    frames = vp.sample_frames(str(path), asset.duration_ms)
    frames_digest = cc.digest([f["sha256"] for f in frames])
    expected_key = (f"visual:{ref['id']}:{asset.sha256}:{p['model']}:"
                    f"{p['analysis_version']}:{frames_digest}")
    request = {"reference": ref["id"], "media_sha256": asset.sha256, "model": p["model"],
               "analysis_version": p["analysis_version"], "frames": frames_digest}
    if _base_key(call) != expected_key or call.get("args_digest") != tasks.payload_digest(request):
        raise rejected()
    audio = vp.analyze_audio(str(path))
    record = _visual_record(job=job, asset=asset, analysis=analysis, audio=audio,
                            frames=frames, env=env, model_id=p["model"])
    record["analysis_version"] = p["analysis_version"]
    record["warnings"].append(f"从已保存调用 {call['id']} 本地恢复完整画面字段，未再次调用模型；原请求及费用记录保留。")
    snapshot = dict(ref, id="ref_recovered_" + call["id"], parent_id=ref["id"],
                    fetched_at=job["created_at"], visual=record, media_asset_id=asset.id, content_digest="",
                    extraction_version=str(ref["extraction_version"]).split("+")[0] + "+" + p["analysis_version"])
    with st.transaction() as c:
        if tasks.get_job(job_id) != job or st.get("provider_jobs", call["id"]) != call:
            raise rejected()
        saved = repo.save_reference(c, snapshot)
    return {"reference_id": saved["id"], "provider_call_id": call["id"], "new_paid_calls": 0}


def find_confirmation_id(work_id: str, reference_id: str, media_asset_id: str) -> str:
    """当前调用清单上、用户已确认的最新记录；没有则返回空串。供主控读取，不创建确认。"""
    try:
        digest = quote(work_id, reference_id, media_asset_id)["request_digest"]
    except ContentRejected:
        return ""
    rows = [r for r in st.all_rows("cost_confirmations", "work_id=?", [work_id])
            if r.get("route") == ROUTE and r.get("request_digest") == digest
            and str(r.get("actor_id") or "").startswith("user")]
    return max(rows, key=lambda r: r.get("confirmed_at", ""))["id"] if rows else ""


def reference_needs_visual(work: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """分镜路线下，当前主参考既无有效转写也无 ready 画面分析时返回该参考，否则 None。"""
    if (work.get("brief") or {}).get("content_route") != "short_video_storyboard":
        return None
    ref = repo.get_reference(work.get("reference_snapshot_id") or "") if work.get("reference_snapshot_id") else None
    if ref is None or cc.transcript_is_usable(ref["transcript"]) or cc.visual_state(ref) == "ready":
        return None
    return ref


def preparation_inputs(work: Dict[str, Any]) -> Dict[str, str]:
    ref = reference_needs_visual(work)
    if ref is None:
        return {"reference_snapshot_id": "", "media_asset_id": "", "visual_confirmation_id": ""}
    media_id = media.reference_media_asset_id(work["id"], ref["id"])
    return {"reference_snapshot_id": ref["id"], "media_asset_id": media_id,
            "visual_confirmation_id": find_confirmation_id(work["id"], ref["id"], media_id) if media_id else ""}
