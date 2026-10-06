"""重型作业队列：Agent 可并行准备，GPU 重型作业串行（AC-AG-04）。

要点（都是"看起来并行、实际串行"这类项目最容易含糊过去的地方）：

- **排队不是失败。** GPU 被占用时，作业留在 queued，Worker 交还租约退出；
  只有真实执行失败才写 failed。
- **未核实结束的作业不释放资源。** 状态为 leased / unknown 的持有者必须
  先对账，才能把 GPU 让给冲突作业。
- **逻辑并行 ≠ GPU 并行。** 稿件、TTS、人物素材可以并行准备；
  提交到同一张卡上的重推理必须一个接一个。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .. import jobs, storage as st


def queued_heavy() -> List[Dict[str, Any]]:
    rows = [j for j in st.all_rows("provider_jobs")
            if j.get("heavy") and j["state"] == "queued"]
    return sorted(rows, key=lambda r: (r.get("created_at", ""), r["id"]))


def claim_next_heavy(worker_id: str) -> Dict[str, Any]:
    """认领下一个重型作业。

    三种结果必须能区分：
    - claimed：拿到租约与 GPU；
    - queue_empty：没有待办；
    - gpu_busy：有作业，但资源被占——作业仍在排队（不是失败）。
    """
    candidates = queued_heavy()
    if not candidates:
        return {"claimed": None, "reason": "QUEUE_EMPTY",
                "note": "当前没有待执行的重型作业"}
    for job in candidates:
        job_id = job["id"]
        try:
            lease = jobs.acquire_lease(job_id, worker_id)
        except jobs.JobRejected:
            # 已被他人持有 / 已终态：跳过，看下一个，不改变任何状态
            continue
        try:
            lock = jobs.acquire_gpu(job_id, worker_id)
        except jobs.JobRejected as e:
            # 资源忙：把租约交还，作业回到 queued，绝不写成 failed
            jobs.release_lease(job_id, worker_id, next_state="queued",
                               reason=f"{e.code}: {e.message}")
            return {"claimed": None, "reason": e.code, "job_id": job_id,
                    "message": e.message,
                    "note": "GPU 被占用；作业保持排队，排队不构成失败"}
        return {"claimed": jobs.get_job(job_id), "lease": lease, "gpu": lock,
                "reason": "CLAIMED"}


def run_one(worker_id: str, executor: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """认领并执行一个重型作业。

    executor 返回 {"state": succeeded|failed|unknown, "result": …, "error": …}。
    unknown 是合法结果：提交丢响应时不得改判成功或失败，也不释放 GPU。
    """
    claim = claim_next_heavy(worker_id)
    if not claim.get("claimed"):
        return {"executed": False, **claim}
    job = claim["claimed"]
    try:
        outcome = executor(job)
    except Exception as e:  # 执行器自身异常也要落账，不能静默
        jobs.fail(job["id"], worker_id,
                  {"code": "WORKER_EXCEPTION", "message": f"{type(e).__name__}: {e}"})
        return {"executed": True, "job_id": job["id"], "state": "failed",
                "error": {"code": "WORKER_EXCEPTION", "message": str(e)}}
    state = outcome.get("state")
    if state == "succeeded":
        done = jobs.complete(job["id"], worker_id, outcome.get("result") or {})
    elif state == "failed":
        done = jobs.fail(job["id"], worker_id,
                         outcome.get("error") or {"code": "EXEC_FAILED", "message": "执行失败"})
    elif state == "unknown":
        done = jobs.mark_unknown(job["id"], worker_id,
                                 reason=outcome.get("reason", "执行过程中结果未知"))
    else:
        raise jobs.JobRejected("VALIDATION_FAILED",
                               f"executor 返回非法状态 {state!r}；只接受 succeeded/failed/unknown")
    return {"executed": True, "job_id": job["id"], "state": done["state"], "job": done}


def run_until_idle(worker_id: str, executor: Callable[[Dict[str, Any]], Dict[str, Any]],
                   max_steps: int = 10) -> Dict[str, Any]:
    """连续执行直到没有可执行作业。返回真实执行序列，作为"串行"的证据。"""
    results: List[Dict[str, Any]] = []
    for _ in range(max_steps):
        r = run_one(worker_id, executor)
        if not r.get("executed"):
            return {"steps": results, "stop_reason": r.get("reason"),
                    "executed_count": len(results)}
        results.append({"job_id": r["job_id"], "state": r["state"]})
    return {"steps": results, "stop_reason": "MAX_STEPS", "executed_count": len(results)}


def queue_view(work_id: Optional[str] = None) -> Dict[str, Any]:
    rows = jobs.jobs_for_work(work_id) if work_id else st.all_rows("provider_jobs")
    heavy = [j for j in rows if j.get("heavy")]
    holder = jobs.gpu_holder()
    return {
        "work_id": work_id,
        "gpu_holder": {"job_id": holder.get("job_id"), "worker_id": holder.get("worker_id")}
                      if holder and holder.get("released_at") is None else None,
        "counts": {state: len([j for j in heavy if j["state"] == state])
                   for state in ("intent", "blocked", "queued", "leased", "succeeded",
                                 "failed", "unknown", "cancelled")},
        "heavy_jobs": [{"id": j["id"], "capability": j.get("capability"), "state": j["state"],
                        "attempts": j.get("attempts"), "created_at": j.get("created_at")}
                       for j in sorted(heavy, key=lambda r: r.get("created_at", ""))],
        "serialized": True,
        "note": "单 GPU 一次只允许一个有效重型作业；云路线（OmniHuman）不占本地 GPU",
    }
