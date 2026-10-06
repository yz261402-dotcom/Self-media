"""T01 契约测试：未知字段与非法枚举拒绝、选择与审批分离、版本摘要绑定。

对应 05 实施计划 T01 第二、三项：
- 实现各契约；人工选择独立于 ReviewDecision；身份、声音、模型、Skill 和规则绑定版本/摘要；
- 按 04 冻结 work_id、route、route_selection_id、pending_selection_ids、input_snapshot_digest。
"""
from __future__ import annotations

import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.contracts import (  # noqa: E402
    BindingSet,
    ReviewDecision,
    ScriptVersion,
    SelectionDecision,
    SelectionOption,
    SelectionRequest,
    Task,
    Work,
    WorkBrief,
)
from app.agents import contracts as ac  # noqa: E402
from app.agents import results as rs  # noqa: E402

WORK_ID = "work_test_fixed_0001"


def _work(**over):
    base = {"id": WORK_ID, "name": "测试作品", "brief": WorkBrief()}
    base.update(over)
    return Work(**base)


def _task(**over):
    base = {"id": "task_1", "work_id": WORK_ID}
    base.update(over)
    return Task(**base)


# ---------- 未知字段一律拒绝 ----------

@pytest.mark.parametrize("factory", [
    lambda: _work(extra_field="x"),
    lambda: _task(surprise=1),
    lambda: WorkBrief(tone="casual"),
    lambda: ScriptVersion(id="s", angle="a", title="t", description="d", text="x",
                          duration_estimate_ms=1000, word_count=10),
])
def test_unknown_field_is_rejected(factory):
    with pytest.raises(ValidationError):
        factory()


def test_agent_contracts_reject_unknown_field():
    with pytest.raises(ValidationError):
        ac.AgentWorkItem(id="wi", task_id="t", stage="script", role="content",
                         objective="写稿", allow_all_tools=True)
    with pytest.raises(ValidationError):
        ac.PerformancePlan(id="p", work_id=WORK_ID, script_version_id="sv1",
                           script_digest="sha256:x", auto_select=True)


def test_selection_request_rejects_unknown_field():
    with pytest.raises(ValidationError):
        SelectionRequest(id="selreq_1", work_id=WORK_ID, kind="script",
                         prompt="请选一稿", decided_by="supervisor")


# ---------- 非法枚举拒绝 ----------

@pytest.mark.parametrize("bad", ["M3", "OMNIHUMAN", "m2", ""])
def test_invalid_route_rejected(bad):
    with pytest.raises(ValidationError):
        _work(route=bad)


@pytest.mark.parametrize("bad", ["done", "SUCCESS", "waiting", ""])
def test_invalid_task_state_rejected(bad):
    with pytest.raises(ValidationError):
        _task(state=bad)


@pytest.mark.parametrize("bad", ["live", "PRODUCTION", ""])
def test_invalid_mode_rejected(bad):
    with pytest.raises(ValidationError):
        _task(mode=bad)


def test_invalid_role_rejected():
    with pytest.raises(ValidationError):
        ac.AgentWorkItem(id="wi", task_id="t", stage="script", role="admin", objective="x")


def test_invalid_check_outcome_rejected():
    with pytest.raises(ValidationError):
        ReviewDecision(id="rd", work_id=WORK_ID, subject_id="sv1",
                       ruleset_version="r1", outcome="accepted")


# ---------- 选择与审批必须分离 ----------

def test_selection_decision_actor_can_only_be_user():
    d = SelectionDecision(id="d1", request_id="req1", work_id=WORK_ID, kind="script",
                          selected_option_id="opt1", selected_ref="sv1")
    assert d.is_user_decision() is True
    with pytest.raises(ValidationError):
        SelectionDecision(id="d2", request_id="req1", work_id=WORK_ID, kind="script",
                          selected_option_id="opt1", selected_ref="sv1", actor="agent")
    with pytest.raises(ValidationError):
        SelectionDecision(id="d3", request_id="req1", work_id=WORK_ID, kind="script",
                          selected_option_id="opt1", selected_ref="sv1", actor="quality")


def test_review_decision_carries_no_selection_semantics():
    rd = ReviewDecision(id="rd", work_id=WORK_ID, subject_id="sv1",
                        ruleset_version="r1", outcome="pass")
    dumped = rd.model_dump()
    assert not any(k.startswith("selected_") for k in dumped)
    assert "selection" not in dumped


def test_quality_pass_cannot_construct_a_selection():
    """审批通过不能顶替选择：ReviewDecision 的字段不足以构造 SelectionDecision。"""
    rd = ReviewDecision(id="rd", work_id=WORK_ID, subject_id="sv1",
                        ruleset_version="r1", outcome="pass")
    with pytest.raises(ValidationError):
        SelectionDecision(**rd.model_dump())
    assert "selected_option_id" not in ReviewDecision.model_fields


def test_two_models_are_not_in_the_same_hierarchy():
    assert not issubclass(SelectionDecision, ReviewDecision)
    assert not issubclass(ReviewDecision, SelectionDecision)


def test_selection_request_lists_options_and_pending_default():
    req = SelectionRequest(
        id="req1", work_id=WORK_ID, kind="script", prompt="请选一稿",
        options=[SelectionOption(option_id="opt1", kind="script", ref_id="sv1", label="角度一"),
                 SelectionOption(option_id="opt2", kind="script", ref_id="sv2", label="角度二")],
    )
    assert req.state == "pending"
    assert req.option_ids() == ["opt1", "opt2"]


# ---------- 冻结字段 ----------

def test_route_selection_and_snapshot_are_frozen_fields():
    t = _task(route_selection_id="sel_route_1", input_snapshot_digest="sha256:abc")
    assert t.route_selection_id == "sel_route_1"
    assert t.input_snapshot_digest == "sha256:abc"


def test_pending_selection_ids_default_empty():
    assert _task().pending_selection_ids == []


def test_work_id_is_required_and_not_silent():
    with pytest.raises(ValidationError):
        Work(name="无 ID 作品", brief=WorkBrief())


# ---------- 绑定版本与摘要 ----------

def test_binding_set_flags_missing_digest():
    b = BindingSet(identity_id="av_base_b_v1", voice_asset_id="v1", ruleset_version="r1")
    assert b.missing() == ["identity", "voice"]


def test_binding_set_flags_missing_ruleset_and_skill():
    b = BindingSet(identity_id="av_base_b_v1", identity_digest="sha256:i",
                   skill_refs=[{"skill_id": "S02", "version": "0.1.0"}])
    assert "ruleset" in b.missing()
    assert "skill:S02" in b.missing()


def test_complete_binding_has_no_gap():
    b = BindingSet(identity_id="av_base_b_v1", identity_digest="sha256:i",
                   voice_asset_id="v1", voice_digest="sha256:v",
                   model_id="m1", model_digest="sha256:m", ruleset_version="r1",
                   skill_refs=[{"skill_id": "S02", "version": "0.1.0", "digest": "sha256:s"}])
    assert b.missing() == []


def test_work_item_skill_refs_must_carry_digest():
    wi = ac.AgentWorkItem(id="wi", task_id="t", stage="script", role="content", objective="写稿",
                          skill_refs=[ac.SkillRef(skill_id="S02", version="0.1.0", digest="sha256:s")])
    assert wi.skill_refs[0].digest.startswith("sha256:")
    with pytest.raises(ValidationError):
        ac.SkillRef(skill_id="S02", version="0.1.0")


# ---------- 结果门禁 ----------

def test_succeeded_result_requires_output_refs_and_digest():
    wi = ac.AgentWorkItem(id="wi", task_id="t", stage="script", role="content", objective="写稿",
                          input_digest="sha256:in")
    r = ac.AgentResult(id="r1", work_item_id="wi", input_digest="sha256:in", state="succeeded")
    with pytest.raises(rs.ResultRejected):
        rs.validate_result(r, wi)


def test_agent_result_cannot_carry_selection_field():
    wi = ac.AgentWorkItem(id="wi", task_id="t", stage="script", role="content", objective="写稿",
                          input_digest="sha256:in")
    with pytest.raises(ValidationError):
        ac.AgentResult(id="r2", work_item_id="wi", input_digest="sha256:in",
                       state="succeeded", selection="sv1")


def test_route_confirmation_freezes_a_user_selection():
    """业务路径证据：确认路线会落一条 actor=user 的决定，并冻结到任务的 route_selection_id。"""
    from fastapi.testclient import TestClient

    from app import storage as st
    from app.main import app

    client = TestClient(app)
    w = client.post("/api/v1/works", json={"name": "T01 冻结验证", "topic": "冻结字段验证"}).json()
    r = client.post(f"/api/v1/works/{w['id']}/route", json={"route": "M2"})
    assert r.status_code == 200, r.text

    rows = st.all_rows("selections", "work_id=?", [w["id"]])
    decisions = [x for x in rows if x.get("kind") == "route" and "selected_ref" in x]
    assert decisions, "路线确认必须留下一条选择决定"
    assert all(d.get("actor") == "user" for d in decisions)

    task = client.post("/api/v1/tasks", json={"work_id": w["id"]}).json()
    assert task.get("route_selection_id") in {d["id"] for d in decisions}
    assert task.get("input_snapshot_digest", "").startswith("sha256:")


def test_task_without_route_confirmation_stays_blocked():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    w = client.post("/api/v1/works", json={"name": "T01 未确认预算", "topic": "未确认预算"}).json()
    t = client.post("/api/v1/tasks", json={"work_id": w["id"]}).json()
    assert t["state"] == "blocked"
    assert t["block_reason"] == "COST_CONFIRMATION_REQUIRED"
    assert t.get("route_selection_id") is None


def test_hard_gate_failure_is_not_offset_by_scores():
    checks = [
        {"code": "IDENTITY_DRIFT", "outcome": "fail", "hard_gate": True},
        {"code": "READABILITY", "outcome": "pass", "hard_gate": False},
        {"code": "PACE", "outcome": "pass", "hard_gate": False},
    ]
    out = rs.recompute_hard_gates(checks)
    assert out["passed"] is False
    assert out["hard_failures"] == ["IDENTITY_DRIFT"]
    assert out["soft_failures"] == []
