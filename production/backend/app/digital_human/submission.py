"""口播提交编排：路线选择 → 前置门禁 → 费用门禁 → 提交 → 查询 → 登记产物。

这里集中体现 T05 的几条"不许含糊"：

- **提交前的门禁是叠加的，不是或的关系。** 路线确认、人物/变体审核、稿件与计划冻结、
  音频确认、底片审核（M2）、费用确认与预算授权，任何一个不过都停在原地。
- **失败保留，不自动回退。** OmniHuman 失败不会切到 M2，也不会自动改用别的云口播；
  M2 失败不会偷偷跑一次 MuseTalk 修补。失败文件、远端 ID、参数、费用一律留证。
- **结果未知先对账。** 提交丢响应 → `unknown`。既不重提（可能已有费用），
  也不改判（没有证据）。要重做必须显式授权。
"""
from __future__ import annotations

import os
import json
import threading
from typing import Any, Dict, List, Optional

from .. import config, jobs, storage as st
from ..contracts import ProviderRun, now_iso
from ..tts import audio as tts_audio
from . import cost as dh_cost
from . import providers as pv
from . import routes as dh_routes
from . import videos as vd
from .errors import TalkingHeadBlocked, TalkingHeadRejected

RUN_TABLE = "provider_runs"
_ORIGINAL_REGISTRATION_LOCK = threading.RLock()


def call_list_digest(route: str, *, audio_id: str, audio_hash: str, variant_id: Optional[str],
                     motion_ref_id: Optional[str], prompt: str, resolution: str,
                     segment_count: int, plate_id: Optional[str] = None,
                     identity_id: Optional[str] = None, source_asset_id: Optional[str] = None,
                     source_asset_hash: Optional[str] = None) -> str:
    """调用清单摘要。金额/输入/路线变化必须导致摘要变化，从而触发重新确认。"""
    payload = {
        "route": route, "audio_id": audio_id, "audio_hash": audio_hash,
        "variant_id": variant_id, "motion_ref_id": motion_ref_id,
        "plate_id": plate_id, "prompt": prompt, "resolution": resolution,
        "segment_count": int(segment_count),
    }
    if route == dh_routes.ROUTE_OMNIHUMAN and any((identity_id, source_asset_id, source_asset_hash)):
        payload.update(identity_id=identity_id, source_asset_id=source_asset_id,
                       source_asset_hash=source_asset_hash)
    return jobs.args_digest(payload)


def current_call_list(work_id: str, *, route: str, prompt: str = "",
                      resolution: str = "1080p", duration_seconds: int = 5) -> Dict[str, Any]:
    """从作品当前绑定推导"这次要提交什么"及其摘要。

    提交与费用确认必须调用同一个函数。否则会出现"用户确认的和实际提交的不是一件事"，
    而这种不一致在金额上完全看不出来——金额一致、清单不同，正是最危险的一类偏差。
    """
    dh_routes.route_spec(route)
    if route == dh_routes.ROUTE_WAN:
        from .wan import current_call_list as wan_call_list
        return wan_call_list(work_id, prompt=prompt, resolution=resolution, duration_seconds=duration_seconds)
    work = st.get("works", work_id) or {}
    audio = tts_audio.final_audio(work_id) or {}
    if not audio:
        raise TalkingHeadBlocked("FINAL_AUDIO_REQUIRED", "尚无已确认最终音频，调用清单不成立")
    audio_id = audio["id"]
    audio_hash = audio.get("file_hash", "")
    variant_id = work.get("variant_id")

    if route == dh_routes.ROUTE_OMNIHUMAN:
        from .plate_source import resolve_plate_source

        source = resolve_plate_source(work)
        audio_ms = int(audio.get("duration_ms") or 0)
        dh_routes.assert_omnihuman_audio_duration_allowed(audio_ms)
        segment_count = 1
        return {
            "route": route, "audio_id": audio_id, "audio_hash": audio_hash,
            "variant_id": variant_id, "motion_ref_id": None, "plate_id": None,
            "identity_id": source["identity_id"], "source_asset_id": source["asset_id"],
            "source_asset_hash": source["asset_hash"],
            "prompt": prompt, "resolution": resolution, "segment_count": segment_count,
            "digest": call_list_digest(route, audio_id=audio_id, audio_hash=audio_hash,
                                       variant_id=variant_id, motion_ref_id=None,
                                       prompt=prompt, resolution=resolution,
                                       segment_count=segment_count,
                                       identity_id=source["identity_id"], source_asset_id=source["asset_id"],
                                       source_asset_hash=source["asset_hash"]),
        }

    from . import plates as pl

    plate = pl.approved_plate_for(work_id)
    return {
        "route": route, "audio_id": audio_id, "audio_hash": audio_hash,
        "variant_id": variant_id,
        "motion_ref_id": (plate or {}).get("motion_ref_id"),
        "plate_id": (plate or {}).get("id"),
        "prompt": "", "resolution": "native", "segment_count": 1,
        "digest": call_list_digest(route, audio_id=audio_id, audio_hash=audio_hash,
                                   variant_id=variant_id,
                                   motion_ref_id=(plate or {}).get("motion_ref_id"),
                                   prompt="", resolution="native", segment_count=1,
                                   plate_id=(plate or {}).get("id")),
    }


def _require_public_url(storage_uri: str, what: str) -> str:
    """供应商只能读 URL。内部相对路径不是"差不多能用"，而是不成立。"""
    if not storage_uri or not storage_uri.startswith(("http://", "https://")):
        raise TalkingHeadBlocked(
            "MEDIA_URL_NOT_READABLE",
            f"{what} 缺少供应商可读取的媒体 URL（当前：{storage_uri or '空'}）；"
            "接口只接受可读取 URL，不接受内部存储路径",
            {"what": what, "storage_uri": storage_uri or ""},
        )
    return storage_uri


def _credentials() -> tuple:
    ak = os.environ.get(pv.SECRET_ENV_AK, "")
    sk = os.environ.get(pv.SECRET_ENV_SK, "")
    if not ak or not sk:
        raise TalkingHeadBlocked(
            "OMNIHUMAN_CREDENTIALS_MISSING",
            f"未配置 {pv.SECRET_ENV_AK} / {pv.SECRET_ENV_SK}：不发起真实请求，也不假装已提交",
        )
    return ak, sk


# ---------- ProviderRun ----------

def start_run(work_id: str, *, route: str, provider: str, model: str, capability: str,
              request_digest: str, idempotency_key: str, task_id: Optional[str] = None,
              is_mock: bool = False, provider_params: Optional[Dict[str, Any]] = None,
              cost_reservation_id: Optional[str] = None,
              reuse_existing: bool = False) -> Dict[str, Any]:
    run = ProviderRun(
        id=st.new_id("prun"), work_id=work_id, task_id=task_id, route=route,
        provider=provider, model=model, capability=capability,
        capability_snapshot_id="t05-2026-09-23",
        request_digest=request_digest, idempotency_key=idempotency_key,
        state="intent", is_mock=is_mock, provider_params=provider_params or {},
        cost_reservation_id=cost_reservation_id,
    )
    if reuse_existing:
        # 把同清单查重与意图登记放在同一写事务中，双击/并发请求只能有一个提交方。
        with st.transaction() as connection:
            rows = connection.execute("SELECT data FROM provider_runs WHERE work_id=?", [work_id]).fetchall()
            prior = [json.loads(row["data"]) for row in rows]
            eligible = [item for item in prior if item.get("request_digest") == request_digest
                        and item.get("provider") == provider and item.get("route") == route
                        and item.get("capability") == capability and not item.get("is_mock")
                        and (route not in (dh_routes.ROUTE_OMNIHUMAN, dh_routes.ROUTE_WAN)
                             or item.get("idempotency_key") == idempotency_key)
                        and item.get("state") in ("intent", "submitted", "waiting_provider", "succeeded", "unknown")]
            if eligible:
                chosen = sorted(eligible, key=lambda item: (item.get("state") == "succeeded", item.get("created_at", "")), reverse=True)[0]
                return dict(chosen, replayed=True)
            st.insert_row(connection, RUN_TABLE, run.id, run.model_dump(), {"work_id": work_id})
    else:
        st.put(RUN_TABLE, run.id, run.model_dump(), {"work_id": work_id})
    return run.model_dump()


def _input_snapshot(work: Dict[str, Any], audio: Dict[str, Any],
                    plate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"work_id": work["id"], "identity_id": work.get("identity_id"),
            "variant_id": work.get("variant_id"), "audio_asset_id": audio.get("id"),
            "audio_hash": audio.get("file_hash"), "plate_id": (plate or {}).get("id"),
            "plate_hash": (plate or {}).get("file_hash"),
            "motion_ref_id": (plate or {}).get("motion_ref_id"),
            "source_asset_id": (plate or {}).get("source_asset_id"),
            "source_asset_hash": (plate or {}).get("source_asset_hash")}


def _run_inputs(run: Dict[str, Any]) -> Dict[str, Any]:
    """取提交时输入；旧 M2 仅能由底片与清单摘要恢复，不借当前作品猜身份。"""
    params = run.get("provider_params") or {}
    snapshot = params.get("input_snapshot")
    if run.get("route") == dh_routes.ROUTE_M2:
        plate = vd.get_video(params.get("plate_id") or "") or {}
        audio = tts_audio.get_audio(plate.get("audio_asset_id") or "") or {}
        reconstructed = _input_snapshot({"id": run["work_id"], "identity_id": plate.get("identity_id"),
                                         "variant_id": plate.get("variant_id")}, audio, plate)
        digest = call_list_digest(dh_routes.ROUTE_M2, audio_id=audio.get("id"),
                                  audio_hash=audio.get("file_hash", ""), variant_id=plate.get("variant_id"),
                                  motion_ref_id=plate.get("motion_ref_id"), prompt="", resolution="native",
                                  segment_count=1, plate_id=plate.get("id"))
        if (plate.get("work_id") != run["work_id"] or audio.get("work_id") != run["work_id"]
                or not plate.get("identity_id") or not audio.get("file_hash")
                or digest != run.get("request_digest") or (snapshot is not None and snapshot != reconstructed)):
            raise TalkingHeadBlocked("RUN_INPUT_SNAPSHOT_UNVERIFIABLE", "运行记录的底片、音频或清单摘要不匹配，先对账，不能按当前人物登记旧结果")
        return reconstructed
    if not snapshot or snapshot.get("work_id") != run["work_id"]:
        raise TalkingHeadBlocked("RUN_INPUT_SNAPSHOT_UNVERIFIABLE", "缺少提交时的输入快照，不能用当前作品绑定解释旧结果")
    return snapshot


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    return st.get(RUN_TABLE, run_id)


def runs_for_work(work_id: str) -> List[Dict[str, Any]]:
    rows = st.all_rows(RUN_TABLE, "work_id=?", [work_id])
    return sorted(rows, key=lambda r: r.get("created_at", ""))


def _update_run(run_id: str, **fields: Any) -> Dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise TalkingHeadRejected("PROVIDER_RUN_NOT_FOUND", f"运行记录不存在：{run_id}")
    run.update(fields)
    run["updated_at"] = now_iso()
    st.put(RUN_TABLE, run_id, run, {"work_id": run.get("work_id")})
    return run


def mark_run_unknown(run_id: str, reason: str) -> Dict[str, Any]:
    """提交丢响应：记未知，等对账。不回写 succeeded/failed。"""
    run = get_run(run_id) or {}
    job_id = run.get("job_id")
    if job_id:
        try:
            jobs.mark_unknown(job_id, reason=reason)
        except jobs.JobRejected:
            pass
    return _update_run(run_id, state="unknown",
                       last_error={"code": "RESULT_UNKNOWN", "message": reason})


def record_blocked_attempt(work_id: str, *, route: str, provider: str, model: str,
                           capability: str, request_digest: str, error,
                           provider_params: Optional[Dict[str, Any]] = None,
                           task_id: Optional[str] = None) -> Dict[str, Any]:
    """被门禁拦下的提交尝试也要入账。

    这类尝试没有外部副作用，所以"看起来可以不留痕"。但审计真正要回答的问题是
    "谁在什么时候试过提交、被哪条规则拦下"——不记就无法回答，
    也无法证明门禁确实拦住了，而不是碰巧没跑。状态用 `blocked`，
    与真正的失败 `failed` 区分开：前者没出去，后者出去了但没成。
    """
    run = start_run(work_id, route=route, provider=provider, model=model,
                    capability=capability, request_digest=request_digest,
                    idempotency_key=f"blocked:{work_id}:{request_digest}",
                    task_id=task_id, is_mock=False,
                    provider_params=provider_params or {})
    return _update_run(run["id"], state="blocked",
                       last_error={"code": error.code, "message": error.message})


def _gate_with_ledger(work_id: str, *, route: str, provider: str, model: str,
                      capability: str, digest: str, estimated_fen: int,
                      task: Optional[Dict[str, Any]],
                      provider_params: Optional[Dict[str, Any]] = None,
                      call_list: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """费用/预算门禁。被拦下时先把这次尝试记进台账，再抛出原错误。"""
    try:
        return dh_cost.assert_submission_allowed(work_id, route=route,
                                                 request_digest=digest,
                                                 estimated_fen=estimated_fen,
                                                 call_list=call_list)
    except (TalkingHeadBlocked, TalkingHeadRejected) as e:
        record_blocked_attempt(work_id, route=route, provider=provider, model=model,
                               capability=capability, request_digest=digest, error=e,
                               provider_params=provider_params,
                               task_id=(task or {}).get("id"))
        raise


def record_failure(run_id: str, *, code: str, message: str,
                   evidence: str = "") -> Dict[str, Any]:
    """失败落证：文件、远端 ID、参数、费用都留着；并显式声明没有自动回退。"""
    run = get_run(run_id)
    if not run:
        raise TalkingHeadRejected("PROVIDER_RUN_NOT_FOUND", f"运行记录不存在：{run_id}")
    if run.get("is_mock"):
        return _update_run(run_id, state="failed",
                           last_error={"code": code, "message": message},
                           auto_fallback=False)
    job_id = run.get("job_id")
    if job_id:
        job = jobs.get_job(job_id)
        if job and job.get("state") == "leased":
            try:
                jobs.fail(job_id, job["lease"]["worker_id"],
                          {"code": code, "message": message})
            except jobs.JobRejected:
                pass
    config.record_provider_evidence(
        run.get("provider", ""), run.get("capability", ""), "failed",
        evidence or f"作业失败：{code}",
        detail=f"run={run_id} route={run.get('route')} remote={run.get('remote_job_id')}")
    return _update_run(run_id, state="failed",
                       last_error={"code": code, "message": message},
                       auto_fallback=False)


def resubmit_guard(work_id: str, request_digest: str,
                   *, replacement_authorized: bool = False) -> Dict[str, Any]:
    """同一清单失败/未知过，就不能"再点一次"：先对账或显式授权重做。"""
    prior = [r for r in runs_for_work(work_id)
             if r.get("request_digest") == request_digest
             and r.get("state") in ("failed", "unknown")]
    if not prior:
        return {"ok": True, "prior": []}
    unknown = [r for r in prior if r.get("state") == "unknown"]
    if unknown and not replacement_authorized:
        raise TalkingHeadBlocked(
            "RESUBMIT_REQUIRES_RECONCILIATION",
            "同一调用清单存在结果未知的作业：先按原远端 ID 对账，再决定是否重做；"
            "直接重提可能产生重复费用",
            {"runs": [r["id"] for r in unknown]},
        )
    if not replacement_authorized:
        raise TalkingHeadBlocked(
            "RESUBMIT_REQUIRES_AUTHORIZATION",
            "同一调用清单此前失败过：重做属于新的付费尝试，需显式授权（仍可能计费）",
            {"runs": [r["id"] for r in prior]},
        )
    return {"ok": True, "prior": [r["id"] for r in prior], "replacement_authorized": True}


def settle_unknown(run_id: str, outcome: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    """对账结论落定。必须带证据，不允许无证据改判。"""
    run = get_run(run_id)
    if not run:
        raise TalkingHeadRejected("PROVIDER_RUN_NOT_FOUND", f"运行记录不存在：{run_id}")
    job_id = run.get("job_id")
    if job_id:
        jobs.resolve_unknown(job_id, outcome, evidence)
    return _update_run(run_id, state=outcome, reconciliation={"evidence": evidence,
                                                              "at": now_iso()})


# ---------- OmniHuman ----------

def submit_omnihuman(work_id: str, *, task: Optional[Dict[str, Any]] = None,
                     prompt: str = "", resolution: str = "1080p", transport=None,
                     dry_run: Optional[bool] = None,
                     replacement_authorized: bool = False) -> Dict[str, Any]:
    """OmniHuman：固定人物图 + 已确认最终 TTS + 可选表演描述 → 原片（单阶段）。"""
    work = st.get("works", work_id)
    if not work:
        raise TalkingHeadRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
    require = dh_routes.require_preflight(work_id, task=task, route=dh_routes.ROUTE_OMNIHUMAN,
                                          require_plate=False)
    audio = tts_audio.final_audio(work_id)
    audio_ms = int(audio.get("duration_ms") or 0)
    dh_routes.assert_omnihuman_audio_duration_allowed(audio_ms)
    segment = {"index": 0, "segment_id": "oh_seg_1", "start_ms": 0,
               "end_ms": audio_ms, "duration_ms": audio_ms,
               "global_offset_ms": 0, "over_limit": False,
               "text": "", "single_call": True}
    pv.assert_omnihuman_input(audio_duration_ms=audio_ms,
                              prompt=prompt, resolution=resolution)
    listing = current_call_list(work_id, route=dh_routes.ROUTE_OMNIHUMAN, prompt=prompt,
                               resolution=resolution)
    if listing["segment_count"] != 1:
        raise TalkingHeadRejected(
            "CALL_LIST_INCONSISTENT",
            "整轨调用清单与提交参数不一致：段数与金额必须来自同一份推导",
            {"listing": listing["segment_count"], "segments": 1},
        )
    digest = listing["digest"]
    zone = resubmit_guard(work_id, digest, replacement_authorized=replacement_authorized)
    est = dh_cost.estimate(dh_routes.ROUTE_OMNIHUMAN, duration_ms=audio_ms,
                           segment_count=1, resolution=resolution)
    authorization = _gate_with_ledger(
        work_id, route=dh_routes.ROUTE_OMNIHUMAN, provider="volcengine",
        model="omnihuman-1.5", capability=dh_routes.CAP_AVATAR_VIDEO_CLOUD,
        digest=digest, estimated_fen=est["estimated_fen"], task=task, call_list=listing,
        provider_params={"prompt": prompt, "resolution": resolution,
                         "segments": 1})

    retry_suffix = ""
    if replacement_authorized and zone.get("prior"):
        # 旧失败/未知尝试的费用确认不能授权新的付费尝试；重做须再次确认。
        confirmation_id = authorization["confirmation_id"]
        prior_confirmations = {(get_run(run_id) or {}).get("cost_reservation_id")
                               for run_id in zone["prior"]}
        if confirmation_id in prior_confirmations:
            raise TalkingHeadBlocked(
                "RESUBMIT_REQUIRES_AUTHORIZATION",
                "重做会产生新的付费调用：请先重新确认调用清单与金额，再明确提交重做；本次未提交。",
                {"confirmation_id": confirmation_id, "runs": zone["prior"]})
        # 同一次新费用确认只对应一个新尝试，双击仍复用，不覆盖旧失败台账。
        retry_suffix = f":retry:{confirmation_id}"

    is_mock = config.resolve_mode() == "mock" if dry_run is None else bool(dry_run)
    if is_mock:
        run = start_run(work_id, route=dh_routes.ROUTE_OMNIHUMAN, provider="volcengine",
                        model="omnihuman-1.5", capability=dh_routes.CAP_AVATAR_VIDEO_CLOUD,
                        request_digest=digest, idempotency_key=f"oh:{work_id}:{digest}",
                        task_id=(task or {}).get("id"), is_mock=True,
                        provider_params={"prompt": prompt, "resolution": resolution,
                                         "segments": 1})
        return {"submitted": False, "is_mock": True, "route": dh_routes.ROUTE_OMNIHUMAN,
                "runs": [run], "segments": [segment], "estimate": est,
                "authorization": authorization, "resubmit": zone,
                "note": "dry_run：未发起真实调用、未登记原片；占位产物不得进入审核与后期"}

    if not transport:
        raise TalkingHeadBlocked("OMNIHUMAN_TRANSPORT_UNAVAILABLE", "未提供网络通道，不假装已提交")
    ak, sk = _credentials()
    from .omni_media import prepare_media

    source = require["source"]
    media = prepare_media(source, audio, ak=ak, sk=sk)
    resolved_snapshot = dict(_input_snapshot(work, audio), source_asset_id=source["asset_id"],
                             source_asset_hash=source["asset_hash"])

    media_args = {"image_url": media["image_url"], "audio_url": media["audio_url"],
                  "ak": ak, "sk": sk}
    run = _submit_one_segment(
        work_id, segment, digest=digest, prompt=prompt, resolution=resolution,
        confirmation_id=authorization["confirmation_id"], retry_suffix=retry_suffix,
        per_seg_fen=max(1, int(est["estimated_fen"])), task=task,
        inputs_snapshot=resolved_snapshot, media=media_args,
        segment_count=1, transport=transport)
    runs = [run]
    return {"submitted": run["state"] == "waiting_provider",
            "is_mock": False, "route": dh_routes.ROUTE_OMNIHUMAN, "runs": runs,
            "segments": [segment], "estimate": est, "authorization": authorization,
            "max_concurrency": 1, "pending_segments": 0,
            "driver_started": False,
            "note": "已受理不等于成功：按原 task_id 查询，下载后仍须通过媒体校验"}


def _segment_audio_url(seg: Dict[str, Any], media: Dict[str, Any]) -> str:
    """取该段自己的音频 URL。

    整轨恰好就是这一段时（音频未超限的单次调用）用整轨；否则必须有切片。
    **缺切片时阻断，绝不用整轨兜底**：整轨超 60 秒时云端会"提交成功、
    查询 50215"，那种失败看起来像云端故障，实际是我们发错了音频。
    """
    url = (media.get("segment_audio_urls") or {}).get(seg.get("segment_id"))
    if url:
        return url
    if seg.get("single_call"):
        return media["audio_url"]
    raise TalkingHeadBlocked(
        "OMNIHUMAN_SEGMENT_AUDIO_MISSING",
        f"分段 {seg.get('segment_id')!r} 缺少自己的音频切片；"
        "整轨音频超过云端 60 秒上限会被拒绝（50215），不得用它兜底。",
        {"segment_id": seg.get("segment_id")})


def _segment_run_key(work_id: str, digest: str, segment: Dict[str, Any],
                     retry_suffix: str) -> str:
    """每段的幂等键：段标识进键，双击/刷新/推进器只会复用，不会重复提交收费请求。"""
    return f"oh:{work_id}:{digest}:{segment['segment_id']}{retry_suffix}"


def _submit_one_segment(work_id: str, seg: Dict[str, Any], *, digest: str, prompt: str,
                        resolution: str, confirmation_id: str, retry_suffix: str,
                        per_seg_fen: int, task: Optional[Dict[str, Any]],
                        inputs_snapshot: Dict[str, Any], media: Dict[str, Any],
                        segment_count: int, transport) -> Dict[str, Any]:
    """登记并提交**一段**；返回该段的 run。

    并发上限为 1 时调用方一次只调它一段——供应商只允许一个生成任务在跑，
    后一段必须等前一段腾出并发位。已有持久化意图（replayed）时直接复用，
    不再发起请求；失败/未知一律按原语义落账，调用方决定是否中断整条计划。

    **仅兼容历史分段台账**：当前 OmniHuman 长音频已在提交入口统一拒绝，
    此处仍保留整轨时长防线，避免旧推进器绕过门禁产生新的供应商调用。
    """
    full_audio = tts_audio.final_audio(work_id) or {}
    dh_routes.assert_omnihuman_audio_duration_allowed(
        int(full_audio.get("duration_ms") or 0))
    key = _segment_run_key(work_id, digest, seg, retry_suffix)
    audio_url = _segment_audio_url(seg, media)
    # 纯参数构造放在提交意图之前；输入非法时尚未产生供应商副作用。
    request = pv.build_submit_request(image_url=media["image_url"],
                                      audio_url=audio_url,
                                      prompt=prompt, output_resolution=resolution,
                                      ak=media["ak"], sk=media["sk"])
    run = start_run(work_id, route=dh_routes.ROUTE_OMNIHUMAN, provider="volcengine",
                    model="omnihuman-1.5", capability=dh_routes.CAP_AVATAR_VIDEO_CLOUD,
                    request_digest=digest, idempotency_key=key,
                    task_id=(task or {}).get("id"),
                    cost_reservation_id=confirmation_id,
                    provider_params={"prompt": prompt, "resolution": resolution,
                                     "segment_index": seg["index"],
                                     "segment_count": segment_count,
                                     "input_snapshot": inputs_snapshot,
                                     "global_offset_ms": seg.get("global_offset_ms", 0),
                                     # 推进器要按同样的幂等键续提交，因此后缀必须持久化，
                                     # 不能只活在本次请求的局部变量里。
                                     "retry_suffix": retry_suffix},
                    reuse_existing=True)
    if run.get("replayed"):
        # 同一段只有获得持久化意图的请求才可 POST；并发请求、刷新和双击均复用。
        return run
    job = jobs.submit(
        work_id=work_id, capability=dh_routes.CAP_AVATAR_VIDEO_CLOUD,
        provider="volcengine", model="omnihuman-1.5",
        args={"route": dh_routes.ROUTE_OMNIHUMAN, "segment": seg["segment_id"],
              "prompt": prompt, "resolution": resolution,
              "audio_asset_id": inputs_snapshot.get("audio_asset_id"),
              "audio_hash": inputs_snapshot.get("audio_hash", ""),
              "request_digest": digest},
        idempotency_key=key, task_id=(task or {}).get("id"),
        billable=True, amount_fen=per_seg_fen,
        note="OmniHuman 云端生成（不占本地 GPU，可与本地重型作业并行）",
    )
    if job["state"] == "blocked":
        record_failure(run["id"], code=job.get("block_code", "JOB_BLOCKED"),
                       message=job.get("block_detail", "作业被阻断"))
        raise TalkingHeadBlocked(job.get("block_code", "JOB_BLOCKED"),
                                job.get("block_detail", "作业被阻断"),
                                {"run_id": run["id"], "job_id": job["id"]})
    if job.get("replayed"):
        run = _update_run(run["id"], job_id=job["id"])
        mark_run_unknown(run["id"], "已有同一收费作业但缺少可复用运行记录，先对账，未重复提交")
        raise TalkingHeadBlocked(
            "RESUBMIT_REQUIRES_RECONCILIATION",
            "已有同一 OmniHuman 作业，先核对原调用结果；本次没有重复提交收费请求",
            {"run_id": run["id"], "job_id": job["id"]})
    jobs.acquire_lease(job["id"], f"submit:{run['id']}", ttl_seconds=120)
    run = _update_run(run["id"], job_id=job["id"], state="submitted")
    try:
        parsed = pv.parse_submit_response(transport(request))
    except (TalkingHeadBlocked, TalkingHeadRejected) as error:
        if error.code in ("OMNIHUMAN_AUTH_FAILED", "OMNIHUMAN_QUOTA_EXHAUSTED"):
            record_failure(run["id"], code=error.code, message=error.message)
            raise
        mark_run_unknown(run["id"], "供应商提交没有可核对的受理结论，可能已计费；先对账，不得盲目重提")
        raise TalkingHeadBlocked(
            "OMNIHUMAN_SUBMISSION_UNKNOWN",
            "口播提交结果未知，已保留调用记录；可能已受理和计费，请先对账，不要再次生成",
            {"run_id": run["id"], "job_id": job["id"]}) from error
    except Exception as error:
        # 不把异常文本写回页面：运输层异常可能带 URL 或签名头。
        mark_run_unknown(run["id"], "供应商提交连接中断或响应无法解析，可能已计费；先对账，不得盲目重提")
        raise TalkingHeadBlocked(
            "OMNIHUMAN_SUBMISSION_UNKNOWN",
            "口播提交结果未知，已保留调用记录；可能已受理和计费，请先对账，不要再次生成",
            {"run_id": run["id"], "job_id": job["id"]}) from error
    if parsed["state"] == "submitted":
        # 先保存远端 ID；即便交还本地租约失败，仍能按原 ID 对账。
        run = _update_run(run["id"], remote_job_id=parsed["remote_job_id"])
        # 云端生成在同步提交结束后继续，交还本地租约供原 task_id 查询回收。
        jobs.release_lease(job["id"], f"submit:{run['id']}",
                           reason="供应商已受理，等待原任务返回")
        return _update_run(run["id"], state="waiting_provider")
    if parsed["state"] == "unknown":
        return mark_run_unknown(run["id"], parsed.get("reason", "提交结果未知"))
    return record_failure(run["id"], code="OMNIHUMAN_SUBMIT_FAILED",
                          message=parsed.get("reason", "提交失败"))


def _variant_asset_id(work: Dict[str, Any]) -> str:
    variant = st.get("variants", work.get("variant_id") or "") or {}
    return variant.get("asset_id") or ""


def poll_run(run_id: str, *, transport=None) -> Dict[str, Any]:
    """按原 remote_job_id 查询。running 只是 running；done 还要能下载并通过校验。"""
    run = get_run(run_id)
    if not run:
        raise TalkingHeadRejected("PROVIDER_RUN_NOT_FOUND", f"运行记录不存在：{run_id}")
    if run.get("provider") == "dashscope_wan":
        from .wan import poll_wan_run
        return poll_wan_run(run_id, transport=transport)
    if run.get("provider") not in ("m2_worker", "volcengine"):
        raise TalkingHeadRejected("PROVIDER_UNSUPPORTED", "运行记录中的视频供应商不受支持，未发起查询")
    if run["state"] in ("succeeded", "failed", "cancelled"):
        return {"run": run, "state": run["state"], "note": "已是终态，不再查询"}
    if run.get("is_mock"):
        raise TalkingHeadBlocked("MOCK_RUN_NOT_POLLABLE",
                                "占位运行没有真实远端任务，不可查询")
    if not run.get("remote_job_id"):
        return {"run": mark_run_unknown(run_id, "缺 task_id：无法查询，先对账"),
                "state": "unknown", "note": "无远端 ID 不得改判"}
    if not transport:
        raise TalkingHeadBlocked("OMNIHUMAN_TRANSPORT_UNAVAILABLE", "未提供网络通道")
    if run.get("provider") == "m2_worker":
        return _poll_m2_run(run_id, transport=transport)
    ak, sk = _credentials()
    request = pv.build_query_request(task_id=run["remote_job_id"], ak=ak, sk=sk)
    parsed = pv.parse_query_response(transport(request))
    if parsed["state"] == "unavailable":
        # 通道抖动不是结论：不改状态、不改判，等下一轮再问。
        return {"run": run, "state": run["state"], "transport_unavailable": True,
                "note": f"查询暂不可用，保持现状：{parsed.get('reason', '')}"}
    if parsed["state"] == "running":
        return {"run": _update_run(run_id, state="waiting_provider",
                                   poll_after=now_iso()),
                "state": "waiting_provider", "remote_status": parsed.get("remote_status")}
    if parsed["state"] == "unknown":
        return {"run": mark_run_unknown(run_id, parsed.get("reason", "状态未知")),
                "state": "unknown"}
    if parsed["state"] == "failed":
        return {"run": record_failure(run_id, code="OMNIHUMAN_REMOTE_FAILED",
                                      message=parsed.get("reason", "远端失败")),
                "state": "failed"}

    downloaded = pv.download_and_verify(parsed["video_url"], transport)
    return _register_original(run_id, downloaded, note="OmniHuman 原片（下载并校验通过）")


def _poll_m2_run(run_id: str, *, transport=None) -> Dict[str, Any]:
    """按 worker 的 job_id 查询 M2 作业状态。

    查询本身只取真相、不推进作业：这里不会重提、不会重试、
    不会把没做完的作业改判成任何结论。重提是另一个显式动作，
    由 resubmit_guard 与幂等键约束，不能藏在轮询里偷偷发生。
    """
    from . import worker_client as dh_worker

    run = get_run(run_id)
    if not dh_worker.configured():
        raise TalkingHeadBlocked(
            "M2_TRANSPORT_UNAVAILABLE",
            "未配置 worker 地址：无法查询 M2 作业状态（这是通道未接通，不是作业失败）",
            {"env": "AVATAR_STUDIO_M2_WORKER_URL"})
    request = dh_worker.build_status_request(dh_worker.worker_url(),
                                             run["remote_job_id"],
                                             token=dh_worker.worker_token())
    parsed = pv.parse_m2_response(transport(request))
    if parsed["state"] == "unavailable":
        # 与云路线一致：没问到 ≠ 问到了"不知道"。
        return {"run": run, "state": run["state"], "transport_unavailable": True,
                "note": f"查询暂不可用，保持现状：{parsed.get('reason', '')}"}
    if parsed["state"] == "running":
        return {"run": _update_run(run_id, state="waiting_provider", poll_after=now_iso()),
                "state": "waiting_provider", "remote_status": parsed.get("remote_status")}
    if parsed["state"] == "unknown":
        return {"run": mark_run_unknown(run_id, parsed.get("reason", "状态未知")),
                "state": "unknown"}
    if parsed["state"] == "failed":
        return {"run": record_failure(run_id, code="M2_REMOTE_FAILED",
                                      message=parsed.get("reason", "远端失败")),
                "state": "failed"}

    downloaded = pv.download_and_verify(parsed["output_url"], transport)
    return _register_original(run_id, downloaded, note="MuseTalk 原片（下载并校验通过）")


def _register_original(run_id: str, downloaded: Dict[str, Any], *, note: str) -> Dict[str, Any]:
    # 当前部署为单 API 进程；同一远端结果的并发轮询也不能同时登记两条资产。
    with _ORIGINAL_REGISTRATION_LOCK:
        return _register_original_locked(run_id, downloaded, note=note)


def _complete_result_job(run: Dict[str, Any], video: Dict[str, Any]) -> None:
    job_id = run.get("job_id")
    job = jobs.get_job(job_id) if job_id else None
    if not job or job.get("state") in jobs.TERMINAL_STATES:
        return
    worker_id = f"recover:{run['id']}"
    jobs.acquire_lease(job_id, worker_id, ttl_seconds=30)
    jobs.complete(job_id, worker_id, {"video_id": video["id"], "file_hash": video["file_hash"],
                                     "remote_job_id": run.get("remote_job_id"),
                                     "source": "provider_result_download_and_media_probe"})


def _register_original_locked(run_id: str, downloaded: Dict[str, Any], *, note: str) -> Dict[str, Any]:
    run = get_run(run_id)
    probe = downloaded["probe"]
    params = run.get("provider_params") or {}
    work_id = run["work_id"]
    work = st.get("works", work_id) or {}
    inputs = _run_inputs(run)
    final = tts_audio.final_audio(work_id) or {}
    stale = (inputs.get("identity_id") != work.get("identity_id")
             or inputs.get("variant_id") != work.get("variant_id")
             or inputs.get("audio_asset_id") != final.get("id")
             or inputs.get("audio_hash") != final.get("file_hash")
             or (run.get("route") == dh_routes.ROUTE_M2 and
                 inputs.get("motion_ref_id") != (work.get("motion_intent") or {}).get("motion_ref_id")))
    existing = None
    for candidate in vd.list_videos(work_id, vd.KIND_ORIGINAL):
        owner = get_run(candidate.get("provider_run_id") or "") or {}
        if (candidate.get("file_hash") != probe.get("file_hash") or not run.get("remote_job_id")
                or owner.get("remote_job_id") != run.get("remote_job_id")
                or owner.get("provider") != run.get("provider") or owner.get("route") != run.get("route")
                or owner.get("request_digest") != run.get("request_digest")
                or owner.get("work_id") != work_id
                or candidate.get("identity_id") != inputs.get("identity_id")
                or candidate.get("variant_id") != inputs.get("variant_id")
                or candidate.get("audio_asset_id") != inputs.get("audio_asset_id")
                or (owner.get("provider_params") or {}).get("segment_index") != params.get("segment_index")):
            continue
        try:
            if _run_inputs(owner) != inputs:
                continue
        except TalkingHeadBlocked:
            continue
        existing = candidate
        break
    if existing:
        # 早期回收遗漏这三项来源关联。只在同任务与输入证据完全核对后补缺项，
        # 不覆盖已有值，也不改变审批、确认或输出文件哈希。
        repaired = False
        for field in ("motion_ref_id", "source_asset_id", "source_asset_hash"):
            if not existing.get(field) and inputs.get(field):
                existing[field] = inputs[field]
                repaired = True
        if repaired:
            st.put(vd.VIDEO_TABLE, existing["id"], existing, {"work_id": work_id})
        if stale and existing.get("approval_state") != "revoked":
            existing = vd.revoke(existing["id"], "旧任务返回时作品输入已变化，仅保留历史原片")
        _complete_result_job(run, existing)
        run = _update_run(run_id, state="succeeded", result_asset_ids=[existing["id"]])
        return {"run": run, "state": "succeeded", "video": existing, "probe": probe,
                "reused": True, "note": "同一远端输出已登记，复用现有原片，不重复写入视频资产"}
    video = vd.register_video(
        work_id=work_id, kind=vd.KIND_ORIGINAL, route=run.get("route") or "M2",
        provider=run.get("provider", ""), model=run.get("model", ""),
        duration_ms=int(probe.get("duration_ms") or 0), file_hash=probe["file_hash"],
        is_mock=False, identity_id=inputs.get("identity_id"),
        variant_id=inputs.get("variant_id"), audio_asset_id=inputs.get("audio_asset_id"),
        motion_ref_id=inputs.get("motion_ref_id"), source_asset_id=inputs.get("source_asset_id"),
        source_asset_hash=inputs.get("source_asset_hash"),
        provider_run_id=run_id, cost_reservation_id=run.get("cost_reservation_id"),
        has_video_track=bool(probe.get("has_video_track")),
        has_audio_track=bool(probe.get("has_audio_track")),
        width=probe.get("width"), height=probe.get("height"),
        fps=probe.get("fps"),
        segment_index=params.get("segment_index"),
        segment_count=params.get("segment_count"),
        global_offset_ms=int(params.get("global_offset_ms") or 0),
        checks=[], note=note,
    )
    video["storage_uri"] = vd.store_video_bytes(work_id, video["id"], downloaded["data"])
    st.put(vd.VIDEO_TABLE, video["id"], video, {"work_id": work_id})
    if stale:
        video = vd.revoke(video["id"], "旧任务返回时作品输入已变化，仅保留历史原片")
    _complete_result_job(run, video)
    run = _update_run(run_id, state="succeeded", result_asset_ids=[video["id"]])
    return {"run": run, "state": "succeeded", "video": video, "probe": probe}


# ---------- M2 ----------

def submit_m2(work_id: str, *, task: Optional[Dict[str, Any]] = None, transport=None,
              dry_run: Optional[bool] = None,
              replacement_authorized: bool = False) -> Dict[str, Any]:
    """M2：已审底片 + 同一条最终音频 → MuseTalk 口型（两阶段中的第二阶段）。"""
    work = st.get("works", work_id)
    if not work:
        raise TalkingHeadRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
    require = dh_routes.require_preflight(work_id, task=task, route=dh_routes.ROUTE_M2,
                                          require_plate=True)
    audio = tts_audio.final_audio(work_id)
    from . import plates as pl

    plate = pl.approved_plate_for(work_id)
    if not plate:
        raise TalkingHeadBlocked("PLATE_NOT_APPROVED", "M2 需要已审动态底片才能对口型")
    digest = call_list_digest(dh_routes.ROUTE_M2, audio_id=audio["id"],
                              audio_hash=audio.get("file_hash", ""),
                              variant_id=work.get("variant_id"),
                              motion_ref_id=plate.get("motion_ref_id"), prompt="",
                              resolution="native", segment_count=1, plate_id=plate["id"])
    resubmit_guard(work_id, digest, replacement_authorized=replacement_authorized)
    est = dh_cost.estimate(dh_routes.ROUTE_M2, duration_ms=int(audio.get("duration_ms") or 0))
    authorization = _gate_with_ledger(
        work_id, route=dh_routes.ROUTE_M2, provider="m2_worker", model="MuseTalk-1.5",
        capability=dh_routes.CAP_LIPSYNC, digest=digest, estimated_fen=0, task=task,
        provider_params={"plate_id": plate["id"]})

    is_mock = config.resolve_mode() == "mock" if dry_run is None else bool(dry_run)
    if is_mock:
        run = start_run(work_id, route=dh_routes.ROUTE_M2, provider="m2_worker",
                        model="MuseTalk-1.5", capability=dh_routes.CAP_LIPSYNC,
                        request_digest=digest, idempotency_key=f"mt:{work_id}:{digest}",
                        task_id=(task or {}).get("id"), is_mock=True,
                        provider_params={"plate_id": plate["id"]})
        return {"submitted": False, "is_mock": True, "route": dh_routes.ROUTE_M2,
                "runs": [run], "estimate": est, "authorization": authorization,
                "plate_id": plate["id"], "preflight": require,
                "note": "dry_run：未提交 GPU 作业、未登记原片"}

    if not transport:
        raise TalkingHeadBlocked("M2_TRANSPORT_UNAVAILABLE", "未提供算力云通道，不假装已提交")
    request = pv.build_musetalk_request(
        plate_url=plate.get("storage_uri", ""), audio_url=audio.get("storage_uri", ""),
        plate_hash=plate.get("file_hash", ""), audio_hash=audio.get("file_hash", ""))
    run = start_run(work_id, route=dh_routes.ROUTE_M2, provider="m2_worker",
                    model="MuseTalk-1.5", capability=dh_routes.CAP_LIPSYNC,
                    request_digest=digest, idempotency_key=f"mt:{work_id}:{digest}",
                    task_id=(task or {}).get("id"),
                    cost_reservation_id=authorization["confirmation_id"],
                    provider_params={"plate_id": plate["id"], "input_snapshot": _input_snapshot(work, audio, plate)},
                    reuse_existing=True)
    if run.get("replayed"):
        return {"submitted": run["state"] in ("submitted", "waiting_provider"), "is_mock": False,
                "route": dh_routes.ROUTE_M2, "runs": [run], "estimate": est,
                "authorization": authorization, "plate_id": plate["id"], "replayed": True,
                "note": "复用已提交的口播任务；查询原任务状态，不重复提交 GPU 作业"}
    job = jobs.submit(work_id=work_id, capability=dh_routes.CAP_LIPSYNC, provider="m2_worker",
                      model="MuseTalk-1.5", args=request["args"],
                      idempotency_key=f"mt:{work_id}:{digest}",
                      task_id=(task or {}).get("id"), billable=True, amount_fen=0,
                      note="MuseTalk 口型（单 GPU 重型作业，排队互斥）")
    if job["state"] == "blocked":
        record_failure(run["id"], code=job.get("block_code", "JOB_BLOCKED"),
                       message=job.get("block_detail", "作业被阻断"))
        raise TalkingHeadBlocked(job.get("block_code", "JOB_BLOCKED"),
                                job.get("block_detail", "作业被阻断"),
                                {"run_id": run["id"], "job_id": job["id"]})
    if job.get("replayed"):
        run = _update_run(run["id"], job_id=job["id"])
        mark_run_unknown(run["id"], "已有同一 GPU 作业但无可复用运行结果，必须对账，未重复提交")
        raise TalkingHeadBlocked("RESUBMIT_REQUIRES_RECONCILIATION", "已有同一 GPU 作业，先查询既有结果，未重复提交",
                                 {"run_id": run["id"], "job_id": job["id"]})
    run = _update_run(run["id"], job_id=job["id"], state="submitted")
    try:
        parsed = pv.parse_m2_response(transport(request))
    except Exception:
        mark_run_unknown(run["id"], "口播提交未取得可核对响应，先查询既有任务")
        raise
    if parsed["state"] == "succeeded":
        run = _update_run(run["id"], state="waiting_provider",
                          remote_job_id=parsed.get("remote_job_id"))
    elif parsed["state"] == "unknown":
        run = mark_run_unknown(run["id"], parsed.get("reason", "结果未知"))
    elif parsed["state"] == "running":
        # 排队/推理中不是失败：它是"还没完"。把它记成 failed 会让用户
        # 以为还要重提一次，而重提会在 GPU 上再排一份队（还绕过幂等键）。
        run = _update_run(run["id"], state="waiting_provider",
                          remote_job_id=parsed.get("remote_job_id"),
                          poll_after=now_iso())
    else:
        run = record_failure(run["id"], code="M2_JOB_FAILED",
                             message=parsed.get("reason", "作业未完成"))
    return {"submitted": run["state"] == "waiting_provider", "is_mock": False,
            "route": dh_routes.ROUTE_M2, "runs": [run], "estimate": est,
            "authorization": authorization, "plate_id": plate["id"],
            "note": "M2 两阶段各有独立 ProviderRun；失败保留，不自动回退其他路线"}


def submission_status(work_id: str) -> Dict[str, Any]:
    runs = runs_for_work(work_id)
    return {
        "work_id": work_id,
        "runs": [{"id": r["id"], "work_id": r.get("work_id"), "task_id": r.get("task_id"),
                  "provider": r.get("provider"), "model": r.get("model"), "created_at": r.get("created_at"),
                  "route": r.get("route"), "capability": r.get("capability"),
                  "state": r["state"], "remote_job_id": r.get("remote_job_id"),
                  "is_mock": r.get("is_mock"), "result_asset_ids": r.get("result_asset_ids"),
                  "request_digest": r.get("request_digest")} for r in runs],
        "auto_fallback": False,
        "note": "失败与未知一律保留原记录；跨路线回退需要用户重新选择，不自动发生",
    }
