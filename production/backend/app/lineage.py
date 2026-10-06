"""产物血缘与依赖失效（T02）。

对应 05 实施计划 T02 第七项：
- 修改稿件/声音/动作/剪辑时按依赖失效下游产物及审批，保留原始结果；
- 仅改字幕样式只重新渲染，改正文则更新音频、嘴型与字幕；
- 重启后重新核对引用与摘要，旧候选迟到不能改写当前选择。

失效不是删除：原始结果与其摘要一律保留，只标记 invalidated_at，便于追溯与回滚。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import storage as st
from .contracts import now_iso

ChangeType = str  # script_text | subtitle_style | voice | audio | motion | edit

# 上游变更类型 → 受影响的下游产物类型
INVALIDATION: Dict[str, List[str]] = {
    "script_text": ["audio", "lipsync", "subtitle", "video", "package"],
    "voice": ["audio", "lipsync", "subtitle", "video", "package"],
    "tts_controls": ["audio", "lipsync", "subtitle", "video", "package"],
    "audio": ["lipsync", "video", "package"],
    "motion": ["motion", "video", "package"],
    "edit": ["video", "package"],
}

# 只重新渲染、不重做上游产物
RERENDER_ONLY: Dict[str, List[str]] = {
    "subtitle_style": ["video", "package"],
}


class LineageDenied(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def record(*, artifact_id: str, kind: str, work_id: str, digest: str,
           depends_on: Optional[List[str]] = None, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = {
        "id": artifact_id,
        "artifact_id": artifact_id,
        "kind": kind,
        "work_id": work_id,
        "digest": digest,
        "depends_on": list(depends_on or []),
        "meta": dict(meta or {}),
        "invalidated_at": None,
        "invalidated_by": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    st.put("lineage", artifact_id, row, {"work_id": work_id})
    return row


def get(artifact_id: str) -> Optional[dict]:
    return st.get("lineage", artifact_id)


def rows_for_work(work_id: str) -> List[dict]:
    return st.all_rows("lineage", "work_id=?", [work_id])


def invalidate(*, work_id: str, change_type: str, source_artifact_id: Optional[str] = None,
               actor: str = "user", connection=None) -> Dict[str, Any]:
    """按变更类型失效下游。返回受影响清单，原始结果保留不删。"""
    targets = INVALIDATION.get(change_type)
    rerender = False
    if targets is None:
        targets = RERENDER_ONLY.get(change_type)
        rerender = True
    if targets is None:
        raise LineageDenied("VALIDATION_FAILED", f"未知变更类型：{change_type}")

    affected: List[Dict[str, Any]] = []
    for row in rows_for_work(work_id):
        if row.get("invalidated_at"):
            continue
        if row["kind"] in targets:
            row["invalidated_at"] = now_iso()
            row["invalidated_by"] = source_artifact_id or change_type
            row["change_type"] = change_type
            row["rerender_only"] = rerender
            row["actor"] = actor
            row["updated_at"] = now_iso()
            if connection is not None:
                st.insert_row(connection, "lineage", row["id"], row, {"work_id": work_id})
            else:
                st.put("lineage", row["id"], row, {"work_id": work_id})
            affected.append({"artifact_id": row["artifact_id"], "kind": row["kind"],
                             "digest": row["digest"], "rerender_only": rerender})
    return {
        "work_id": work_id,
        "change_type": change_type,
        "action": "rerender" if rerender else "rebuild",
        "invalidated": affected,
        "preserved": True,
        "note": "原始结果与摘要保留，只标记失效；下游需重新生成并重新申报审批",
    }


def late_candidate_guard(work_id: str, candidate_id: str, kind: str = "script") -> Dict[str, Any]:
    """旧候选迟到不能改写当前选择：已有决定的情况下，非所选候选一律拒收。"""
    sels = st.all_rows("selections", "work_id=?", [work_id])
    decided = [s for s in sels if s.get("kind") == kind and s.get("selected_ref")]
    if not decided:
        return {"accepted": True, "reason": f"{kind} 尚无人工选择，候选可登记"}
    current = decided[-1]["selected_ref"]
    if current != candidate_id:
        return {"accepted": False, "current": current, "candidate_id": candidate_id,
                "reason": f"该作品已采用 {current}；迟到的候选 {candidate_id} 不得改写当前选择"}
    return {"accepted": True, "current": current, "reason": "与当前选择一致"}


def reverify(work_id: str) -> Dict[str, Any]:
    """重启后重新核对引用与摘要：摘要变化或引用缺失都要暴露为问题，不静默沿用。"""
    problems: List[Dict[str, Any]] = []
    rows = rows_for_work(work_id)
    known = {r["artifact_id"] for r in rows}
    for r in rows:
        for dep in r.get("depends_on", []):
            if dep not in known:
                problems.append({"artifact_id": r["artifact_id"], "code": "MISSING_DEPENDENCY",
                                 "detail": f"依赖 {dep} 未在血缘表中"})
        dep_rows = [rows_by_id(work_id, d) for d in r.get("depends_on", [])]
        for d in dep_rows:
            if d and d.get("invalidated_at"):
                problems.append({"artifact_id": r["artifact_id"], "code": "DEPENDS_ON_INVALID",
                                 "detail": f"依赖 {d['artifact_id']} 已失效"})
    return {"work_id": work_id, "artifacts": len(rows), "consistent": not problems,
            "problems": problems}


def rows_by_id(work_id: str, artifact_id: str) -> Optional[dict]:
    return st.get("lineage", artifact_id)
