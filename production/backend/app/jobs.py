"""Provider 作业台账、租约与重型资源互斥（T02）。

对应 05 实施计划 T02：
- 先测租约竞争、过期 Worker 回写、并发预留、提交丢响应、检查点与业务账本不一致、取消后仍需结算。
- 持久意图、幂等键和预算先于副作用：意图先落库，预算不通过即 blocked，绝不产生外部副作用。
- 结果 unknown 先对账，不能创建替代计费任务。
- 单 GPU 重型作业互斥；过期作业未核实结束前不释放资源给冲突任务。

事实来源是本模块的 provider_jobs 表；Agent 的检查点不能代替账本。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import storage as st

JobState = str  # intent | blocked | queued | leased | succeeded | failed | unknown | cancelled

TERMINAL_STATES = ("succeeded", "failed", "cancelled")
# 需要重型资源（单 GPU 互斥）的能力
HEAVY_CAPABILITIES = {"motion", "lipsync", "avatar_video", "render"}
GPU_LOCK_ID = "gpu:0"


class JobRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(), timezone.utc).isoformat()


def args_digest(args: Any) -> str:
    raw = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def get_job(job_id: str) -> Optional[dict]:
    return st.get("provider_jobs", job_id)


def _load(c, job_id: str) -> Optional[dict]:
    row = c.execute("SELECT data FROM provider_jobs WHERE id=?", [job_id]).fetchone()
    return json.loads(row["data"]) if row else None


def _idem_lookup(c, key: str) -> Optional[dict]:
    row = c.execute("SELECT job_id FROM provider_jobs_idem WHERE idem_key=?", [key]).fetchone()
    if not row:
        return None
    return _load(c, row["job_id"])


# 底片幂等键的世代后缀：只有"已对账取消且无产物"的死尝试才会让出键位。
_PLATE_RETRY_TAG = "#retry:"


def _plate_attempt_no(key: str, base_key: str) -> Optional[int]:
    if key == base_key:
        return 0
    if key.startswith(base_key + _PLATE_RETRY_TAG):
        tail = key[len(base_key) + len(_PLATE_RETRY_TAG):]
        return int(tail) if tail.isdigit() else None
    return None


def _plate_retry_allowed(job: Optional[dict]) -> bool:
    """已对账取消的作业不构成待处理的 GPU 作业。

    依据 resolve_unknown 的语义：outcome=cancelled 时 settlement_required 被置为 False，
    即这次尝试无产物、无计费，且已核实未在远端产生结果。若让这类死尝试永久占用幂等键，
    一次网络抖动就会把这组（作品/图/动作/音频）彻底锁死，且没有任何输入可以改动来解锁。
    """
    return bool(job) and job.get("state") == "cancelled" \
        and not job.get("result") and not job.get("settlement_required")


def plate_idempotency_key(base_key: str) -> str:
    """底片作业的幂等键：仅当最新一次尝试"已对账取消且无产物"时才顺延世代。

    M2 底片免费但占单 GPU，幂等键的首要职责是阻止同输入重复排队。因此
    queued/leased/unknown/succeeded/failed 一律复用最新键位——由 create_plate
    的既有分支决定是复用产物还是拒绝重提。只有对账结论为 cancelled 的死尝试
    才让出键位，使后继尝试能重新排队；由于键位随世代单调递增，
    "同一死世代内重复点击"仍会命中同一个键，不会多排一份 GPU 队列。
    """
    c = st.conn()
    rows = c.execute(
        "SELECT idem_key FROM provider_jobs_idem WHERE idem_key=? OR starts_with(idem_key, ?)",
        [base_key, base_key + _PLATE_RETRY_TAG]).fetchall()
    attempts = []
    for row in rows:
        no = _plate_attempt_no(row["idem_key"], base_key)
        if no is not None:
            attempts.append((no, row["idem_key"]))
    if not attempts:
        return base_key
    attempts.sort()
    no, key = attempts[-1]
    if _plate_retry_allowed(_idem_lookup(c, key)):
        return f"{base_key}{_PLATE_RETRY_TAG}{no + 1}"
    return key


def _reject_deleted_work(work_id: str) -> None:
    work = st.get("works", work_id)
    if work and work.get("deleted_at"):
        raise JobRejected("WORK_DELETED", "作品已删除，不能创建外部调用")


def submit(*, work_id: str, capability: str, provider: str, model: str, args: Dict[str, Any],
           idempotency_key: str, task_id: Optional[str] = None, billable: bool = False,
           amount_fen: int = 0, namespace: str = "", note: str = "") -> Dict[str, Any]:
    """持久化提交意图。

    顺序不可颠倒：幂等查重 → 意图落库 → 预算校验 → 资源校验收敛。
    预算不通过时作业停在 blocked，不产生任何外部副作用。
    """
    if not idempotency_key:
        raise JobRejected("VALIDATION_FAILED", "提交外部作业必须提供幂等键")
    digest = args_digest(args)
    with st.transaction() as c:
        _reject_deleted_work(work_id)
        prior = _idem_lookup(c, idempotency_key)
        if prior is not None:
            return dict(prior, replayed=True)  # 重放：不重复产生副作用

        job: Dict[str, Any] = {
            "id": st.new_id("job"),
            "work_id": work_id,
            "task_id": task_id,
            "capability": capability,
            "provider": provider,
            "model": model,
            "args_digest": digest,
            "idempotency_key": idempotency_key,
            "namespace": namespace,
            "billable": bool(billable),
            "amount_fen": int(amount_fen),
            "heavy": capability in HEAVY_CAPABILITIES,
            "state": "intent",
            "settlement_required": False,
            "stale_writers": [],
            "attempts": 0,
            "note": note,
            "created_at": _iso(),
            "updated_at": _iso(),
        }
        st.insert_row(c, "provider_jobs", job["id"], job, {"work_id": work_id})
        c.execute("INSERT INTO provider_jobs_idem (idem_key,job_id) VALUES (?,?) "
                  "ON CONFLICT (idem_key) DO UPDATE SET job_id=EXCLUDED.job_id",
                  [idempotency_key, job["id"]])

        if billable:
            from .config import budget_policy

            policy = budget_policy()
            # 所有可计费供应商共享并发跨作品预算预留；与幂等登记处于同一写事务。
            # 已失败且费用未明确结清的调用也保留预留，避免未知结果释放额度后再付费。
            day = job["created_at"][:10]
            reserved = []
            for row in c.execute("SELECT data FROM provider_jobs WHERE id<>?", [job["id"]]).fetchall():
                item = json.loads(row["data"])
                if (not item.get("billable") or str(item.get("created_at", ""))[:10] != day
                        or item.get("state") == "blocked"
                        or (item.get("state") == "cancelled" and not item.get("settlement_required"))):
                    continue
                reserved.append(item)
            policy.spent_today_fen = max(policy.spent_today_fen,
                sum(max(0, int(item.get("amount_fen") or 0)) for item in reserved))
            policy.submissions_today = max(policy.submissions_today, len(reserved))
            job["budget_snapshot"] = policy.to_dict()
            unlimited_storyboard = (namespace == "wan-storyboard"
                                    and os.environ.get("AVATAR_STUDIO_WAN_STORYBOARD_BUDGET_UNLIMITED", "").strip() == "1")
            if unlimited_storyboard:
                job["budget_snapshot"]["unlimited_namespace"] = namespace
            elif not policy.allows_billable(int(amount_fen)):
                job["state"] = "blocked"
                job["block_code"] = "BUDGET_NOT_AUTHORIZED"
                job["block_detail"] = f"可计费作业 {capability} 超出已授权预算或当日预留额度，未提交"
                job["updated_at"] = _iso()
                st.insert_row(c, "provider_jobs", job["id"], job, {"work_id": work_id})
                return dict(job, replayed=False)

        job["state"] = "queued"
        job["updated_at"] = _iso()
        st.insert_row(c, "provider_jobs", job["id"], job, {"work_id": work_id})
        return dict(job, replayed=False)


def lease_ttl_seconds() -> int:
    return int(os.environ.get("AVATAR_STUDIO_LEASE_TTL", "30"))


def acquire_lease(job_id: str, worker_id: str, ttl_seconds: Optional[int] = None, *,
                  require_queued: bool = False) -> Dict[str, Any]:
    """抢占租约。同一作业同一时刻只允许一个有效持有者。"""
    ttl = ttl_seconds or lease_ttl_seconds()
    now = _now()
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        if require_queued and job["state"] != "queued":
            raise JobRejected("JOB_NOT_QUEUED", "生成请求已取得发送租约或已结束，不能重复发送")
        if job["state"] in TERMINAL_STATES:
            raise JobRejected("JOB_TERMINAL", f"作业已处于终态 {job['state']}，不可再租")
        if job["state"] == "blocked":
            raise JobRejected("JOB_BLOCKED",
                              f"作业被 {job.get('block_code')} 阻断，未取得授权前不得租出执行")
        if job.get("namespace") == "content-v2":
            # content-v2 调用只能经 mark_sending 从 queued 发出；unknown/等待远端/发送中一律不得再租
            raise JobRejected("NOT_SENDABLE", "content-v2 外部调用只能通过 mark_sending 发送，禁止重新租出")
        lease = job.get("lease")
        if lease and lease.get("expires_at", 0) > now:
            if lease.get("worker_id") != worker_id:
                raise JobRejected("LEASE_HELD",
                                  f"作业已被 Worker {lease.get('worker_id')} 持有至 {lease.get('expires_at')}")
        elif lease and lease.get("expires_at", 0) <= now:
            # 租约过期：可抢占，但原持有者的后续回写一律判为过期写入
            job.setdefault("stale_writers", []).append(lease.get("worker_id"))

        job["lease"] = {"worker_id": worker_id, "acquired_at": now, "expires_at": now + ttl,
                        "acquired_iso": _iso(now), "expires_iso": _iso(now + ttl)}
        job["state"] = "leased"
        job["attempts"] = int(job.get("attempts", 0)) + 1
        job["updated_at"] = _iso()
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return dict(job["lease"])


def heartbeat(job_id: str, worker_id: str, ttl_seconds: Optional[int] = None) -> Dict[str, Any]:
    ttl = ttl_seconds or lease_ttl_seconds()
    now = _now()
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        lease = job.get("lease")
        if not lease or lease.get("worker_id") != worker_id:
            raise JobRejected("LEASE_NOT_HELD", "心跳方不是当前租约持有者")
        if lease.get("expires_at", 0) <= now:
            raise JobRejected("LEASE_EXPIRED", "租约已过期，需重新获取后再续期")
        lease["expires_at"] = now + ttl
        lease["expires_iso"] = _iso(now + ttl)
        job["updated_at"] = _iso()
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return dict(lease)


def release_lease(job_id: str, worker_id: str, *, next_state: str = "queued",
                  reason: str = "") -> Dict[str, Any]:
    """交还租约但不改终态（T05）。

    典型场景：Worker 拿到了租约，但 GPU 被别的作业占着。此时正确结果是
    "仍在排队"，不是"失败"——把资源竞争报成失败会让用户看到假的失败率，
    也会让人误以为任务需要重提。
    """
    if next_state in TERMINAL_STATES:
        raise JobRejected("VALIDATION_FAILED", "交还租约不得直接把作业置为终态")
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        if job["state"] in TERMINAL_STATES:
            raise JobRejected("JOB_TERMINAL", f"作业已处于终态 {job['state']}，无法交还租约")
        lease = job.get("lease")
        if not lease or lease.get("worker_id") != worker_id:
            raise JobRejected("LEASE_NOT_HELD",
                              f"交还方 {worker_id} 不是当前租约持有者，拒绝交还")
        job["lease"] = None
        job["state"] = next_state
        job["release_reason"] = reason
        job["updated_at"] = _iso()
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return job


def _check_writer(job: dict, worker_id: str) -> None:
    """过期 Worker 的回写必须被拒：不能让迟到的旧持有者覆盖新持有者的结果。"""
    now = _now()
    lease = job.get("lease")
    if not lease:
        raise JobRejected("LEASE_NOT_HELD", "作业当前没有租约持有者，回写被拒")
    if lease.get("worker_id") != worker_id:
        raise JobRejected("STALE_WRITER",
                          f"回写方 {worker_id} 不是当前持有者 {lease.get('worker_id')}，判为过期写入")
    if lease.get("expires_at", 0) <= now:
        raise JobRejected("LEASE_EXPIRED", "租约已过期，回写被拒；需重新获取租约并对账后再写")


def complete(job_id: str, worker_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        _check_writer(job, worker_id)
        job["state"] = "succeeded"
        job["result"] = result
        job["result_digest"] = args_digest(result)
        job["lease"] = None
        job["settlement_required"] = bool(job.get("billable"))
        job["updated_at"] = _iso()
        # 先落定状态再释放资源：否则释放检查读到的仍是 leased，会误判"未核实结束"
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        if job.get("heavy"):
            _release_gpu_locked(c, job_id, force=False)
        return job


def fail(job_id: str, worker_id: str, error: Dict[str, Any]) -> Dict[str, Any]:
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        _check_writer(job, worker_id)
        job["state"] = "failed"
        job["error"] = error
        job["lease"] = None
        job["settlement_required"] = bool(job.get("billable"))
        job["updated_at"] = _iso()
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        if job.get("heavy"):
            _release_gpu_locked(c, job_id, force=False)
        return job


def mark_unknown(job_id: str, worker_id: Optional[str] = None, reason: str = "提交后未收到确定响应", *,
                 preserve_terminal: bool = False) -> Dict[str, Any]:
    """提交丢响应：状态记为 unknown，等待对账，不得直接改判成功或失败。"""
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        if preserve_terminal and job["state"] in TERMINAL_STATES:
            return job
        job["state"] = "unknown"
        job["unknown_reason"] = reason
        job["unknown_at"] = _iso()
        job["settlement_required"] = True
        job["updated_at"] = _iso()
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return job


def reconcile(job_id: str) -> Dict[str, Any]:
    """对账：unknown 作业必须先核实真实远端状态。

    明确不做的事：不创建替代计费任务。计费作业在核实前既不算成功也不重发。
    """
    job = get_job(job_id)
    if job is None:
        raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
    if job["state"] != "unknown":
        return {"job_id": job_id, "state": job["state"], "requires_reconciliation": False,
                "may_create_replacement": False, "note": "作业状态确定，无需对账"}
    return {
        "job_id": job_id,
        "state": "unknown",
        "requires_reconciliation": True,
        "may_create_replacement": False,
        "billable": bool(job.get("billable")),
        "note": "结果未知：先核实远端状态再定态；不得创建替代计费任务，也不得凭猜测改判成功",
        "action": "resolve_unknown",
    }


def resolve_unknown(job_id: str, outcome: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    """对账结论落定：outcome ∈ succeeded|failed|cancelled，必须带证据。"""
    if outcome not in ("succeeded", "failed", "cancelled"):
        raise JobRejected("VALIDATION_FAILED", "对账结论只能是 succeeded/failed/cancelled")
    if not evidence or not evidence.get("source"):
        raise JobRejected("EVIDENCE_REQUIRED", "对账结论必须附证据来源，不接受无证据改判")
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        if job["state"] != "unknown":
            raise JobRejected("STATE_CONFLICT", f"作业当前为 {job['state']}，不是待对账状态")
        job["state"] = outcome
        job["reconciliation"] = {"evidence": evidence, "at": _iso()}
        job["settlement_required"] = bool(job.get("billable")) and outcome != "cancelled"
        job["updated_at"] = _iso()
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        if job.get("heavy"):
            _release_gpu_locked(c, job_id, force=False)
        return job


def cancel(job_id: str, worker_id: Optional[str] = None) -> Dict[str, Any]:
    """取消：只停止新提交；已受理的外部作业仍需结算与对账。"""
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        if job["state"] in ("succeeded", "failed"):
            raise JobRejected("JOB_TERMINAL", "已完成的作业不能取消，只能对账")
        submitted = job["state"] in ("leased", "unknown")
        job["state"] = "cancelled"
        job["cancelled_at"] = _iso()
        # 已租出或状态未知的作业可能已在远端产生成本，取消不免除结算
        job["settlement_required"] = bool(submitted or job.get("billable"))
        job["lease"] = None
        job["updated_at"] = _iso()
        if job.get("heavy") and not submitted:
            _release_gpu_locked(c, job_id, force=False)
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return job


# --------------------------- content-v2 外部调用台账 ---------------------------
# 与旧媒体作业共用 provider_jobs 表和幂等表，但走独立的状态语义：
#   queued(已预留、从未发送) → leased/sending(发送前持久检查点) → succeeded | failed | unknown | waiting_external
# 发送只允许从 queued 进入；重启、租约过期、刷新一律不会把 sending/unknown 放回 queued，
# 因此同一幂等键最多发出一次请求。

CONTENT_NAMESPACE = "content-v2"


def content_call_key(*parts: Any) -> str:
    """幂等键：flow_id/step/round/candidate_set_digest/模型/请求摘要 等的稳定摘要，与 Agent attempt 无关。"""
    return "content-v2:" + args_digest(list(parts))[7:]


def submit_content_call(*, parent_id: str, parent_kind: str, work_id: str, provider: str, model: str,
                        capability: str, idempotency_key: str, request_digest: str,
                        price_snapshot: Optional[Dict[str, Any]] = None, reserve_fen: int = 0,
                        billable: bool = True, step: str = "", round_no: int = 0) -> Dict[str, Any]:
    """事务内：幂等查重 → 意图落库 → 记录预留 → queued。内容调用不设预算配额，网络请求在事务外。"""
    if not idempotency_key:
        raise JobRejected("VALIDATION_FAILED", "外部调用必须提供幂等键")
    with st.transaction() as c:
        _reject_deleted_work(work_id)
        prior = _idem_lookup(c, idempotency_key)
        if prior is not None:
            if prior.get("args_digest") != request_digest:
                raise JobRejected("IDEMPOTENCY_CONFLICT", "同一幂等键对应了不同的请求内容")
            return dict(prior, replayed=True)
        now = _iso()
        job: Dict[str, Any] = {
            "id": st.new_id("job"), "work_id": work_id, "task_id": None, "capability": capability,
            "provider": provider, "model": model, "args_digest": request_digest,
            "idempotency_key": idempotency_key, "namespace": CONTENT_NAMESPACE,
            "parent_content_job_id": parent_id, "step": step, "round": round_no,
            "billable": bool(billable), "amount_fen": int(reserve_fen), "heavy": False,
            "state": "intent", "settlement_required": False, "stale_writers": [], "attempts": 0,
            "price_snapshot": price_snapshot or {}, "reserved_fen": 0, "actual_cost_fen": None,
            "usage": None, "settlement_state": None, "note": "", "created_at": now, "updated_at": now,
        }
        st.insert_row(c, "provider_jobs", job["id"], job, {"work_id": work_id})
        c.execute("INSERT INTO provider_jobs_idem (idem_key,job_id) VALUES (?,?)",
                  [idempotency_key, job["id"]])
        if billable:
            job["reserved_fen"] = int(reserve_fen)
            job["settlement_state"] = "reserved"
        job.update(state="queued", updated_at=_iso())
        st.insert_row(c, "provider_jobs", job["id"], job, {"work_id": work_id})
        return dict(job, replayed=False)


def mark_sending(job_id: str, worker_id: str, ttl_seconds: Optional[int] = None) -> Dict[str, Any]:
    """发送前持久检查点。只有 queued 能进入，且状态先落库、后发请求。"""
    ttl = ttl_seconds or lease_ttl_seconds()
    now = _now()
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        if job.get("namespace") != CONTENT_NAMESPACE:
            raise JobRejected("VALIDATION_FAILED", "不是 content-v2 外部调用")
        if job["state"] != "queued":
            raise JobRejected("NOT_SENDABLE", f"作业当前为 {job['state']}，不是从未发送的 queued，禁止再次发送")
        job.update(state="leased", sending=True, sent_at=_iso(now), attempts=int(job.get("attempts", 0)) + 1,
                   lease={"worker_id": worker_id, "acquired_at": now, "expires_at": now + ttl,
                          "acquired_iso": _iso(now), "expires_iso": _iso(now + ttl)},
                   updated_at=_iso())
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return job


def _late_archive(job: dict, worker_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    job.setdefault("late_responses", []).append(
        {"worker_id": worker_id, "at": _iso(), "digest": args_digest(payload), "payload": payload})
    job["updated_at"] = _iso()
    return job


def settle_content_call(job_id: str, worker_id: str, *, response: Optional[Dict[str, Any]],
                        usage: Optional[Dict[str, Any]], actual_cost_fen: Optional[int],
                        provider_request_id: str = "", remote_task_id: Optional[str] = None,
                        pending: bool = False) -> Dict[str, Any]:
    """事务内保存响应与 usage，结算预留。

    - 有真实 usage 与费用：settled，按实际费用计入。
    - 缺 usage/费用：保留预留（held），不写 0 元；待人工对账。
    - pending（拿到远端 taskId 但结果未出）：waiting_external，预留保持。
    - 租约已失效/持有者已变：迟到响应只归档，不覆盖，不改状态。
    """
    stale = False
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        lease = job.get("lease") or {}
        holder_ok = job["state"] == "leased" and lease.get("worker_id") == worker_id
        if not holder_ok:
            _late_archive(job, worker_id, {"response": response, "usage": usage,
                                           "actual_cost_fen": actual_cost_fen})
            job["settlement_required"] = bool(job.get("billable"))
            st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
            stale = True
    if stale:  # 归档已提交后再抛出，否则异常会把归档一并回滚
        raise JobRejected("STALE_WRITER", "迟到响应已归档，不改变作业状态也不绑定业务结果")
    with st.transaction() as c:
        job = _load(c, job_id)
        lease = job.get("lease") or {}
        if job["state"] != "leased" or lease.get("worker_id") != worker_id:
            raise JobRejected("STALE_WRITER", "租约在结算前已失效")
        job["lease"] = None
        job["provider_request_id"] = provider_request_id
        if remote_task_id:
            job["remote_task_id"] = remote_task_id
        if pending:
            job["state"] = "waiting_external"
        else:
            job["state"] = "succeeded"
            job["result"] = response
            job["result_digest"] = args_digest(response)
            job["usage"] = usage
            if actual_cost_fen is not None:
                job.update(actual_cost_fen=int(actual_cost_fen), settlement_state="settled",
                           settlement_required=False)
            elif job.get("billable"):
                job.update(settlement_state="held", settlement_required=True)
        job["updated_at"] = _iso()
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return job


def finish_remote_call(job_id: str, *, response: Dict[str, Any], usage: Optional[Dict[str, Any]],
                       actual_cost_fen: Optional[int]) -> Dict[str, Any]:
    """waiting_external/unknown(有 remote_task_id) → succeeded：只能由对同一 remote_task_id 的查询结果触发。"""
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None or job["state"] not in ("waiting_external", "unknown") or not job.get("remote_task_id"):
            raise JobRejected("STATE_CONFLICT", "作业不在等待远端结果状态或没有远端句柄")
        job.update(state="succeeded", result=response, result_digest=args_digest(response), usage=usage)
        if actual_cost_fen is not None:
            job.update(actual_cost_fen=int(actual_cost_fen), settlement_state="settled",
                       settlement_required=False)
        elif job.get("billable"):
            job.update(settlement_state="held", settlement_required=True)
        job["updated_at"] = _iso()
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return job


def fail_content_call(job_id: str, worker_id: str, error: Dict[str, Any], *, charged: Optional[bool]) -> Dict[str, Any]:
    """已确认失败。charged=False（供应商明确拒绝且未计费）才释放预留；None/True 保留预留待对账。"""
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        lease = job.get("lease") or {}
        if job["state"] != "leased" or lease.get("worker_id") != worker_id:
            raise JobRejected("STALE_WRITER", "回写方不是当前持有者")
        job.update(state="failed", error=error, lease=None, updated_at=_iso())
        if job.get("billable"):
            if charged is False:
                job.update(settlement_state="released", settlement_required=False)
            else:
                job.update(settlement_state="held", settlement_required=True)
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return job


def release_unsent_call(job_id: str, worker_id: str, error: Dict[str, Any]) -> Dict[str, Any]:
    """已进入 sending 检查点但确定没有发出请求（如本地缺凭据）：failed 并释放预留。"""
    return fail_content_call(job_id, worker_id, dict(error, sent=False), charged=False)


def mark_content_unknown(job_id: str, reason: str) -> Dict[str, Any]:
    """发送后无法确认结果：unknown，保留预留，不写 0 元。已终态的调用不改判。"""
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        if job["state"] in TERMINAL_STATES:
            return job
        job.update(state="unknown", unknown_reason=reason, unknown_at=_iso(), settlement_required=True,
                   lease=None, updated_at=_iso())
        if job.get("billable"):
            job["settlement_state"] = "held"
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return job


def cancel_unsent_call(job_id: str) -> Dict[str, Any]:
    """只取消从未发出的 queued 调用并释放预留；已发送的调用取消不免除结算（沿用 cancel）。"""
    with st.transaction() as c:
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        if job["state"] != "queued":
            return job
        job.update(state="cancelled", cancelled_at=_iso(), updated_at=_iso(), settlement_required=False)
        if job.get("billable"):
            job["settlement_state"] = "released"
        st.insert_row(c, "provider_jobs", job_id, job, {"work_id": job.get("work_id")})
        return job


def recover_content_calls(parent_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """启动/续约恢复：只把"发送中且租约已过期"的调用转 unknown；绝不放回 queued。"""
    now = _now()
    changed = []
    with st.transaction() as c:
        for row in c.execute("SELECT data FROM provider_jobs").fetchall():
            job = json.loads(row["data"])
            if job.get("namespace") != CONTENT_NAMESPACE or job["state"] != "leased":
                continue
            if parent_id and job.get("parent_content_job_id") != parent_id:
                continue
            lease = job.get("lease") or {}
            if lease.get("expires_at", 0) > now:
                continue
            job.update(state="unknown", unknown_reason="发送后进程中断或租约过期，结果未知",
                       unknown_at=_iso(), settlement_required=True, lease=None, updated_at=_iso())
            if job.get("billable"):
                job["settlement_state"] = "held"
            st.insert_row(c, "provider_jobs", job["id"], job, {"work_id": job.get("work_id")})
            changed.append(job)
    return changed


def content_calls(parent_id: str) -> List[dict]:
    return [j for j in st.all_rows("provider_jobs")
            if j.get("namespace") == CONTENT_NAMESPACE and j.get("parent_content_job_id") == parent_id]


# --------------------------- 重型资源互斥 ---------------------------

def gpu_holder() -> Optional[dict]:
    return st.get("resource_locks", GPU_LOCK_ID)


def acquire_gpu(job_id: str, worker_id: str) -> Dict[str, Any]:
    """单 GPU 互斥：逻辑并行不等于 GPU 并行。"""
    now = _now()
    with st.transaction() as c:
        cur = st.get("resource_locks", GPU_LOCK_ID)
        job = _load(c, job_id)
        if job is None:
            raise JobRejected("JOB_NOT_FOUND", f"作业不存在：{job_id}")
        if cur and cur.get("released_at") is None:
            holder = cur.get("job_id")
            if holder != job_id:
                # 过期作业未核实结束前，不把资源交给冲突任务
                hjob = _load(c, holder)
                unverified = hjob is None or hjob.get("state") in ("leased", "unknown")
                raise JobRejected(
                    "GPU_BUSY",
                    f"GPU 被作业 {holder} 占用（状态 {hjob.get('state') if hjob else '?'}）；"
                    + ("未核实结束前不释放" if unverified else "等待释放"),
                )
        lock = {"id": GPU_LOCK_ID, "job_id": job_id, "worker_id": worker_id,
                "acquired_at": now, "acquired_iso": _iso(now), "released_at": None}
        st.insert_row(c, "resource_locks", GPU_LOCK_ID, lock)
        return lock


def release_gpu(job_id: str, force: bool = False) -> Dict[str, Any]:
    with st.transaction() as c:
        return _release_gpu_locked(c, job_id, force=force)


def _release_gpu_locked(c, job_id: str, force: bool = False) -> Dict[str, Any]:
    cur = st.get("resource_locks", GPU_LOCK_ID)
    if not cur or cur.get("released_at") is not None:
        return {"id": GPU_LOCK_ID, "released": False, "reason": "当前无占用"}
    if cur.get("job_id") != job_id and not force:
        raise JobRejected("GPU_NOT_HELD", f"GPU 当前由作业 {cur.get('job_id')} 占用，不能被 {job_id} 释放")
    holder = _load(c, cur["job_id"])
    if not force and holder and holder.get("state") in ("leased", "unknown"):
        raise JobRejected("GPU_BUSY_UNVERIFIED",
                          "占用作业尚未核实结束（leased/unknown），不得释放给冲突任务")
    cur["released_at"] = _now()
    cur["released_iso"] = _iso(cur["released_at"])
    st.insert_row(c, "resource_locks", GPU_LOCK_ID, cur)
    return {"id": GPU_LOCK_ID, "released": True}


# --------------------------- 查询 ---------------------------

def jobs_for_work(work_id: str) -> List[dict]:
    return st.all_rows("provider_jobs", "work_id=?", [work_id])


def unsettled_jobs(work_id: Optional[str] = None) -> List[dict]:
    rows = st.all_rows("provider_jobs", "work_id=?" if work_id else None, [work_id] if work_id else None)
    return [j for j in rows if j.get("settlement_required")]


def ledger_consistent(work_id: str) -> Dict[str, Any]:
    """账本与任务状态一致性检查：检查点不能代替账本。"""
    rows = jobs_for_work(work_id)
    problems = []
    for j in rows:
        if j["state"] == "leased" and not j.get("lease"):
            problems.append({"job_id": j["id"], "code": "LEASE_MISSING"})
        if j["state"] == "unknown" and not j.get("unknown_at"):
            problems.append({"job_id": j["id"], "code": "UNKNOWN_WITHOUT_REASON"})
        if j["state"] == "blocked" and j.get("lease"):
            problems.append({"job_id": j["id"], "code": "BLOCKED_BUT_LEASED"})
        if j["state"] == "succeeded" and not j.get("result_digest"):
            problems.append({"job_id": j["id"], "code": "SUCCESS_WITHOUT_DIGEST"})
    return {"work_id": work_id, "jobs": len(rows), "consistent": not problems, "problems": problems}
