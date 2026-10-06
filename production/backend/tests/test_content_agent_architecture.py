"""The active Deep Agents graph owns content; media remains user-gated."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from app import mcp, storage as st
from app.agents import framework, supervisor, tools
from app.agents.contracts import AgentWorkItem
from app.agents.roles import ROLES, SKILL_DIRS


def test_active_graph_contains_only_content_roles_and_skills():
    assert set(ROLES) == {"supervisor", "content", "quality"}
    assert set(SKILL_DIRS) == {"S01", "S02", "S03"}
    assert set(supervisor.STAGE_PLAN) == {"script", "content_review"}
    assert "submit_tts" in ROLES["supervisor"].tools
    assert "submit_avatar_video" in ROLES["supervisor"].tools
    assert "search_douyin" in ROLES["supervisor"].tools
    assert "submit_tts" not in ROLES["content"].tools
    assert "submit_avatar_video" not in ROLES["quality"].tools


def test_mcp_media_capabilities_are_supervisor_only():
    assert {"tts", "avatar_video", "avatar_video_cloud"} <= set(mcp.capabilities_for("supervisor"))
    assert mcp.capabilities_for("audio") == []
    assert mcp.capabilities_for("avatar") == []


@pytest.mark.parametrize("role", ["audio", "avatar"])
def test_removed_roles_are_rejected_by_new_work_item_contract(role):
    with pytest.raises(ValueError):
        AgentWorkItem(id="new_item", task_id="new_task", stage="content_review", role=role, objective="test")


@pytest.mark.parametrize("tool", ["submit_motion", "submit_lipsync", "submit_tts"])
def test_media_intent_tools_require_user_confirmation(tool, monkeypatch):
    work_id = st.new_id("w_media_intent")
    st.put("works", work_id, {"id": work_id, "route": "M2"})
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_FEN", "10000")
    with pytest.raises(tools.ToolDenied) as caught:
        tools.invoke("supervisor", tool, {"work_id": work_id, "route": "M2"},
                     {"work_id": work_id, "work_item_id": st.new_id("wi")})
    assert caught.value.code == "COST_CONFIRMATION_REQUIRED"
    assert st.all_rows("media_jobs", "work_id=?", [work_id]) == []


def test_mcp_billable_submit_requires_persisted_user_confirmation(monkeypatch):
    work_id = st.new_id("w_mcp_cost")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_FEN", "10000")
    with pytest.raises(mcp.McpDenied) as caught:
        mcp.submit(role="supervisor", capability="tts", args={"text": "测试"},
                   ctx={"work_id": work_id}, idempotency_key=st.new_id("idem"))
    assert caught.value.code == "COST_CONFIRMATION_REQUIRED"
    assert st.all_rows("provider_jobs", "work_id=?", [work_id]) == []


def test_media_tool_requires_persisted_user_confirmation_even_with_budget(monkeypatch):
    work_id = st.new_id("w_agent_media")
    st.put("works", work_id, {"id": work_id, "route": "OMNIHUMAN_1_5"})
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_FEN", "10000")
    with pytest.raises(tools.ToolDenied) as caught:
        tools.invoke("supervisor", "submit_avatar_video",
                     {"work_id": work_id, "route": "OMNIHUMAN_1_5"},
                     {"work_id": work_id, "work_item_id": st.new_id("wi")})
    assert caught.value.code == "COST_CONFIRMATION_REQUIRED"
    assert st.all_rows("media_jobs", "work_id=?", [work_id]) == []


def test_redfox_tool_requires_request_id_before_enqueuing(monkeypatch):
    work_id = st.new_id("w_agent_search")
    st.put("works", work_id, {"id": work_id, "brief": {"topic": "测试主题"}})
    with pytest.raises(tools.ToolDenied) as caught:
        tools.invoke("supervisor", "search_douyin", {"work_id": work_id, "query": "测试主题"},
                     {"work_id": work_id, "work_item_id": st.new_id("wi")})
    assert caught.value.code == "VALIDATION_FAILED"
    assert st.all_rows("content_jobs", "work_id=?", [work_id]) == []


def test_redfox_tool_uses_content_job_ledger(monkeypatch):
    from app.content import tasks

    calls = []
    monkeypatch.setattr(tasks, "submit_job", lambda *a, **kw: (
        calls.append((a, kw)) or {"id": "search_job", "state": "queued"}))
    monkeypatch.setattr(tasks, "get_scheduler", lambda: SimpleNamespace(start=lambda: None))
    out = tools.invoke("supervisor", "search_douyin",
                       {"work_id": "w_search", "query": "RAG", "request_id": "user-click-1"},
                       {"work_id": "w_search", "work_item_id": st.new_id("wi")})
    assert out["result"]["job_id"] == "search_job"
    assert calls == [(("w_search", "search", "user-click-1",
                     {"query": "RAG", "period_days": 7, "sort": "recommended",
                      "cursor": None, "refresh": False}), {})]


def test_framework_graph_does_not_register_media_subagents():
    graph = framework.build_agent()
    names = framework.available_tool_names(graph)
    assert "task" in names
    assert "submit_tts" in names
    assert "search_douyin" in names
