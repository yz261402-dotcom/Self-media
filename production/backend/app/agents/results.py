"""AgentResult 校验与硬门禁复算。

对应 04 §3.4：结果须通过输出 schema、对象摘要、依赖版本及工具审计核验；
subagent 不能仅返回长文本就把工作标记为 succeeded。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import contracts as ac

# Agent 结果中不得出现的语义：选择与审批是独立于 Agent 的决定
FORBIDDEN_RESULT_KEYS = {"selection", "selection_decision", "approve", "approval_state",
                         "confirmed", "adopted", "user_choice"}
FORBIDDEN_REF_KINDS = {"selection", "selection_decision", "approval", "adopted", "user_choice"}
FORBIDDEN_TOOL_NAMES = {"select_script", "adopt_script", "approve", "confirm_audio", "confirm_cost"}


class ResultRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_result(result: ac.AgentResult, work_item: ac.AgentWorkItem) -> None:
    """结构校验：
    - succeeded 必须有 output_refs 与非空 result_digest（不接受"只说完成了"）
    - 结果不得携带选择/审批类字段（Agent 不能替用户决定）
    - input_digest 必须与工作项一致（依赖版本不符即失效）
    """
    if result.state == "succeeded":
        if not result.output_refs:
            raise ResultRejected("RESULT_INCOMPLETE", "succeeded 结果必须带 output_refs，不接受仅文本声明完成")
        if not result.result_digest:
            raise ResultRejected("RESULT_INCOMPLETE", "succeeded 结果必须带 result_digest")
    if result.input_digest != work_item.input_digest:
        raise ResultRejected("STALE_RESULT", "结果输入摘要与工作项不一致，属于过期产物")
    blob = {k: v for k, v in result.model_dump().items() if k in FORBIDDEN_RESULT_KEYS}
    for k, v in blob.items():
        if v not in (None, [], {}, False):
            raise ResultRejected("SELECTION_FORGERY", f"Agent 结果不得包含选择与审批字段：{k}")
    for ref in result.output_refs:
        if not isinstance(ref, dict) or not ref.get("id"):
            raise ResultRejected("RESULT_INCOMPLETE", "output_refs 每项必须是带 id 的对象引用")
        if str(ref.get("kind", "")) in FORBIDDEN_REF_KINDS:
            raise ResultRejected("SELECTION_FORGERY", f"产物引用不得夹带选择与审批语义：{ref.get('kind')}")
    for call in result.tool_calls:
        name = str((call or {}).get("tool", ""))
        if name in FORBIDDEN_TOOL_NAMES:
            raise ResultRejected("SELECTION_FORGERY", f"Agent 不得调用选择与审批类工具：{name}")


def recompute_hard_gates(checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """业务层复算硬门禁：硬门禁失败不能被综合评分或 Agent 结论抵消。"""
    hard_fail = [c for c in checks if c.get("hard_gate") and c.get("outcome") != "pass"]
    soft_fail = [c for c in checks if not c.get("hard_gate") and c.get("outcome") != "pass"]
    return {
        "hard_failures": [c.get("code") for c in hard_fail],
        "soft_failures": [c.get("code") for c in soft_fail],
        "passed": not hard_fail,
        "note": "硬门禁不可被综合评分抵消；review/not_run 不等于 pass",
    }


def make_result(work_item: ac.AgentWorkItem, state: str, output_refs: List[Dict[str, Any]],
                tool_calls: List[Dict[str, Any]], model_run_id: Optional[str] = None,
                error: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, Any]] = None) -> ac.AgentResult:
    payload = {
        "work_item_id": work_item.id,
        "input_digest": work_item.input_digest,
        "state": state,
        "output_refs": output_refs,
        "tool_calls": tool_calls,
        "model_run_id": model_run_id,
        "error": error,
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if k in ac.AgentResult.model_fields})
    r = ac.AgentResult(id=ac.digest(payload)[:40].replace("sha256:", "res_"), **payload)
    r.result_digest = ac.digest({"work_item": work_item.id, "outputs": output_refs, "state": state})
    return r
