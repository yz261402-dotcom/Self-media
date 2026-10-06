"""OmniHuman 分段串行推进器。

为什么需要它
------------
OmniHuman 的**并发上限由账号决定**（免费试用实测为 1：一次只能有一个生成任务在跑）。
把整条音轨的所有分段一口气提交出去，第 2 段必然撞 429；而 429 在
`providers._classify_http` 里被判成"额度不足、不得重试"，于是整个分段计划在第 2 段
就被整体打断，后面几段连作业都建不出来——2026-09-28 真实踩到过。

所以：**同时最多只让 N 段在飞**（N = 账号并发上限，见 `routes.omnihuman_max_concurrency`）。
超出的段不预先提交，由本模块按"前一段到达终态 → 提交下一段"的顺序推进。

沿用本工程既有约定
------------------
- **排队不是失败。** 还没轮到提交的段不写任何状态，更不写 failed。
- **未知不得改判。** 任一段进入 `unknown` 就停住等对账（可能已计费），不自动重提。
- **失败不自动重试。** 任一段 `failed` 就停住；重做要重新确认费用，不能藏在这里发生。
- **查询只取真相。** 轮询只读远端状态；提交是独立的、有并发位才发生的动作。

进程重启不会丢：所有状态都在台账里，重启后重新 `ensure()` 即可从台账续推。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

from .. import storage as st
from . import cost as dh_cost
from . import routes as dh_routes
from . import segments as sg
from . import submission as sub
from .errors import TalkingHeadBlocked, TalkingHeadRejected

# 在飞状态：这些段占着并发位，必须先问清楚才能发下一段。
IN_FLIGHT = ("intent", "submitted", "waiting_provider", "leased")
# 终态：不再变化。succeeded 才算产出。
TERMINAL = ("succeeded", "failed", "cancelled", "unknown")

POLL_INTERVAL_ENV = "AVATAR_STUDIO_OMNIHUMAN_POLL_SEC"
SETTLE_ENV = "AVATAR_STUDIO_OMNIHUMAN_SETTLE_SEC"

_drivers: Dict[str, threading.Thread] = {}
_lock = threading.Lock()


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def poll_interval_sec() -> float:
    return _env_float(POLL_INTERVAL_ENV, 15.0)


def settle_sec() -> float:
    """前一段到终态后、提交下一段前的静置时间。

    远端把任务标成 done 之后，并发位不一定立刻释放；立刻发下一段仍可能吃 429。
    这个等待不是"重试"，是给供应商留出释放并发位的时间。
    """
    return _env_float(SETTLE_ENV, 15.0)


def _omni_runs(work_id: str) -> List[Dict[str, Any]]:
    rows = [r for r in st.all_rows(sub.RUN_TABLE)
            if r.get("work_id") == work_id
            and r.get("route") == dh_routes.ROUTE_OMNIHUMAN
            and not r.get("is_mock")]
    return sorted(rows, key=lambda r: ((r.get("provider_params") or {}).get("segment_index") or 0,
                                       r.get("created_at", "")))


def _attempt_key(run: Dict[str, Any]) -> str:
    """一次"尝试"的身份：摘要 + 重试后缀。

    只按摘要分组是错的：重做会复用同一个摘要（输入没变），但换一组幂等键
    （`:retry:<新确认书>`）。若把两次尝试并成一组，上一轮遗留的 `unknown` 段
    会立刻把新尝试判成"需对账"而停住——新尝试是用户显式授权的，不该被它拦住。
    """
    suffix = (run.get("provider_params") or {}).get("retry_suffix") or ""
    return f"{run.get('request_digest')}#{suffix}"


def _active_group(work_id: str) -> Optional[Dict[str, Any]]:
    """取该作品**最近一次尝试**的那组分段运行。

    按尝试分组而不是"取全部"：改稿/换音频会产生新摘要，重做会产生新后缀；
    混在一起会把别的轮次的段算进本轮的并发位，也会让旧轮次的失败状态污染本轮判断。
    """
    runs = _omni_runs(work_id)
    if not runs:
        return None
    latest = max(runs, key=lambda r: r.get("created_at", ""))
    attempt = _attempt_key(latest)
    group = [r for r in runs if _attempt_key(r) == attempt]
    return {"digest": latest.get("request_digest"), "attempt": attempt,
            "runs": group, "anchor": latest}


def _plan_segments(work_id: str) -> List[Dict[str, Any]]:
    plan = sg.latest_plan(work_id)
    return list((plan or {}).get("segments") or [])


def advance_once(work_id: str, *, transport=None) -> Dict[str, Any]:
    """推进一步。返回本次做了什么、以及为什么停/继续。

    state 取值：
    - `IDLE`       没有 OmniHuman 运行记录
    - `WAITING`    并发位已满，只查询，等下一轮
    - `ADVANCED`   提交了下一段
    - `DONE`       所有分段都 succeeded
    - `STOPPED`    有段 failed / unknown，停住等人工对账
    """
    group = _active_group(work_id)
    if group is None:
        return {"work_id": work_id, "state": "IDLE", "note": "没有 OmniHuman 运行记录"}
    if not transport:
        raise TalkingHeadBlocked("OMNIHUMAN_TRANSPORT_UNAVAILABLE", "未提供网络通道")

    # 1) 先把在飞的段问清楚。查询只读状态，不产生费用，也不改判未知。
    for run in group["runs"]:
        if run.get("state") in IN_FLIGHT and run.get("remote_job_id"):
            try:
                sub.poll_run(run["id"], transport=transport)
            except (TalkingHeadBlocked, TalkingHeadRejected) as error:
                # 查询被拒（如鉴权/限流）：保持现状等下一轮，绝不据此改判成功或失败。
                return {"work_id": work_id, "state": "WAITING",
                        "note": f"查询暂不可用，保持现状：{error.code}"}

    runs = _active_group(work_id)["runs"]
    by_index = {int((r.get("provider_params") or {}).get("segment_index") or 0): r for r in runs}
    segments = _plan_segments(work_id)
    total = (len(segments)
             or int((group["anchor"].get("provider_params") or {}).get("segment_count") or 0)
             or 1)

    unknown = [r for r in runs if r.get("state") == "unknown"]
    if unknown:
        return {"work_id": work_id, "state": "STOPPED", "stop_reason": "UNKNOWN_REQUIRES_RECONCILIATION",
                "runs": runs, "note": "有分段提交结果未知，可能已计费：先对账，不自动重提"}
    failed = [r for r in runs if r.get("state") == "failed"]
    if failed:
        return {"work_id": work_id, "state": "STOPPED", "stop_reason": "SEGMENT_FAILED",
                "runs": runs, "note": "有分段失败：重做需重新确认费用，不在此处自动重试"}

    if total and all(by_index.get(i, {}).get("state") == "succeeded" for i in range(total)):
        return {"work_id": work_id, "state": "DONE", "runs": runs,
                "note": "历史已提交的 OmniHuman 作业全部回收完成"}

    pending = [i for i in range(total) if i not in by_index]
    if pending:
        return {"work_id": work_id, "state": "STOPPED",
                "stop_reason": "SEGMENT_SUBMISSION_RETIRED", "submitted": [],
                "remaining": len(pending), "runs": runs,
                "note": "OmniHuman 分段提交已退休；仅查询已提交作业，不再续发新分段"}

    in_flight = [r for r in runs if r.get("state") in IN_FLIGHT]
    if in_flight:
        return {"work_id": work_id, "state": "WAITING", "runs": runs,
                "in_flight": len(in_flight), "submitted": [],
                "note": "仅查询已提交的 OmniHuman 作业，不提交新分段"}
    return {"work_id": work_id, "state": "STOPPED",
            "stop_reason": "SEGMENT_SUBMISSION_RETIRED", "submitted": [],
            "runs": runs, "note": "分段提交已退休；仅保留历史作业查询"}


def progress(work_id: str) -> Dict[str, Any]:
    """分段进度快照（只读）。

    界面要靠它回答"还差几段、现在能不能继续"，所以这里**只报告事实**：
    不启动线程、不改状态、不补默认值。`state` 取值：

    - `IDLE`    没有 OmniHuman 运行记录
    - `PENDING` 还有段没发，且没有阻塞原因 —— 可以续发
    - `DONE`    全部段已 succeeded
    - `STOPPED` 有段 failed / unknown —— 先对账或重新授权，续发不会自动越过
    """
    group = _active_group(work_id)
    if group is None:
        return {"work_id": work_id, "state": "IDLE", "total": 0, "succeeded": 0,
                "in_flight": 0, "pending": 0, "failed": 0, "unknown": 0,
                "resumable": False, "stop_reason": None, "attempt": None,
                "note": "没有 OmniHuman 运行记录：先提交一次生成，才有分段可续发"}
    runs = group["runs"]
    anchor_params = group["anchor"].get("provider_params") or {}
    total = len(_plan_segments(work_id)) or int(anchor_params.get("segment_count") or 0)
    by_index = {int((r.get("provider_params") or {}).get("segment_index") or 0): r for r in runs}
    succeeded = [i for i in range(total) if by_index.get(i, {}).get("state") == "succeeded"]
    in_flight = [r["id"] for r in runs if r.get("state") in IN_FLIGHT]
    failed = [r["id"] for r in runs if r.get("state") == "failed"]
    unknown = [r["id"] for r in runs if r.get("state") == "unknown"]
    pending = [i for i in range(total) if i not in by_index]

    stop_reason = ("UNKNOWN_REQUIRES_RECONCILIATION" if unknown
                   else "SEGMENT_FAILED" if failed else None)
    if total and len(succeeded) == total:
        state, note = "DONE", "历史已提交的 OmniHuman 作业全部回收完成"
    elif stop_reason:
        state, note = "STOPPED", ("有分段提交结果未知，可能已计费：先对账"
                                  if unknown else "有分段失败：重做需重新确认费用")
    elif pending or in_flight:
        state, stop_reason, note = (
            "STOPPED", "SEGMENT_SUBMISSION_RETIRED",
            "OmniHuman 分段提交已退休；仅查询已提交作业，不再续发新分段")
    else:
        state, stop_reason, note = (
            "STOPPED", "SEGMENT_SUBMISSION_RETIRED",
            "分段提交已退休；仅保留历史作业查询")
    return {"work_id": work_id, "state": state, "attempt": group["attempt"],
            "total": total, "succeeded": len(succeeded), "in_flight": len(in_flight),
            "pending": len(pending), "failed": len(failed), "unknown": len(unknown),
            "resumable": False,
            "stop_reason": stop_reason,
            "blocked_runs": unknown + failed, "note": note}


def resume(work_id: str) -> Dict[str, Any]:
    """确保推进器在跑，并返回进度快照。

    与"重提"的区别是本质的：**重提会产生新的付费调用**（必须重新确认费用），
    续发只是把**已经授权过的那次调用清单**里还没发出的段按序发出去，
    因此沿用原提交的 `cost_reservation_id`，不需要任何新的费用确认。

    阻塞时不硬推：有段 unknown/failed 就先返回 `STOPPED`，让人去对账或重新授权，
    而不是在这里偷偷越过（这正是 `_loop` 的既有语义）。
    """
    raise TalkingHeadRejected(
        "OMNIHUMAN_SEGMENT_SUBMISSION_RETIRED",
        "OmniHuman 分段提交接口已退休；仅保留已提交作业查询，不能续发新分段",
    )


def _loop(work_id: str) -> None:
    from . import providers as pv

    while True:
        try:
            outcome = advance_once(work_id, transport=pv.urllib_transport)
        except (TalkingHeadBlocked, TalkingHeadRejected):
            # 阻断（含 429/鉴权/未配置）：留给用户与对账处理，不在这里重试。
            return
        except Exception:  # noqa: BLE001 - 后台线程不能把异常抛到无处可去的地方
            return
        if outcome.get("state") in ("DONE", "STOPPED", "IDLE"):
            return
        # 刚提交完一段，先静置再查，避免把供应商还没释放的并发位撞成 429。
        time.sleep(settle_sec() if outcome.get("state") == "ADVANCED" else poll_interval_sec())


def ensure(work_id: str) -> bool:
    """确保该作品有一个推进线程在跑。幂等：已有活线程就不重复起。"""
    with _lock:
        existing = _drivers.get(work_id)
        if existing is not None and existing.is_alive():
            return False
        thread = threading.Thread(target=_loop, args=(work_id,),
                                  name=f"omni-driver-{work_id}", daemon=True)
        _drivers[work_id] = thread
        thread.start()
        return True


def recover_pending() -> List[str]:
    """重启后把仍有分段在飞的作品重新挂上推进器。

    **恢复不是重放**：状态全在台账里，这里只是重新起一个线程，让它按当前真相
    （哪几段在飞、哪几段还没提交）继续推进；不会重发任何已提交的段。
    """
    works = sorted({r.get("work_id") for r in st.all_rows(sub.RUN_TABLE)
                    if r.get("route") == dh_routes.ROUTE_OMNIHUMAN
                    and not r.get("is_mock") and r.get("state") in IN_FLIGHT
                    and r.get("work_id")})
    return [work_id for work_id in works if ensure(work_id)]
