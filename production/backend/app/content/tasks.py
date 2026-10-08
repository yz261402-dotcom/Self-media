"""口播稿升级的持久父任务、调度器与外部调用包装。

- content_jobs 是流程进度（用户看得到的进度）；provider_jobs 是收费副作用（唯一台账）。
- 唯一键 (work_id, kind, request_id)：同 payload 重放返回原任务，异 payload 报 IDEMPOTENCY_CONFLICT。
- 会改变当前提示/稿件的任务（MUTATING_KINDS）同作品单飞，并占用 work.current_content_job_id。
- 结果只在任务仍是"当前"时才落到业务绑定；否则任务转 superseded，结果归档在任务里。
- 网络请求一律在事务之外；重启/刷新/租约过期不会重发已发送的调用。
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import nullcontext
from http.client import RemoteDisconnected
from urllib.error import URLError
from typing import Any, Callable, Dict, List, Optional

from .. import jobs
from .. import storage as st
from ..contracts import now_iso
from . import costs
from .errors import ContentBlocked, ContentError, ContentRejected

MUTATING_KINDS = ("prompt", "prompt_revision", "generation", "repair", "reference_revision", "review",
                  "visual_analysis")
ALL_KINDS = ("search", "import", "media_fetch") + MUTATING_KINDS

ACTIVE_STATES = ("queued", "running", "waiting_external")
TERMINAL_STATES = ("succeeded", "partial", "blocked", "failed", "unknown", "cancelled", "superseded")


class IdempotencyConflict(ContentError):
    code = "IDEMPOTENCY_CONFLICT"


class FlowActive(ContentError):
    code = "CONTENT_FLOW_ACTIVE"


class CallPending(ContentError):
    """该外部调用已发送但结果未确认，不会自动重发。"""
    code = "CONTENT_CALL_PENDING"


def response_timeout_error(call_id: str) -> CallPending:
    return CallPending("外部服务响应超时，原请求的结果和费用尚未确认。系统不会自动重发；"
                       "再次提交前须重新确认可能产生的费用。",
                       code="PROVIDER_RESPONSE_TIMEOUT", detail=call_id)


def connection_closed_error(call_id: str) -> CallPending:
    return CallPending("外部服务连接在返回结果前断开，原请求的结果和费用尚未确认。系统不会自动重发；"
                       "请先核对中转服务调用记录，再确认是否重新生成。",
                       code="PROVIDER_CONNECTION_CLOSED", detail=call_id)


class JobCancelled(ContentError):
    code = "JOB_CANCELLED"


class JobSuperseded(ContentError):
    code = "SUPERSEDED"


def checkpoint(job: dict) -> None:
    """步骤之间的检查点：已取消或输入已变化就停止后续收费步骤。"""
    latest = get_job(job["id"]) or job
    if latest.get("cancel_requested") or latest["state"] == "cancelled":
        raise JobCancelled("任务已取消，不再执行后续步骤")
    with st.transaction() as c:
        current = is_current(c, latest)
    if not current:
        raise JobSuperseded("创作输入已变化，本任务已被取代，不再执行后续收费步骤")


class NotSent(Exception):
    """send 回调抛出：请求确定没有离开本机（如缺凭据、参数非法），可释放预留。"""

    def __init__(self, message: str, code: str = "NOT_SENT"):
        super().__init__(message)
        self.message, self.code = message, code


class ProviderRefused(Exception):
    """供应商明确拒绝且未计费（401/403/429/400 等）。charged=False 才释放预留。"""

    def __init__(self, message: str, code: str = "PROVIDER_REFUSED", status: Optional[int] = None,
                 charged: Optional[bool] = False):
        super().__init__(message)
        self.message, self.code, self.status, self.charged = message, code, status, charged


class CallOutcome:
    def __init__(self, response: Optional[Dict[str, Any]] = None, usage: Optional[Dict[str, Any]] = None,
                 cost_fen: Optional[int] = None, provider_request_id: str = "",
                 remote_task_id: Optional[str] = None, pending: bool = False):
        self.response, self.usage, self.cost_fen = response, usage, cost_fen
        self.provider_request_id, self.remote_task_id, self.pending = provider_request_id, remote_task_id, pending


def payload_digest(payload: Any) -> str:
    return jobs.args_digest(payload)


# ------------------------------------------------------------------ 父任务存取

def _row(c, job_id: str) -> Optional[dict]:
    r = c.execute("SELECT data FROM content_jobs WHERE id=?", [job_id]).fetchone()
    return json.loads(r["data"]) if r else None


def _write(c, job: dict) -> None:
    job["updated_at"] = now_iso()
    st.insert_row(c, "content_jobs", job["id"], job, {
        "work_id": job["work_id"], "kind": job["kind"], "request_id": job["request_id"],
        "input_digest": job["input_digest"], "state": job["state"]})


def get_job(job_id: str) -> Optional[dict]:
    return st.get("content_jobs", job_id)


def list_jobs(work_id: str, *, kinds: Optional[List[str]] = None, active_only: bool = False) -> List[dict]:
    rows = st.all_rows("content_jobs", "work_id=?", [work_id])
    if kinds:
        rows = [r for r in rows if r["kind"] in kinds]
    if active_only:
        rows = [r for r in rows if r["state"] in ACTIVE_STATES]
    return sorted(rows, key=lambda r: r["created_at"])


def _work(c, work_id: str) -> dict:
    r = c.execute("SELECT data FROM works WHERE id=?", [work_id]).fetchone()
    if r is None:
        raise ContentRejected(f"作品不存在：{work_id}", "WORK_NOT_FOUND")
    work = json.loads(r["data"])
    if work.get("deleted_at"):
        raise ContentRejected("作品已删除，不能创建内容任务", "WORK_DELETED")
    return work


def submit_job(work_id: str, kind: str, request_id: str, payload: Dict[str, Any], *,
               expected_revision: Optional[int] = None, _connection=None) -> Dict[str, Any]:
    """创建（或重放）父任务，返回带 replayed 标记的任务。"""
    if kind not in ALL_KINDS:
        raise ContentRejected(f"未知内容任务类型：{kind}", "VALIDATION_FAILED")
    if not request_id:
        raise ContentRejected("写操作必须带 request_id", "VALIDATION_FAILED")
    digest = payload_digest({"kind": kind, "payload": payload})
    with nullcontext(_connection) if _connection is not None else st.transaction() as c:
        work = _work(c, work_id)
        row = c.execute("SELECT data FROM content_jobs WHERE work_id=? AND kind=? AND request_id=?",
                        [work_id, kind, request_id]).fetchone()
        if row is not None:
            prior = json.loads(row["data"])
            if prior["input_digest"] != digest:
                raise IdempotencyConflict("同一 request_id 对应了不同的请求内容", detail=request_id)
            return dict(prior, replayed=True)
        revision = int(work.get("content_revision") or 0)
        if expected_revision is not None and expected_revision != revision:
            from .repository import StaleContentWrite
            raise StaleContentWrite(f"创作输入已变化（当前 {revision}，请求基于 {expected_revision}）")
        mutating = kind in MUTATING_KINDS
        if mutating:
            for other in c.execute("SELECT data FROM content_jobs WHERE work_id=? AND state IN "
                                   "('queued','running','waiting_external')", [work_id]).fetchall():
                o = json.loads(other["data"])
                if o["kind"] in MUTATING_KINDS:
                    raise FlowActive("该作品已有进行中的写作流程，请等待完成或先取消", detail=o["id"])
        job = {
            "id": st.new_id("cjob"), "work_id": work_id, "kind": kind, "request_id": request_id,
            "input_digest": digest, "state": "queued", "stage": "queued", "payload": payload,
            "content_revision": revision, "mutating": mutating, "result_refs": {}, "error": None,
            "cancel_requested": False, "created_at": now_iso(), "updated_at": now_iso(),
        }
        if kind in ("generation", "repair"):
            # 首次创建才冻结执行身份，不参与用户 payload 摘要；部署后同 request_id 仍重放原任务。
            from .providers import text as text_provider
            job["writer_runtime_profiles"] = {role: text_provider.runtime_profile(role)["profile_id"]
                                              for role in ("writer", "repair")}
        _write(c, job)
        if mutating:
            work["current_content_job_id"] = job["id"]
            work["version"] = int(work.get("version") or 1) + 1
            st.insert_row(c, "works", work_id, work)
        return dict(job, replayed=False)


def is_current(c, job: dict) -> bool:
    """任务是否仍能改变当前业务绑定：修订号一致，且（会改状态的任务）仍是当前指针。"""
    work = _work(c, job["work_id"])
    if int(work.get("content_revision") or 0) != int(job["content_revision"]):
        return False
    if job.get("mutating") and work.get("current_content_job_id") != job["id"]:
        return False
    return True


def set_progress(job_id: str, *, stage: Optional[str] = None, state: Optional[str] = None,
                 **fields: Any) -> dict:
    """推进阶段/状态。终态不可改；取消请求生效后不再往 running 推进。"""
    with st.transaction() as c:
        job = _row(c, job_id)
        if job is None:
            raise ContentRejected(f"内容任务不存在：{job_id}", "JOB_NOT_FOUND")
        if job["state"] in TERMINAL_STATES:
            return job
        if state in ("running", "waiting_external") and job.get("cancel_requested"):
            job["state"], job["stage"] = "cancelled", job.get("stage", "")
        else:
            if state:
                job["state"] = state
            if stage:
                job["stage"] = stage
        job.update(fields)
        _write(c, job)
        return job


def finish_job(job_id: str, state: str, *, result_refs: Optional[Dict[str, Any]] = None,
               error: Optional[Dict[str, Any]] = None,
               apply: Optional[Callable[[Any], None]] = None) -> dict:
    """终结父任务。

    apply(connection) 在同一事务内写业务绑定，且只在任务仍是当前时执行；
    否则任务记为 superseded，结果只归档不绑定（A→B→A 也一样：比对的是修订号而非摘要）。
    """
    if state not in TERMINAL_STATES:
        raise ContentRejected(f"{state} 不是终态", "VALIDATION_FAILED")
    with st.transaction() as c:
        job = _row(c, job_id)
        if job is None:
            raise ContentRejected(f"内容任务不存在：{job_id}", "JOB_NOT_FOUND")
        if job["state"] in TERMINAL_STATES:
            return job
        bindable = state in ("succeeded", "partial")
        if job.get("cancel_requested") and bindable:
            job.update(state="cancelled", stage="cancelled", archived_result=result_refs or {})
        elif bindable and not is_current(c, job):
            job.update(state="superseded", stage="superseded", archived_result=result_refs or {},
                       error={"code": "SUPERSEDED", "message": "创作输入已变化，结果已归档，未绑定为当前稿件"})
        else:
            if bindable and apply is not None:
                apply(c)
            job.update(state=state, error=error)
            if result_refs is not None:
                job["result_refs"] = result_refs
            job["stage"] = state if state != "succeeded" else "done"
        _write(c, job)
        _finish_direct_main_task(c, job)
        return job


def _finish_direct_main_task(c, job: dict) -> None:
    if (job.get("payload") or {}).get("orchestration") != "direct" or not is_current(c, job):
        return
    work = _work(c, job["work_id"])
    row = c.execute("SELECT data FROM tasks WHERE id=?", [work.get("current_task_id") or ""]).fetchone()
    if row is None:
        return
    task = json.loads(row["data"])
    if job["state"] in ("succeeded", "partial"):
        task.update(stage="content_review", state="waiting_selection", last_error=None, block_reason=None)
        message = "候选稿件与独立审核已保存，等待人工审查或重新审核"
    else:
        err = job.get("error") or {}
        task.update(stage="script", state="blocked" if job["state"] in ("unknown", "blocked") else "failed",
                    last_error={"code": err.get("code") or "CONTENT_JOB_FAILED",
                                "message": err.get("message") or f"内容任务未成功（{job['state']}）", "retryable": False})
        message = task["last_error"]["message"]
    task["stage_rows"] = st.stage_rows(task["stage"], task["state"])
    st.insert_row(c, "tasks", task["id"], task, {"work_id": work["id"]})
    st.add_event_tx(c, task["id"], {"type": "stage.completed" if job["state"] in ("succeeded", "partial") else "stage.failed",
                                    "occurred_at": now_iso(), "stage": task["stage"], "state": task["state"],
                                    "message": message})


def cancel_job(job_id: str) -> dict:
    """禁止后续步骤；已发送的调用保留结算，不承诺远端取消。"""
    with st.transaction() as c:
        job = _row(c, job_id)
        if job is None:
            raise ContentRejected(f"内容任务不存在：{job_id}", "JOB_NOT_FOUND")
        if job["state"] in TERMINAL_STATES:
            return job
        job["cancel_requested"] = True
        if job["state"] == "queued":
            job.update(state="cancelled", stage="cancelled")
        _write(c, job)
    if job["state"] == "cancelled":
        for call in jobs_calls(job_id):
            if call["state"] == "queued":
                jobs.cancel_unsent_call(call["id"])
    return get_job(job_id)


def jobs_calls(parent_id: str) -> List[dict]:
    return jobs.content_calls(parent_id)


def mark_job_unknown(job_id: str, reason: str) -> dict:
    return finish_job(job_id, "unknown", error={"code": "CONTENT_RESULT_UNKNOWN", "message": reason})


# ------------------------------------------------------------------ 外部调用包装

def run_call(*, parent: dict, provider: str, model: str, capability: str, step: str, round_no: int,
             request: Dict[str, Any], send: Callable[[], CallOutcome],
             price_snapshot: Optional[Dict[str, Any]] = None, reserve_fen: int = 0,
             billable: bool = True, worker_id: str = "content-worker",
             idempotency_key: Optional[str] = None, lease_seconds: Optional[int] = None) -> Dict[str, Any]:
    """一次外部调用的完整生命周期：预留 → 发送前检查点 → 事务外请求一次 → 结算。

    返回已 succeeded 的 provider job（含 result/usage）。其他结局以异常表达：
    ContentBlocked（预算/门禁）、CallPending（已发送但结果未确认）、ContentRejected（已确认失败）。
    同一幂等键重复进入只会取回原结果，绝不产生第二次请求。
    """
    digest = payload_digest(request)
    key = idempotency_key or jobs.content_call_key(parent["id"], step, round_no, provider, model, digest)
    call = jobs.submit_content_call(
        parent_id=parent["id"], parent_kind=parent["kind"], work_id=parent["work_id"], provider=provider,
        model=model, capability=capability, idempotency_key=key, request_digest=digest,
        price_snapshot=price_snapshot, reserve_fen=reserve_fen, billable=billable,
        step=step, round_no=round_no)
    state = call["state"]
    if state == "succeeded":
        return call
    if state == "blocked":
        raise ContentBlocked(call.get("block_detail", "预算未放行"), call.get("block_code", "BLOCKED"),
                             retryable=False)
    if state == "failed":
        err = call.get("error") or {}
        raise ContentRejected(err.get("message", "外部调用已失败"), err.get("code", "CALL_FAILED"))
    if state != "queued":  # leased / unknown / waiting_external / cancelled：不重发
        raise CallPending(f"外部调用 {call['id']} 状态为 {state}，已发送的调用不会自动重发", detail=call["id"])
    if get_job(parent["id"]) and get_job(parent["id"]).get("cancel_requested"):
        jobs.cancel_unsent_call(call["id"])
        raise ContentBlocked("任务已取消，未发送该调用", "JOB_CANCELLED", retryable=False)
    try:
        if lease_seconds is None:
            jobs.mark_sending(call["id"], worker_id)
        else:
            jobs.mark_sending(call["id"], worker_id,
                              ttl_seconds=max(jobs.lease_ttl_seconds(), int(lease_seconds)))
    except jobs.JobRejected as exc:  # 竞争失败：别的进程已抢到发送权
        raise CallPending(f"外部调用 {call['id']} 已被其他执行者发送", detail=call["id"]) from exc
    try:
        out = send()
    except NotSent as exc:
        jobs.release_unsent_call(call["id"], worker_id, {"code": exc.code, "message": exc.message})
        raise ContentBlocked(exc.message, exc.code, retryable=False) from exc
    except ProviderRefused as exc:
        jobs.fail_content_call(call["id"], worker_id,
                               {"code": exc.code, "message": exc.message, "status": exc.status},
                               charged=exc.charged)
        raise ContentRejected(exc.message, exc.code) from exc
    except Exception as exc:  # noqa: BLE001  发送后的任何不确定异常：保守 unknown，保留预留
        jobs.mark_content_unknown(call["id"], f"{type(exc).__name__}: 发送后未收到确定响应")
        if isinstance(exc, TimeoutError) or (isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)):
            raise response_timeout_error(call["id"]) from exc
        cause = exc.reason if isinstance(exc, URLError) else exc
        if isinstance(cause, (RemoteDisconnected, ConnectionResetError, BrokenPipeError)):
            raise connection_closed_error(call["id"]) from exc
        raise CallPending("请求已发出但未收到确定响应，结果未知，需核对，不会自动重发",
                          detail=call["id"]) from exc
    try:
        settled = jobs.settle_content_call(
            call["id"], worker_id, response=out.response, usage=out.usage, actual_cost_fen=out.cost_fen,
            provider_request_id=out.provider_request_id, remote_task_id=out.remote_task_id,
            pending=out.pending)
    except jobs.JobRejected as exc:  # 租约已失效：迟到响应已归档
        raise CallPending("响应到达时租约已失效，已归档，未采用", detail=call["id"]) from exc
    if settled["state"] == "waiting_external":
        raise CallPending("远端任务尚未完成，将只用同一 taskId 查询", detail=call["id"])
    return settled


def resume_remote(call_id: str, query: Callable[[str], CallOutcome]) -> Dict[str, Any]:
    """已保存 remote taskId 的调用：只用同一个 taskId 查询，绝不重新提交。"""
    call = jobs.get_job(call_id)
    if call is None or call.get("namespace") != jobs.CONTENT_NAMESPACE:
        raise ContentRejected("不是 content-v2 外部调用", "JOB_NOT_FOUND")
    if call["state"] == "succeeded":
        return call
    task_id = call.get("remote_task_id")
    if call["state"] not in ("waiting_external", "unknown") or not task_id:
        raise CallPending("没有可查询的远端句柄，需要人工核对；不会重新提交", detail=call_id)
    out = query(task_id)
    if out.pending:
        return call
    return jobs.finish_remote_call(call_id, response=out.response or {}, usage=out.usage,
                                   actual_cost_fen=out.cost_fen)


# ------------------------------------------------------------------ 调度器与恢复

Handler = Callable[[dict], None]
HANDLERS: Dict[str, Handler] = {}


def register(kind: str, handler: Handler) -> None:
    HANDLERS[kind] = handler


def _import_remote_call(job: dict) -> Optional[dict]:
    if job["kind"] != "import":
        return None
    calls = jobs_calls(job["id"])
    if not any(call.get("step") == "detail" and call["state"] == "succeeded" for call in calls):
        return None
    if any(call["state"] in ("unknown", "leased") for call in calls):
        return None
    return next((call for call in calls if call.get("provider") == "redfox"
                 and call.get("step") == "transcript_submit" and call.get("remote_task_id")
                 and call["state"] in ("waiting_external", "succeeded")), None)


def _remote_poll_settings() -> tuple[int, int]:
    def setting(name: str, default: int, lower: int, upper: int) -> int:
        try:
            return min(upper, max(lower, int(os.environ.get(name, "") or default)))
        except ValueError:
            return default
    return (setting("AVATAR_STUDIO_REDFOX_AUTO_POLL_INTERVAL_SECONDS", 10, 1, 300),
            setting("AVATAR_STUDIO_REDFOX_AUTO_POLL_TIMEOUT_SECONDS", 600, 60, 86400))


def _remote_poll(job: dict, *, reset: bool = False) -> dict:
    poll = {} if reset else dict(job.get("remote_poll") or {})
    now = time.time()
    _, timeout = _remote_poll_settings()
    poll.setdefault("next_at", now)
    poll.setdefault("deadline_at", now + timeout)
    poll.setdefault("attempts", 0)
    return poll


def _defer_import_poll(job_id: str, exc: CallPending) -> None:
    with st.transaction() as c:
        job = _row(c, job_id)
        if job is None or job["state"] in TERMINAL_STATES:
            return
        if job.get("cancel_requested"):
            job.update(state="cancelled", stage="cancelled")
        else:
            poll = _remote_poll(job)
            retry = exc.code == "REFERENCE_QUERY_RETRY"
            failures = int(poll.get("failures") or 0) + 1 if retry else 0
            interval, _ = _remote_poll_settings()
            delay = min(60, interval * 2 ** min(max(failures - 1, 0), 3))
            poll.update(next_at=time.time() + delay, failures=failures,
                        last_checked_at=now_iso(), last_error=exc.to_dict() if retry else None)
            job.update(state="waiting_external", stage="transcript", remote_poll=poll, error=None)
        _write(c, job)


def request_import_refresh(job_id: str) -> dict:
    """只恢复已知转写句柄；正在执行的任务不会被另一个刷新请求重新排队。"""
    with st.transaction() as c:
        job = _row(c, job_id)
        if job is None:
            raise ContentRejected("参考导入任务不存在", "JOB_NOT_FOUND")
        if job.get("cancel_requested") or job["state"] in ("cancelled", "superseded"):
            return job
        paused = job["state"] == "unknown" and (job.get("error") or {}).get("code") in (
            "REFERENCE_POLL_TIMEOUT", "REFERENCE_QUERY_FAILED")
        if job["state"] != "waiting_external" and not paused:
            return job
        if _import_remote_call(job) is None:
            return job
        poll = _remote_poll(job, reset=paused)
        poll["next_at"] = time.time()
        job.update(state="queued", stage="transcript", remote_poll=poll, error=None)
        _write(c, job)
        return job


def recover_on_startup() -> Dict[str, int]:
    """启动恢复，仅扫描 content-v2：

    - 发送中且租约已过期的外部调用 → unknown（不回 queued）；
    - running/waiting 的父任务：有未确认调用 → unknown；否则回 queued 由调度器续跑
      （续跑复用同一幂等键，已成功的调用直接取回结果）。
    """
    unknown_calls = jobs.recover_content_calls()
    requeued = made_unknown = 0
    for row in st.all_rows("content_jobs"):
        if row["state"] not in ("running", "waiting_external"):
            continue
        calls = jobs_calls(row["id"])
        if any(c["state"] in ("unknown", "leased") for c in calls):
            finish_job(row["id"], "unknown", error={"code": "CONTENT_RESULT_UNKNOWN",
                                                    "message": "重启时存在发送后未确认的外部调用，需核对，未自动重发"})
            made_unknown += 1
        elif any(c["state"] == "waiting_external" for c in calls):
            if _import_remote_call(row) is not None:
                with st.transaction() as c:
                    job = _row(c, row["id"])
                    if job["state"] in ("running", "waiting_external"):
                        poll = _remote_poll(job)
                        poll["next_at"] = time.time()
                        job.update(state="waiting_external", stage="transcript", remote_poll=poll)
                        _write(c, job)
            continue
        else:
            with st.transaction() as c:
                job = _row(c, row["id"])
                job.update(state="queued", stage=job.get("stage", "queued"))
                _write(c, job)
            requeued += 1
    return {"calls_unknown": len(unknown_calls), "jobs_requeued": requeued, "jobs_unknown": made_unknown}


def _claim(concurrency: int) -> Optional[dict]:
    """领取排队任务或到期的参考查询；等待查询间隔不占执行槽。"""
    with st.transaction() as c:
        running = c.execute("SELECT COUNT(*) AS n FROM content_jobs WHERE state='running'").fetchone()["n"]
        if running >= concurrency:
            return None
        rows = c.execute("SELECT data FROM content_jobs WHERE state='queued' OR "
                         "(state='waiting_external' AND kind='import') "
                         "ORDER BY CASE WHEN state='queued' THEN 0 ELSE 1 END, ins_seq").fetchall()
        for row in rows:
            job = json.loads(row["data"])
            if job.get("cancel_requested"):
                job.update(state="cancelled", stage="cancelled")
                _write(c, job)
                continue
            if job["state"] == "waiting_external":
                if _import_remote_call(job) is None:
                    continue
                if _remote_poll(job)["next_at"] > time.time():
                    continue
            if job["kind"] == "import":
                poll = _remote_poll(job)
                poll["attempts"] += 1
                job["remote_poll"] = poll
            job.update(state="running", stage=job.get("stage") if job.get("stage") != "queued" else "starting")
            _write(c, job)
            return job
        return None


def run_job(job: dict) -> None:
    """执行一个已领取的任务：处理器抛出的异常统一映射成终态，绝不吞掉成未知的成功。"""
    handler = HANDLERS.get(job["kind"])
    try:
        if handler is None:
            raise ContentBlocked(f"没有注册 {job['kind']} 的处理器", "HANDLER_MISSING", retryable=False)
        handler(job)
    except Exception as exc:  # noqa: BLE001
        run_job_error(job, exc)
    else:
        latest = get_job(job["id"])
        if latest and latest["state"] in ACTIVE_STATES:  # 处理器忘记终结：不能留假进行中
            finish_job(job["id"], "failed", error={"code": "HANDLER_DID_NOT_FINISH", "message": "处理器未给出终态"})


def run_job_error(job: dict, exc: BaseException) -> None:
    """处理器异常 → 父任务终态。unknown/blocked/failed/superseded/cancelled 各自区分，不吞成成功。"""
    try:
        raise exc
    except CallPending as exc:
        latest = get_job(job["id"]) or job
        calls = jobs_calls(job["id"])
        if latest.get("cancel_requested"):
            finish_job(job["id"], "cancelled")
        elif exc.code in ("REFERENCE_POLL_TIMEOUT", "REFERENCE_QUERY_FAILED"):
            finish_job(job["id"], "unknown", error=exc.to_dict())
        elif _import_remote_call(latest) is not None and latest["state"] not in TERMINAL_STATES:
            _defer_import_poll(job["id"], exc)
        elif any(c["state"] == "waiting_external" for c in calls) and latest["state"] not in TERMINAL_STATES:
            set_progress(job["id"], state="waiting_external", stage=latest.get("stage"))
        else:
            finish_job(job["id"], "unknown", error={"code": exc.code, "message": exc.message, "detail": exc.detail})
    except JobCancelled:
        finish_job(job["id"], "cancelled")
    except JobSuperseded as exc:
        finish_job(job["id"], "superseded", error=exc.to_dict())
    except ContentBlocked as exc:
        finish_job(job["id"], "blocked", error=exc.to_dict())
    except ContentError as exc:
        finish_job(job["id"], "failed", error=exc.to_dict())
    except Exception as exc:  # noqa: BLE001
        finish_job(job["id"], "failed", error={"code": "CONTENT_INTERNAL_ERROR", "message": type(exc).__name__})


def claim_job(job_id: str) -> Optional[dict]:
    """CAS 领取指定任务：只有 queued 能被领走，调度器与同步入口不会重复执行同一任务。"""
    with st.transaction() as c:
        job = _row(c, job_id)
        if job is None or job["state"] != "queued":
            return None
        job.update(state="running", stage=job.get("stage") if job.get("stage") != "queued" else "starting")
        _write(c, job)
        return job


def run_inline(job_id: str, *, wait_seconds: float) -> dict:
    """同步入口用：领取并在后台线程执行，最多等 wait_seconds。

    超时不取消任务——任务继续在后台跑，调用方拿到的仍是非终态的父任务（据此返回 job_id 供轮询）。
    已被调度器领走或已终态的任务不会被再次执行。
    """
    claimed = claim_job(job_id)
    if claimed is not None:
        threading.Thread(target=run_job, args=(claimed,), name=f"content-inline-{job_id}", daemon=True).start()
    end = time.time() + wait_seconds
    while True:
        job = get_job(job_id)
        if job is None or job["state"] in TERMINAL_STATES or time.time() >= end:
            return job
        time.sleep(0.05)


def find_job(work_id: str, kind: str, request_id: str) -> Optional[dict]:
    row = st.conn().execute("SELECT data FROM content_jobs WHERE work_id=? AND kind=? AND request_id=?",
                            [work_id, kind, request_id]).fetchone()
    return json.loads(row["data"]) if row else None


class Scheduler:
    """单进程内容调度器。并发上限来自配置；同作品写作单飞已由 submit_job 保证。"""

    def __init__(self, concurrency: Optional[int] = None, poll_seconds: float = 0.5):
        self.concurrency = concurrency or costs.content_limits().concurrency
        self.poll = poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._workers: List[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="content-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        for w in list(self._workers):
            w.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001  调度线程不因单次异常退出
                pass
            self._stop.wait(self.poll)

    def tick(self) -> int:
        """领取并异步执行，直到并发满或没有 queued。返回本次领取数。"""
        claimed = 0
        while True:
            with self._lock:
                self._workers = [w for w in self._workers if w.is_alive()]
                if len(self._workers) >= self.concurrency:
                    return claimed
            job = _claim(self.concurrency)
            if job is None:
                return claimed
            worker = threading.Thread(target=run_job, args=(job,), name=f"content-{job['id']}", daemon=True)
            with self._lock:
                self._workers.append(worker)
            worker.start()
            claimed += 1

    def drain(self, timeout: float = 30.0) -> None:
        """测试用：等所有活动 worker 结束且没有 queued。"""
        end = time.time() + timeout
        while time.time() < end:
            self.tick()
            with self._lock:
                alive = [w for w in self._workers if w.is_alive()]
            queued = st.conn().execute("SELECT COUNT(*) AS n FROM content_jobs WHERE state='queued'").fetchone()["n"]
            if not alive and not queued:
                return
            time.sleep(0.02)


_scheduler: Optional[Scheduler] = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
