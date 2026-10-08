"""只读识别当前候选批次的未确认审核，不查询供应商或更改历史账本。"""
from typing import Optional

from .. import jobs, storage as st
from . import tasks


def batch_id_for_job(job: dict, current: Optional[dict] = None) -> Optional[str]:
    if current is None:
        current = st.get("script_batches", job["work_id"]) or {}
    # 手改批次会保留来源 job_id 作留痕，但它已经是另一个候选集合。
    # 仅本任务生成的正式批次或预审快照可通过来源关联；手改的新 ID 不可借此匹配。
    generated_id = "cbatch_" + job["id"]
    owned_snapshot = current.get("id") == generated_id or str(current.get("id") or "").startswith(generated_id + "_draft_")
    if current.get("job_id") == job["id"] and owned_snapshot:
        return current.get("id")
    return ((job.get("result_refs") or {}).get("batch_id")
            or (job.get("payload") or {}).get("batch_id"))


def pending_review_call(work_id: str, batch: Optional[dict], revision: int) -> Optional[dict]:
    if not batch or batch.get("work_id") != work_id or batch.get("content_revision") != revision:
        return None
    parents = {j["id"]: j for j in tasks.list_jobs(work_id)
               if j["kind"] in ("generation", "repair", "review")
               and j.get("content_revision") == revision
               and batch_id_for_job(j, batch) == batch["id"]}
    calls = [c for c in st.all_rows("provider_jobs", "work_id=?", [work_id])
             if c.get("namespace") == jobs.CONTENT_NAMESPACE
             and c.get("parent_content_job_id") in parents
             and c.get("capability") == "chat" and c.get("step") == "review"]
    if not calls:
        return None
    latest = max(calls, key=lambda c: (c.get("created_at", ""), c["id"]))
    parent = parents[latest["parent_content_job_id"]]
    if latest["state"] not in ("unknown", "waiting_external") or parent["state"] not in ("unknown", "waiting_external"):
        return None
    return latest


def public_review_retry(work_id: str, batch: Optional[dict], revision: int) -> Optional[dict]:
    call = pending_review_call(work_id, batch, revision)
    if not call or call["state"] != "unknown" or call.get("remote_task_id"):
        return None
    return {"call_id": call["id"], "job_id": call["parent_content_job_id"],
            "batch_id": batch["id"], "reserved_fen": call.get("reserved_fen") or 0,
            "has_remote_handle": False}
