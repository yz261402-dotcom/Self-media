"""改稿次数与版本链：初稿后最多 3 次自动重写，且受预算约束。

每次改稿产生新版本并重审，旧版本保留但失效；超限即停止，
不重置计数、不换稿绕开上限、不用旧版本覆盖当前有效版本。
计数存在 script_batches 行内（沿用既有表，不新增 schema）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import storage as st
from .errors import ContentRejected


class RewriteLedger:
    MAX_REWRITES = 3

    def __init__(self, store=st):
        self._st = store

    def _batch(self, work_id: str) -> Dict[str, Any]:
        return self._st.get("script_batches", work_id) or {}

    def count(self, work_id: str) -> int:
        return int(self._batch(work_id).get("rewrite_count") or 0)

    def history(self, work_id: str) -> List[Dict[str, Any]]:
        return list(self._batch(work_id).get("rewrite_history") or [])

    def can_rewrite(self, work_id: str) -> bool:
        return self.count(work_id) < self.MAX_REWRITES

    def remaining(self, work_id: str) -> int:
        return max(0, self.MAX_REWRITES - self.count(work_id))

    def record(self, work_id: str, reason: str = "",
               candidate_id: Optional[str] = None) -> int:
        """登记一次改稿。超限抛 ContentRejected（改输入/换策略也无效）。"""
        batch = self._batch(work_id)
        if not batch:
            raise ContentRejected(f"作品 {work_id} 尚无稿件批次，不能登记改稿",
                                  code="BATCH_NOT_FOUND")
        used = self.count(work_id)
        if used >= self.MAX_REWRITES:
            raise ContentRejected(
                f"改稿次数已用尽（{used}/{self.MAX_REWRITES}）；"
                "需人工介入或新策略，不得换稿重置计数",
                code="REWRITE_LIMIT_EXCEEDED")
        batch["rewrite_count"] = used + 1
        batch.setdefault("rewrite_history", []).append(
            {"seq": used + 1, "reason": reason, "candidate_id": candidate_id})
        self._st.put("script_batches", work_id, batch, {"work_id": work_id})
        return batch["rewrite_count"]
