"""Agent 层契约。

字段唯一来源为 specs/0001-douyin-ai-studio/04-接口契约与数据字典.md §3.2 / §3.4，
本文件不另建同义枚举，状态复用 contracts.py 中的 TaskState / CheckOutcome / Mode。

设计红线（对应 10 文档）：
- AgentWorkItem 的 skill_refs 绑定 Skill 的 ID＋版本＋摘要，缺失或不匹配即阻断。
- AgentResult 不能仅凭长文本标记成功；succeeded 必须有 output_refs 与 result_digest。
- QualityDecision 不是批准令牌，不能改写硬门禁结论；MemoryCandidate 只创建候选。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ..contracts import CheckOutcome, Mode, StrictModel, TaskState, now_iso

RoleId = Literal["supervisor", "content", "quality"]
MemoryState = Literal["proposed", "approved", "rejected", "superseded"]


def digest(obj: Any) -> str:
    """稳定摘要：字典按键排序，保证同一内容跨进程一致。"""
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class SkillRef(StrictModel):
    skill_id: str
    version: str
    digest: str


class AgentWorkItem(StrictModel):
    """一个工作项 = 一个角色在一个阶段内的受控职责。"""
    id: str
    task_id: str
    stage: str
    role: RoleId
    objective: str
    depends_on_ids: List[str] = Field(default_factory=list)
    input_refs: List[Dict[str, Any]] = Field(default_factory=list)
    input_digest: str = ""
    output_schema_version: str = "1.0"
    skill_refs: List[SkillRef] = Field(default_factory=list)
    tool_policy_id: str = "default"
    namespace: str = ""
    checkpoint_ref: Optional[str] = None
    budget_policy_id: str = "bp_zero_v1"
    state: TaskState = "queued"
    mode: Mode = "mock"
    attempt: int = 0
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    last_error: Optional[Dict[str, Any]] = None


class AgentResult(StrictModel):
    """工作项产出。succeeded 必须带 output_refs 与 result_digest。"""
    id: str
    work_item_id: str
    input_digest: str
    state: TaskState
    output_refs: List[Dict[str, Any]] = Field(default_factory=list)
    evidence_asset_ids: List[str] = Field(default_factory=list)
    quality_decision_ids: List[str] = Field(default_factory=list)
    provider_run_ids: List[str] = Field(default_factory=list)
    model_run_id: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[Dict[str, Any]] = None
    result_digest: str = ""
    created_at: str = Field(default_factory=now_iso)


class PerformanceSegment(StrictModel):
    segment_id: str
    text: str
    emotion: str = "calm"
    pace: str = "normal"
    emphasis: List[str] = Field(default_factory=list)
    motion: str = "neutral"


class PerformancePlan(StrictModel):
    """意图层计划：策划写，声音/数字人/后期读，不得各自维护第二份。"""
    id: str
    work_id: str
    script_version_id: str
    script_digest: str
    identity_id: Optional[str] = None
    voice_asset_id: Optional[str] = None
    synthesis_profile: Dict[str, Any] = Field(default_factory=dict)
    tts_controls_digest: str = ""
    tts_controls_revision: int = 0
    segments: List[PerformanceSegment] = Field(default_factory=list)
    target_duration_ms: int = Field(default=0, ge=0)
    plan_digest: str = ""
    revision_of: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class QualityDecision(StrictModel):
    """质量结论。非批准令牌：业务层按 ruleset 复算硬门禁。"""
    id: str
    work_item_id: str
    subject_id: str
    subject_digest: str
    ruleset_version: str = "ruleset-2026.09.22-mock"
    report_ids: List[str] = Field(default_factory=list)
    recommendation: CheckOutcome = "not_run"
    issues: List[Dict[str, Any]] = Field(default_factory=list)
    model_run_id: Optional[str] = None
    decision_digest: str = ""
    created_at: str = Field(default_factory=now_iso)
    # 口播稿升级：独立语义审核。旧规则型结论默认 independent=False / review_kind="rules"，
    # 因此历史或空 checks 的结论永远不会被当成独立审核（见 content.contracts.is_independent_review）。
    independent: bool = False
    review_kind: Literal["rules", "semantic_single", "semantic_set"] = "rules"
    reviewer: Dict[str, Any] = Field(default_factory=dict)
    binding: Dict[str, Any] = Field(default_factory=dict)
    findings: List[Dict[str, Any]] = Field(default_factory=list)
    work_id: Optional[str] = None


class MemoryCandidate(StrictModel):
    """经验候选：只入候选库，审核合并前不参与跨任务检索。"""
    id: str
    task_id: str
    work_item_id: str
    scope: Literal["role", "project"] = "role"
    namespace: str
    content_asset_id: Optional[str] = None
    content_digest: str
    evidence_refs: List[str] = Field(default_factory=list)
    applicability: Dict[str, Any] = Field(default_factory=dict)
    supersedes_id: Optional[str] = None
    state: MemoryState = "proposed"
    review_decision_id: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class ToolCallRecord(StrictModel):
    """工具审计：谁、以什么角色、调了什么、参数摘要、是否命中幂等。"""
    id: str
    work_item_id: str
    role: RoleId
    tool: str
    args_digest: str
    idempotency_key: str
    outcome: Literal["ok", "denied", "error"]
    message: str = ""
    replayed: bool = False
    created_at: str = Field(default_factory=now_iso)
