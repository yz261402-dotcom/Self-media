"""生产工作项由真实 DeepAgents 图执行；调度离线，Provider 不触网。"""
from __future__ import annotations

import asyncio

import pytest

from app import storage as st
from app.agents import contracts as ac, framework as fw, supervisor as sup, tools as tl


@pytest.fixture
def context():
    wid, tid, iid = st.new_id("w_runtime"), st.new_id("task"), st.new_id("wi")
    st.put("works", wid, {"id": wid, "name": "框架执行测试", "brief": {"topic": "写作观察"}, "route": "M2"})
    return {"work_id": wid, "task_id": tid, "work_item_id": iid, "role": "content",
            "topic": "写作观察", "input_digest": "sha256:current-input", "attempt": 1}


SCRIPT = [{"tool": "register_script_candidates", "args": {"work_id": "{work_id}", "topic": "{topic}"}}]


def test_real_framework_executes_controlled_business_tool(context):
    out = fw.execute_work_item("content", context, SCRIPT)
    assert out["runtime"] == "deepagents"
    assert out["model_ref"]["backend"] == "deterministic"
    assert out["error"] is None
    assert len(out["calls"]) == 1
    assert out["calls"][0]["result"]["batch_id"] == st.get("script_batches", context["work_id"])["id"]
    audit = st.all_rows("agent_tool_audit", "work_item_id=?", [context["work_item_id"]])
    assert len(audit) == 1
    assert ":isha256:current-input:a1" in audit[0]["idempotency_key"]


def test_production_execution_is_supervisor_task_delegation(context, monkeypatch):
    import deepagents

    created = []
    original = deepagents.create_deep_agent

    def capture(*args, **kwargs):
        graph = original(*args, **kwargs)
        created.append((kwargs, graph))
        return graph

    monkeypatch.setattr(deepagents, "create_deep_agent", capture)
    out = fw.execute_work_item("content", context, SCRIPT)
    assert out["error"] is None
    assert len(created) == 2
    assert created[0][0]["name"] == "avatar_studio_content"
    assert created[1][0]["name"] == "avatar_studio_supervisor"
    assert "task" in fw.available_tool_names(created[1][1])
    assert "execute" not in fw.available_tool_names(created[1][1])
    assert out["delegations"] == [{"role": "content", "tool": "task", "outcome": "ok"}]
    delegation = st.all_rows("agent_tool_audit", "work_item_id=?", [context["work_item_id"] + "_supervisor"])
    assert len(delegation) == 1 and delegation[0]["tool"] == "task"
    assert ":isha256:current-input:a1" in delegation[0]["idempotency_key"]


def test_framework_replay_keeps_attempt_and_input_boundaries(context):
    first = fw.execute_work_item("content", context, SCRIPT)
    second = fw.execute_work_item("content", context, SCRIPT)
    third = fw.execute_work_item("content", dict(context, attempt=2), SCRIPT)
    fourth = fw.execute_work_item("content", dict(context, input_digest="sha256:new-input"), SCRIPT)
    assert second["calls"][0]["replayed"] is True
    assert second["calls"][0]["result"] == first["calls"][0]["result"]
    assert third["calls"][0]["result"]["batch_id"] != first["calls"][0]["result"]["batch_id"]
    assert fourth["calls"][0]["result"]["batch_id"] != first["calls"][0]["result"]["batch_id"]


def test_framework_domain_failure_stops_later_effects(context, monkeypatch):
    def denied(*args):
        raise tl.ToolDenied("SELECTION_REQUIRED", "请先选择稿件")
    monkeypatch.setitem(tl.TOOLS, "register_script_candidates", denied)
    script = [*SCRIPT, *SCRIPT]
    out = fw.execute_work_item("content", context, script)
    assert out["error"]["code"] == "SELECTION_REQUIRED"
    assert len(out["calls"]) == 1
    assert st.get("script_batches", context["work_id"]) is None


def test_framework_rejects_foreign_tool_before_any_effect(context):
    out = fw.execute_work_item("content", context, [{"tool": "submit_tts", "args": {"work_id": "{work_id}"}}])
    assert out["error"]["code"] == "TOOL_NOT_ALLOWED"
    assert st.all_rows("provider_jobs", "work_id=?", [context["work_id"]]) == []


def test_production_graph_has_no_filesystem_writes_or_recursive_delegation(context, monkeypatch):
    import deepagents
    original = deepagents.create_deep_agent
    graphs = []
    def create(*args, **kwargs):
        graph = original(*args, **kwargs)
        graphs.append(graph)
        return graph
    monkeypatch.setattr(deepagents, "create_deep_agent", create)
    fw.execute_work_item("content", context, SCRIPT)
    names = set(fw.available_tool_names(graphs[0]))
    assert "register_script_candidates" in names
    assert not names.intersection({"task", "execute", "write_file", "edit_file", "delete", "glob", "grep", "ls"})


def test_legacy_runtime_requires_explicit_setting(context, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_AGENT_RUNTIME", "legacy")
    def unavailable(*args, **kwargs):
        raise AssertionError("legacy must not call DeepAgents")
    monkeypatch.setattr(fw, "execute_work_item", unavailable)
    task, item = _task_item(context)
    result = asyncio.run(sup._run_work_item(task, st.get("works", context["work_id"]), item, SCRIPT))
    assert result.state == "succeeded"
    assert result.tool_calls[0]["runtime"] == "legacy"


def _task_item(context):
    task = {"id": context["task_id"], "work_id": context["work_id"], "mode": "mock", "route": "M2"}
    item = ac.AgentWorkItem(id=context["work_item_id"], task_id=context["task_id"], role="content",
                            stage="script", objective="写稿", input_digest=context["input_digest"],
                            namespace="test/runtime", attempt=context["attempt"], mode="mock")
    return task, item


def test_production_work_item_uses_deepagents_by_default(context, monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_AGENT_RUNTIME", raising=False)
    real = fw.execute_work_item
    calls = []
    def execute(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)
    monkeypatch.setattr(fw, "execute_work_item", execute)
    task, item = _task_item(context)
    result = asyncio.run(sup._run_work_item(task, st.get("works", context["work_id"]), item, SCRIPT))
    assert result.state == "succeeded"
    assert calls == [True]
    assert result.output_refs[0]["kind"] == "batch_id"
    assert result.tool_calls[0]["runtime"] == "deepagents"


def test_unknown_runtime_fails_without_legacy_fallback(context, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_AGENT_RUNTIME", "mistyped-runtime")
    task, item = _task_item(context)
    result = asyncio.run(sup._run_work_item(task, st.get("works", context["work_id"]), item, SCRIPT))
    assert result.state == "failed"
    assert result.error["code"] == "AGENT_RUNTIME_UNSUPPORTED"
    assert st.get("script_batches", context["work_id"]) is None


def test_missing_framework_fails_without_legacy_fallback(context, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_AGENT_RUNTIME", "deepagents")
    def unavailable(*args, **kwargs):
        raise ImportError("missing-framework-private-detail")
    monkeypatch.setattr(fw, "execute_work_item", unavailable)
    task, item = _task_item(context)
    result = asyncio.run(sup._run_work_item(task, st.get("works", context["work_id"]), item, SCRIPT))
    assert result.state == "failed"
    assert "private-detail" not in str(result.error)
    assert st.get("script_batches", context["work_id"]) is None


@pytest.mark.parametrize("runtime", ["deepagents", "legacy"])
@pytest.mark.parametrize("interruption", ["cancel", "replace"])
def test_interruption_between_quality_tools_stops_later_effects_and_task_transition(context, monkeypatch, runtime, interruption):
    """真实框架的第一个工具返回时取消/换任务，不能再写审核、推进选择门。"""
    monkeypatch.setenv("AVATAR_STUDIO_AGENT_RUNTIME", runtime)
    wid, tid = context["work_id"], context["task_id"]
    work = st.get("works", wid)
    work["current_task_id"] = tid
    st.put("works", wid, work)
    task = {"id": tid, "work_id": wid, "mode": "mock", "route": "M2",
            "stage": "content_review", "state": "queued", "pending_selection_ids": []}
    st.put("tasks", tid, task, {"work_id": wid})
    later_calls = []

    def interrupt(args, ctx):
        if interruption == "cancel":
            row = st.get("tasks", tid)
            row["state"] = "cancelled"
            st.put("tasks", tid, row, {"work_id": wid})
        else:
            row = st.get("works", wid)
            row["current_task_id"] = "replacement_task"
            st.put("works", wid, row)
        return {"matches": []}

    def later(args, ctx):
        later_calls.append("submitted")
        return {"quality_decision_id": "must_not_exist"}

    monkeypatch.setitem(tl.TOOLS, "search_content_history", interrupt)
    monkeypatch.setitem(tl.TOOLS, "submit_quality_report", later)
    asyncio.run(sup.advance(tid))
    assert later_calls == []
    persisted = st.get("tasks", tid)
    assert persisted["state"] == ("cancelled" if interruption == "cancel" else "running")
    assert persisted["pending_selection_ids"] == []
    results = st.all_rows("agent_results", "work_item_id=?", [f"wi_{tid}_content_review_review"])
    assert len(results) == 1
    assert results[0]["state"] == "cancelled"
    assert results[0]["error"]["code"] == ("TASK_CANCELLED" if interruption == "cancel" else "TASK_SUPERSEDED")


@pytest.mark.parametrize("runtime", ["deepagents", "legacy"])
def test_cancel_during_accepted_tool_preserves_receipt_without_retry(context, monkeypatch, runtime):
    monkeypatch.setenv("AVATAR_STUDIO_AGENT_RUNTIME", runtime)
    task, item = _task_item(context)
    task.update(state="running", stage="script")
    work = st.get("works", context["work_id"])
    work["current_task_id"] = task["id"]
    st.put("works", work["id"], work)
    st.put("tasks", task["id"], task, {"work_id": work["id"]})
    calls = []

    def accepted(args, ctx):
        calls.append("accepted")
        current = st.get("tasks", task["id"])
        current["state"] = "cancelled"
        st.put("tasks", task["id"], current, {"work_id": work["id"]})
        return {"job_id": "external_receipt"}

    monkeypatch.setitem(tl.TOOLS, "register_script_candidates", accepted)
    result = asyncio.run(sup._run_work_item(task, work, item, SCRIPT))
    assert calls == ["accepted"]
    assert result.state == "cancelled"
    assert {"kind": "job_id", "id": "external_receipt"} in result.output_refs
    assert result.tool_calls[0]["outcome"] == "ok"
    assert st.get("tasks", task["id"])["state"] == "cancelled"


def test_persisted_unbound_task_is_not_allowed_to_execute(context):
    task, _ = _task_item(context)
    task.update(state="running", stage="script")
    st.put("tasks", task["id"], task, {"work_id": context["work_id"]})
    out = fw.execute_work_item("content", context, SCRIPT)
    assert out["error"]["code"] == "TASK_SUPERSEDED"
    assert st.get("script_batches", context["work_id"]) is None


def test_cancelled_task_reentry_does_not_invoke_tools_or_rebuild_work_items(context, monkeypatch):
    task, _ = _task_item(context)
    task.update(state="cancelled", stage="script")
    work = st.get("works", context["work_id"])
    work["current_task_id"] = task["id"]
    st.put("works", work["id"], work)
    st.put("tasks", task["id"], task, {"work_id": work["id"]})
    monkeypatch.setitem(tl.TOOLS, "register_script_candidates", lambda *args: pytest.fail("cancelled task invoked a tool"))
    assert fw.execute_work_item("content", context, SCRIPT)["error"]["code"] == "TASK_CANCELLED"
    summary = asyncio.run(sup.run_stage(task, work, "script"))
    assert summary["executed"] == 0
    assert summary["failures"][0]["error"]["code"] == "TASK_CANCELLED"
    asyncio.run(sup.advance(task["id"]))
    assert st.all_rows("agent_work_items", "task_id=?", [task["id"]]) == []
    assert st.get("tasks", task["id"])["state"] == "cancelled"
