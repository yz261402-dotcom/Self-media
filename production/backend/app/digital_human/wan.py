"""Wan 3.0 单任务编排：费用摘要、持久提交意图、原 ID 查询和原片登记。"""
from __future__ import annotations

import threading
from typing import Any, Dict
from weakref import WeakValueDictionary

from .. import config, jobs, storage as st
from . import cost, providers, routes, wan_provider as provider
from .errors import TalkingHeadBlocked, TalkingHeadRejected
from .wan_media import prepare_media, prepare_native_media
from .wan_inputs import resolve_source

_POLL_LOCKS = WeakValueDictionary()
_POLL_LOCKS_GUARD = threading.Lock()
_TERMINAL_RUN_STATES = ("succeeded", "failed", "cancelled")


def _poll_lock(run_id: str):
    # Current deployment has one API process. Keep one lock per live polling run;
    # weak references release idle locks without retaining every historical run.
    with _POLL_LOCKS_GUARD:
        lock = _POLL_LOCKS.get(run_id)
        if lock is None:
            lock = threading.RLock()
            _POLL_LOCKS[run_id] = lock
        return lock


def prepare_original(run: Dict[str, Any], downloaded: Dict[str, Any]) -> Dict[str, Any]:
    from .wan_audio import prepare_original as restore
    return restore(run, downloaded)


def _current_selection(work_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
    work = st.get("works", work_id) or {}
    current = st.get("tasks", (task or {}).get("id") or "") or {}
    if (work.get("route") != routes.ROUTE_WAN or not current or current.get("work_id") != work_id
            or work.get("current_task_id") != current["id"]):
        raise TalkingHeadRejected("WAN_ROUTE_SELECTION_CHANGED", "作品路线或当前任务已变化；此次 Wan 调用尚未提交，请重新确认。")
    routes.assert_route_selected(current, routes.ROUTE_WAN)
    return current


def current_call_list(work_id: str, *, prompt: str = "", resolution: str = "720p",
                      duration_seconds: int = 5) -> Dict[str, Any]:
    work = st.get("works", work_id)
    if not work:
        raise TalkingHeadRejected("WORK_NOT_FOUND", "作品不存在。")
    params = provider.assert_native_input(duration_seconds=duration_seconds, prompt=prompt, resolution=resolution)
    source = resolve_source(work)
    listing = {"route": routes.ROUTE_WAN, "provider": provider.PROVIDER, "model": provider.MODEL,
               "audio_id": None, "audio_hash": None, "audio_duration_ms": None,
               "variant_id": source.get("variant_id"),
               "motion_ref_id": None, "plate_id": None, "identity_id": source.get("identity_id"),
               "source_asset_id": source["asset_id"], "source_asset_hash": source["asset_hash"],
               "prompt": params["prompt"], "resolution": params["resolution"], "segment_count": 1,
               "effective_prompt": params["effective_prompt"], "duration_seconds": params["duration_seconds"],
               "generated_seconds": params["generated_seconds"], "input_mode": "reference_image",
               "generation_mode": "image_prompt_native_audio",
               "ratio": "9:16", "audio": True, "sound_mode": "native_audio",
               "final_audio_mode": "provider_native_audio"}
    listing["digest"] = jobs.args_digest(listing)
    return listing


def submit_wan(work_id: str, *, task=None, prompt: str = "", resolution: str = "720p",
               duration_seconds: int = 5, transport=None, dry_run=None,
               replacement_authorized: bool = False) -> Dict[str, Any]:
    from . import submission as sub
    preflight = routes.require_preflight(work_id, task=task, route=routes.ROUTE_WAN, require_plate=False)
    task = _current_selection(work_id, task)
    work = st.get("works", work_id)
    listing = current_call_list(work_id, prompt=prompt, resolution=resolution, duration_seconds=duration_seconds)
    digest = listing["digest"]
    zone = sub.resubmit_guard(work_id, digest, replacement_authorized=replacement_authorized)
    est = cost.estimate(routes.ROUTE_WAN, duration_seconds=listing["duration_seconds"],
                        resolution=listing["resolution"], segment_count=1)
    authorization = sub._gate_with_ledger(work_id, route=routes.ROUTE_WAN, provider=provider.PROVIDER,
        model=provider.MODEL, capability=routes.CAP_SHORT_VIDEO_CLOUD, digest=digest,
        estimated_fen=est["estimated_fen"], task=task, call_list=listing, provider_params=listing)
    suffix = ""
    if replacement_authorized and zone.get("prior"):
        prior_confirmations = {(sub.get_run(rid) or {}).get("cost_reservation_id") for rid in zone["prior"]}
        if authorization["confirmation_id"] in prior_confirmations:
            raise TalkingHeadBlocked("RESUBMIT_REQUIRES_AUTHORIZATION",
                                     "Wan 重做属于新的付费调用；请重新确认清单与金额后再明确授权重做。")
        suffix = ":retry:" + authorization["confirmation_id"]
    key = f"wan:{work_id}:{digest}{suffix}"
    source = preflight["source"]
    if (source["asset_id"] != listing["source_asset_id"] or source["asset_hash"] != listing["source_asset_hash"]
            or source.get("identity_id") != listing["identity_id"] or source.get("variant_id") != listing["variant_id"]):
        raise TalkingHeadRejected("COST_LIST_CHANGED", "前置检查期间作品输入已变化；请重新确认调用清单。")
    snapshot = {"work_id": work_id, "identity_id": source.get("identity_id"),
                "variant_id": source.get("variant_id"), "source_asset_id": source["asset_id"],
                "source_asset_hash": source["asset_hash"], "audio_asset_id": None, "audio_hash": None,
                "plate_id": None, "plate_hash": None, "motion_ref_id": None}
    params = dict(listing, input_snapshot=snapshot, segment_index=0, global_offset_ms=0)
    is_mock = config.resolve_mode() == "mock" if dry_run is None else bool(dry_run)
    segment = {"index": 0, "segment_id": "wan_native_video", "start_ms": 0,
               "end_ms": listing["duration_seconds"] * 1000, "duration_ms": listing["duration_seconds"] * 1000,
               "global_offset_ms": 0, "single_call": True}
    def result(run):
        return {"submitted": run["state"] in ("submitted", "waiting_provider"), "is_mock": is_mock,
                "route": routes.ROUTE_WAN, "runs": [run], "segments": [segment], "estimate": est,
                "authorization": authorization, "replayed": bool(run.get("replayed")),
                "max_concurrency": 1, "pending_segments": 0, "driver_started": False,
                "note": "图片与提示词单次生成原生有声短片；远端受理不等于成功，原片仍须观看审核。"}
    if is_mock:
        run = sub.start_run(work_id, route=routes.ROUTE_WAN, provider=provider.PROVIDER,
            model=provider.MODEL, capability=routes.CAP_SHORT_VIDEO_CLOUD, request_digest=digest,
            idempotency_key=key, task_id=(task or {}).get("id"), is_mock=True, provider_params=params)
        return result(run)
    if not transport:
        raise TalkingHeadBlocked("WAN_TRANSPORT_UNAVAILABLE", "未提供 Wan 网络通道，尚未提交。")
    # Replay before touching storage: refreshing an existing job needs no second upload.
    existing = [r for r in sub.runs_for_work(work_id) if not r.get("is_mock")
                and r.get("provider") == provider.PROVIDER and r.get("idempotency_key") == key
                and r.get("state") in ("intent", "submitted", "waiting_provider", "succeeded", "unknown")]
    if existing:
        return result(dict(existing[-1], replayed=True))
    root = provider.endpoint()
    api_key = provider.credentials()
    media = prepare_native_media(source)
    request = provider.build_native_submit_request(image_url=media["image_url"],
        prompt=listing["prompt"], duration_seconds=listing["duration_seconds"], resolution=listing["resolution"],
        api_key=api_key, base_url=root)
    # Asset bindings may have changed while media was being validated/published.
    if current_call_list(work_id, prompt=prompt, resolution=resolution, duration_seconds=duration_seconds)["digest"] != digest:
        raise TalkingHeadRejected("COST_LIST_CHANGED", "素材准备期间作品输入已变化；请重新确认调用清单。")
    task = _current_selection(work_id, task)
    routes.require_preflight(work_id, task=task, route=routes.ROUTE_WAN, require_plate=False)
    params["endpoint"] = root
    run = sub.start_run(work_id, route=routes.ROUTE_WAN, provider=provider.PROVIDER,
        model=provider.MODEL, capability=routes.CAP_SHORT_VIDEO_CLOUD, request_digest=digest,
        idempotency_key=key, task_id=(task or {}).get("id"), provider_params=params,
        cost_reservation_id=authorization["confirmation_id"], reuse_existing=True)
    if run.get("replayed"):
        return result(run)
    try:
        job = jobs.submit(work_id=work_id, capability=routes.CAP_SHORT_VIDEO_CLOUD, provider=provider.PROVIDER,
            model=provider.MODEL, args=listing, idempotency_key=key, task_id=(task or {}).get("id"),
            billable=True, amount_fen=est["estimated_fen"], note="Wan 官方短视频，图片与提示词生成原生声音")
        run = sub._update_run(run["id"], job_id=job["id"])
        if job["state"] == "blocked":
            code = job.get("block_code", "JOB_BLOCKED")
            message = job.get("block_detail", "作业被预算门禁阻断")
            sub.record_failure(run["id"], code=code, message=message)
            raise TalkingHeadBlocked(code, message)
        if job.get("replayed"):
            sub.mark_run_unknown(run["id"], "已有同一付费作业但缺少可复用运行记录；先对账。")
            raise TalkingHeadBlocked("RESUBMIT_REQUIRES_RECONCILIATION", "已有 Wan 原作业；请先对账。")
        jobs.acquire_lease(job["id"], f"submit:{run['id']}", ttl_seconds=120)
    except jobs.JobRejected as error:
        sub.record_failure(run["id"], code=error.code, message=error.message)
        raise TalkingHeadBlocked(error.code, error.message) from error
    run = sub._update_run(run["id"], state="submitted")
    try:
        parsed = provider.parse_submit_response(transport(request))
    except (TalkingHeadBlocked, TalkingHeadRejected) as error:
        if error.code in ("WAN_AUTH_FAILED", "WAN_QUOTA_EXHAUSTED", "WAN_INPUT_REJECTED"):
            sub.record_failure(run["id"], code=error.code, message=error.message)
            raise
        parsed = {"state": "unknown"}
    except Exception:
        parsed = {"state": "unknown"}
    if parsed["state"] != "submitted":
        sub.mark_run_unknown(run["id"], "Wan 提交响应未能核对，可能已受理和计费；保留台账，先对账。")
        raise TalkingHeadBlocked("WAN_SUBMISSION_UNKNOWN", "Wan 提交结果未知，可能已计费；请先对账，不要再次生成。",
                                 {"run_id": run["id"], "job_id": job["id"]})
    run = sub._update_run(run["id"], remote_job_id=parsed["remote_job_id"], state="waiting_provider")
    jobs.release_lease(job["id"], f"submit:{run['id']}", reason="Wan 已受理，后续只查询原任务")
    return result(run)


def poll_wan_run(run_id: str, *, transport=None) -> Dict[str, Any]:
    """Serialize each run's poll/recovery in the current single API process.

    No database transaction spans HTTP or FFmpeg. Multiple API processes require
    a shared claim/CAS mechanism before this process-local lock can be relied on.
    """
    with _poll_lock(run_id):
        return _poll_wan_run_locked(run_id, transport=transport)


def _poll_wan_run_locked(run_id: str, *, transport=None) -> Dict[str, Any]:
    from . import submission as sub
    run = sub.get_run(run_id)
    if not run:
        raise TalkingHeadRejected("PROVIDER_RUN_NOT_FOUND", "Wan 运行记录不存在。")
    if run.get("provider") != provider.PROVIDER or run.get("route") != routes.ROUTE_WAN:
        raise TalkingHeadRejected("WAN_RUN_MISMATCH", "该运行记录不是 Wan 任务。")
    if run["state"] in _TERMINAL_RUN_STATES:
        return {"run": run, "state": run["state"]}
    if run.get("is_mock"):
        raise TalkingHeadBlocked("MOCK_RUN_NOT_POLLABLE", "占位运行没有真实 Wan 任务。")
    if not run.get("remote_job_id"):
        return {"run": sub.mark_run_unknown(run_id, "缺少 Wan 原 task_id，请先对账；不得重提。"), "state": "unknown"}
    if not transport:
        raise TalkingHeadBlocked("WAN_TRANSPORT_UNAVAILABLE", "未提供 Wan 查询通道。")
    params = run.get("provider_params") or {}
    if not params.get("endpoint"):
        raise TalkingHeadBlocked("WAN_ENDPOINT_SNAPSHOT_MISSING", "旧运行缺少原工作空间地址；请先对账，不使用当前配置猜测。")
    request = provider.build_query_request(task_id=run["remote_job_id"], api_key=provider.credentials(),
                                           base_url=params["endpoint"])
    try:
        response = transport(request)
        document = provider._document(response)
        output = document.get("output")
        if isinstance(output, dict) and output.get("task_id") and output["task_id"] != run["remote_job_id"]:
            parsed = {"state": "unavailable"}
        else:
            parsed = provider.parse_query_response(response)
    except Exception:
        parsed = {"state": "unavailable"}
    # Other reconciliation paths can finish a run while this GET is in flight.
    # Never apply that older response over an already persisted terminal result.
    latest = sub.get_run(run_id)
    if latest and latest["state"] in _TERMINAL_RUN_STATES:
        return {"run": latest, "state": latest["state"]}
    if parsed["state"] == "unavailable":
        return {"run": run, "state": run["state"], "transport_unavailable": True,
                "note": "查询暂不可用，保留原状态与原 task_id。"}
    if parsed["state"] == "running":
        return {"run": sub._update_run(run_id, state="waiting_provider"), "state": "waiting_provider"}
    if parsed["state"] == "unknown":
        return {"run": sub.mark_run_unknown(run_id, parsed["reason"]), "state": "unknown"}
    if parsed["state"] == "failed":
        job = jobs.get_job(run.get("job_id")) if run.get("job_id") else None
        if job and job["state"] == "unknown":
            jobs.resolve_unknown(job["id"], "failed", {"source": "wan_original_task_query",
                                                        "remote_job_id": run["remote_job_id"]})
        elif job and job["state"] not in jobs.TERMINAL_STATES:
            worker = f"poll:{run_id}"
            jobs.acquire_lease(job["id"], worker, ttl_seconds=30)
            jobs.fail(job["id"], worker, {"code": "WAN_REMOTE_FAILED", "message": parsed["reason"]})
        return {"run": sub.record_failure(run_id, code="WAN_REMOTE_FAILED", message=parsed["reason"]), "state": "failed"}
    try:
        downloaded = providers.download_and_verify(parsed["video_url"], transport,
                                                    declared_duration_ms=int(params["generated_seconds"]) * 1000)
    except (TalkingHeadBlocked, TalkingHeadRejected):
        # URL expiry or bad bytes do not create a new job; the original task stays queryable.
        raise
    except Exception as error:
        raise TalkingHeadBlocked("WAN_VIDEO_DOWNLOAD_FAILED", "Wan 原片下载中断；保留原 task_id，下次查询原任务获取下载地址。") from error
    if params.get("generation_mode") == "image_prompt_native_audio":
        from . import wan_native
        downloaded = wan_native.prepare_original(run, downloaded)
        latest = sub.get_run(run_id)
        if latest and latest["state"] in _TERMINAL_RUN_STATES:
            return {"run": latest, "state": latest["state"]}
        return wan_native.register_original(run_id, downloaded)
    # Historical runs keep their original reference-audio restoration contract.
    downloaded = prepare_original(run, downloaded)
    probe = downloaded["probe"]
    if not probe.get("has_audio_track"):
        raise TalkingHeadRejected("WAN_OUTPUT_AUDIO_MISSING", "Wan 返回原片没有音轨；不登记为合格原片，按原任务核查。")
    restoration = downloaded["audio_restoration"]
    required_ms = restoration.get("audio_duration_ms", params["audio_duration_ms"])
    if int(probe.get("duration_ms") or 0) < int(required_ms):
        raise TalkingHeadRejected("WAN_OUTPUT_TOO_SHORT", "Wan 原片短于完整原音频；不得用截字、变速或 -shortest 掩盖不足。")
    latest = sub.get_run(run_id)
    if latest and latest["state"] in _TERMINAL_RUN_STATES:
        return {"run": latest, "state": latest["state"]}
    sub._update_run(run_id, provider_params=dict(params, audio_restoration=restoration))
    result = sub._register_original(run_id, downloaded,
        note="Wan 3.0 审核预览；供应商原片另存留证，已恢复完整原音频，口型与身份效果仍须独立审核。")
    video = result["video"]
    video["audio_restoration"] = restoration
    st.put("videos", video["id"], video, {"work_id": run["work_id"]})
    from . import videos
    from .plate_source import resolve_plate_source
    work = st.get("works", run["work_id"]) or {}
    snapshot = params["input_snapshot"]
    stale = work.get("route") != routes.ROUTE_WAN
    try:
        current_source = resolve_plate_source(work)
        stale = stale or (current_source["asset_id"] != snapshot.get("source_asset_id")
                          or current_source["asset_hash"] != snapshot.get("source_asset_hash"))
    except (TalkingHeadBlocked, TalkingHeadRejected):
        stale = True
    if stale:
        result["video"] = videos.revoke(video["id"], "Wan 旧任务返回时人物原图或路线已变化，仅保留历史原片。")
    return result
