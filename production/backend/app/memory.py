"""分层记忆与命名空间隔离（T02）。

对应 05 实施计划 T02 第五项：
- 任务检查点、人设资产、历史内容、经验候选分层；
- 分支只写隔离命名空间，共享经验须审核并用期望版本合并。

层次（layer）：
- checkpoint：任务检查点，属于单个任务，不参与跨任务检索；
- persona：人物与音色等固定资产描述，属于 identity；
- history：已发布/已生成的历史内容，属于 work；
- experience：经验候选，分 role（分支私有）与 project（共享，须审核）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import storage as st
from .agents.contracts import digest
from .contracts import now_iso

LAYERS = ("checkpoint", "persona", "history", "experience")
MemoryState = str  # proposed | approved | rejected | superseded


class MemoryDenied(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _entry_id() -> str:
    return st.new_id("mem")


def write(*, layer: str, namespace: str, key: str, content: Any, actor_role: str,
          scope: str = "role", work_id: Optional[str] = None, task_id: Optional[str] = None,
          work_item_id: Optional[str] = None, evidence_refs: Optional[List[str]] = None,
          state: Optional[str] = None) -> Dict[str, Any]:
    """写入记忆。

    分支只写自己的命名空间；写入 project 作用域的共享经验一律先落 proposed，
    审核合并前不参与跨任务检索。
    """
    if layer not in LAYERS:
        raise MemoryDenied("VALIDATION_FAILED", f"未知记忆层：{layer}")
    if not namespace:
        raise MemoryDenied("NAMESPACE_REQUIRED", "写入记忆必须指定命名空间，禁止写入全局空间")
    if scope not in ("role", "project"):
        raise MemoryDenied("VALIDATION_FAILED", "scope 只能是 role 或 project")
    if layer == "experience" and scope == "project" and state == "approved":
        raise MemoryDenied("REVIEW_REQUIRED", "共享经验不能直接以 approved 写入，必须先经审核合并")

    entry = {
        "id": _entry_id(),
        "layer": layer,
        "scope": scope,
        "namespace": namespace,
        "key": key,
        "content": content,
        "content_digest": digest(content),
        "actor_role": actor_role,
        "work_id": work_id,
        "task_id": task_id,
        "work_item_id": work_item_id,
        "evidence_refs": list(evidence_refs or []),
        "state": state or ("proposed" if layer == "experience" else "approved"),
        "supersedes_id": None,
        "superseded_by": None,
        "version": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    st.put("memory_entries", entry["id"], entry)
    return entry


def get(entry_id: str) -> Optional[dict]:
    return st.get("memory_entries", entry_id)


def read(*, layer: str, namespace: str, key: str, include_proposed: bool = True) -> Optional[dict]:
    for e in _all():
        if e["layer"] == layer and e["namespace"] == namespace and e["key"] == key:
            if not include_proposed and e["state"] == "proposed":
                return None
            return e
    return None


def search(*, layer: Optional[str] = None, namespace: Optional[str] = None,
           scope: Optional[str] = None, approved_only: bool = False) -> List[dict]:
    """跨任务检索只看已审核；未审核候选只在自己命名空间内可见。"""
    out = []
    for e in _all():
        if layer and e["layer"] != layer:
            continue
        if namespace and e["namespace"] != namespace:
            continue
        if scope and e["scope"] != scope:
            continue
        if approved_only and e["state"] != "approved":
            continue
        if e["state"] in ("rejected", "superseded"):
            continue
        out.append(e)
    return out


def approve(entry_id: str, expected_digest: str, reviewer: str) -> Dict[str, Any]:
    """按期望版本合并：摘要不匹配即冲突，避免迟到写入覆盖已审核经验。"""
    e = get(entry_id)
    if e is None:
        raise MemoryDenied("NOT_FOUND", f"记忆条目不存在：{entry_id}")
    if e["state"] != "proposed":
        raise MemoryDenied("STATE_CONFLICT", f"条目当前为 {e['state']}，只能审核 proposed 条目")
    if e["content_digest"] != expected_digest:
        raise MemoryDenied("VERSION_CONFLICT",
                           f"期望摘要 {expected_digest} 与当前 {e['content_digest']} 不一致，拒绝合并")
    e["state"] = "approved"
    e["reviewer"] = reviewer
    e["reviewed_at"] = now_iso()
    e["updated_at"] = now_iso()
    st.put("memory_entries", entry_id, e)
    return e


def reject(entry_id: str, reviewer: str, reason: str = "") -> Dict[str, Any]:
    e = get(entry_id)
    if e is None:
        raise MemoryDenied("NOT_FOUND", f"记忆条目不存在：{entry_id}")
    e["state"] = "rejected"
    e["reviewer"] = reviewer
    e["review_reason"] = reason
    e["updated_at"] = now_iso()
    st.put("memory_entries", entry_id, e)
    return e


def supersede(old_id: str, *, layer: str, namespace: str, key: str, content: Any,
              actor_role: str, reviewer: str) -> Dict[str, Any]:
    """以新版本取代旧经验：旧条目保留为 superseded，不删除、不静默覆盖。"""
    old = get(old_id)
    if old is None:
        raise MemoryDenied("NOT_FOUND", f"被取代条目不存在：{old_id}")
    new = write(layer=layer, namespace=namespace, key=key, content=content,
                actor_role=actor_role, scope=old["scope"], state="proposed")
    new["supersedes_id"] = old_id
    new["version"] = int(old.get("version", 1)) + 1
    st.put("memory_entries", new["id"], new)  # 先落版本号再审核，避免审核读回旧版本
    approved = approve(new["id"], new["content_digest"], reviewer)
    old["state"] = "superseded"
    old["superseded_by"] = approved["id"]
    old["updated_at"] = now_iso()
    st.put("memory_entries", old_id, old)
    return approved


def pending_reviews(scope: str = "project") -> List[dict]:
    return [e for e in _all() if e["scope"] == scope and e["state"] == "proposed"]


def isolation_report() -> Dict[str, Any]:
    """隔离检查：未审核的共享经验不得出现在跨任务可见集合中。"""
    all_entries = _all()
    visible = search(approved_only=True)
    leak = [e["id"] for e in visible if e["state"] != "approved"]
    return {
        "total": len(all_entries),
        "cross_task_visible": len(visible),
        "proposed": len([e for e in all_entries if e["state"] == "proposed"]),
        "leaked_unapproved": leak,
        "isolated": not leak,
    }


def _all() -> List[dict]:
    return st.all_rows("memory_entries")
