"""T00 · DeepAgents 离线最小编排与恢复（对应 05 计划 T00 与 AC-AG 结构约束）。

本文件验证的是业务侧编排与权限/门禁；框架运行时（真实委派、interrupt、checkpoint）
的验证见 test_agent_framework.py。所有用例离线运行，不调用任何收费服务。
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app import engine, storage as st
from app.agents import contracts as ac, results as res, supervisor, tools
from app.agents.models import build_model
from app.agents.roles import ROLES, load_skill_ref


def _work(topic: str = "生成一期关于 RAG 的 AI 科普视频") -> dict:
    w = st.get("works", "w_test")
    if w:
        return w
    w = {"id": "w_test", "name": "测试作品", "brief": {"topic": topic, "target_duration_ms": 60000},
         "identity_id": "av_base_b_v1", "voice_asset_id": "voice_minimax_warm_bestie_v1",
         "script_version_id": None, "route": "M2", "current_task_id": None, "version": 1}
    st.put("works", w["id"], w)
    return w


def _task() -> dict:
    t = st.get("tasks", "t_test")
    if t:
        return t
    t = {"id": "t_test", "work_id": "w_test", "mode": "mock", "stage": "topic", "state": "queued",
         "route": "M2", "route_confirmed": False, "pending_selection_ids": [], "stage_rows": []}
    st.put("tasks", t["id"], t, {"work_id": "w_test"})
    w = _work()
    w["current_task_id"] = t["id"]
    st.put("works", w["id"], w)
    return t


# --------------------------- 角色与工具权限 ---------------------------

def test_role_cannot_call_tool_outside_whitelist():
    ctx = {"work_item_id": "wi_x", "work_id": "w_test"}
    with pytest.raises(tools.ToolDenied) as e:
        tools.invoke("content", "submit_tts", {"work_id": "w_test"}, ctx)
    assert e.value.code == "TOOL_NOT_ALLOWED"


def test_malicious_tool_name_rejected():
    ctx = {"work_item_id": "wi_x"}
    for bad in ("__import__", "os.system", "rm -rf /", "../evil", "EXEC"):
        with pytest.raises(tools.ToolDenied) as e:
            tools.invoke("content", bad, {}, ctx)
        assert e.value.code == "TOOL_NOT_ALLOWED"


def test_path_traversal_rejected():
    ctx = {"work_item_id": "wi_x"}
    with pytest.raises(tools.ToolDenied) as e:
        tools.invoke("quality", "search_content_history", {"query": "../../etc/passwd"}, ctx)
    assert e.value.code == "PATH_NOT_ALLOWED"


def test_quality_cannot_rewrite_existing_check():
    _work()
    from app.mockdata import make_candidates

    batch = {"id": "b1", "work_id": "w_test", "candidates": [c.model_dump() for c in make_candidates("RAG")]}
    st.put("script_batches", "w_test", batch)
    sid = batch["candidates"][0]["id"]
    code = batch["candidates"][0]["quality_report"]["checks"][0]["code"]
    ctx = {"work_item_id": "wi_q", "work_id": "w_test"}
    with pytest.raises(tools.ToolDenied) as e:
        tools.invoke("quality", "submit_quality_report",
                     {"work_id": "w_test", "subject_id": sid,
                      "checks": [{"code": code, "outcome": "pass"}]}, ctx)
    assert e.value.code == "QUALITY_REPORT_TAMPERING"


def test_billable_route_blocked_without_budget():
    _work()
    ctx = {"work_item_id": "wi_a", "work_id": "w_test"}
    with pytest.raises(tools.ToolDenied) as e:
        tools.invoke("supervisor", "submit_avatar_video", {"work_id": "w_test", "route": "OMNIHUMAN_1_5"}, ctx)
    assert e.value.code == "COST_CONFIRMATION_REQUIRED"


def test_removed_editor_cannot_call_subtitle_tool():
    _work()
    ctx = {"work_item_id": "wi_e", "work_id": "w_test"}
    with pytest.raises(tools.ToolDenied) as e:
        tools.invoke("editor", "align_subtitles", {"work_id": "w_test"}, ctx)
    assert e.value.code == "ROLE_UNKNOWN"


def test_memory_candidate_is_proposed_only():
    ctx = {"work_item_id": "wi_m", "work_id": "w_test", "task_id": "t"}
    # Agent 只能提候选；直接写 approved 必须被拒
    with pytest.raises(tools.ToolDenied) as e:
        tools.invoke("content", "propose_memory",
                     {"namespace": "术语读法", "content_digest": "sha256:x", "state": "approved"}, ctx)
    assert e.value.code == "MEMORY_WRITE_DENIED"
    out = tools.invoke("content", "propose_memory",
                       {"namespace": "术语读法", "content_digest": "sha256:x"}, ctx)
    assert out["result"]["state"] == "proposed"


# --------------------------- 幂等与审计 ---------------------------

def test_side_effect_tool_is_idempotent():
    _work()
    ctx = {"work_item_id": "wi_idem", "work_id": "w_test"}
    a = tools.invoke("content", "register_script_candidates", {"work_id": "w_test", "topic": "RAG"}, ctx)
    b = tools.invoke("content", "register_script_candidates", {"work_id": "w_test", "topic": "RAG"}, ctx)
    assert a["replayed"] is False and b["replayed"] is True
    assert a["idempotency_key"] == b["idempotency_key"]


def test_long_result_survives_idempotent_replay(monkeypatch):
    """重放必须能取回**完整**结果，不能靠一条被截断的日志。

    写稿结果带上自修复留痕后超过 300 字，曾被截成非法 JSON：重放退化成空结果，
    产物引用全丢，整个工作项被判 `RESULT_INCOMPLETE` 失败——稿子明明写好了。
    """
    big = {"batch_id": "b_long", "note": "自修复留痕" * 200}
    monkeypatch.setitem(tools.TOOLS, "register_script_candidates", lambda a, c: dict(big))
    ctx = {"work_item_id": "wi_long", "work_id": "w_test"}
    a = tools.invoke("content", "register_script_candidates", {"work_id": "w_test"}, ctx)
    b = tools.invoke("content", "register_script_candidates", {"work_id": "w_test"}, ctx)
    assert a["replayed"] is False and b["replayed"] is True
    assert b["result"] == big, "重放拿回的结果必须与首次执行一致"


def test_missing_replay_payload_fails_loudly(monkeypatch):
    """载荷取不回来就明确报错，不许用空结果冒充已完成。"""
    ctx = {"work_item_id": "wi_badpayload", "work_id": "w_test"}
    args = {"work_id": "w_test"}
    idem = f"wi_badpayload:register_script_candidates:{tools.args_digest(args)}"
    st.put("agent_tool_audit", "audit_bad", {
        "id": "audit_bad", "work_item_id": "wi_badpayload", "role": "content",
        "tool": "register_script_candidates", "args_digest": tools.args_digest(args),
        "idempotency_key": idem, "outcome": "ok", "message": '{"batch_id": "b_trunc',
        "replayed": False, "created_at": "2026-09-25T00:00:00+00:00"}, {"work_item_id": "wi_badpayload"})
    with pytest.raises(tools.ToolDenied) as e:
        tools.invoke("content", "register_script_candidates", args, ctx)
    assert e.value.code == "REPLAY_PAYLOAD_MISSING"


def test_changed_input_digest_is_not_a_replay(monkeypatch):
    """输入变了（例如给作品补了参考资料）就不是重放，必须真的重执行。

    工作项按新输入摘要重建时 `attempt` 从 0 起算，键若只绑 attempt 就会与上一轮相撞：
    新输入根本没被执行，"补资料后重新出稿"返回的却是旧批次。
    """
    calls: list = []

    def _fake(a, c):
        calls.append(1)
        return {"batch_id": f"b{len(calls)}"}

    monkeypatch.setitem(tools.TOOLS, "register_script_candidates", _fake)
    args = {"work_id": "w_test"}
    a = tools.invoke("content", "register_script_candidates", args,
                     {"work_item_id": "wi_in", "work_id": "w_test",
                      "input_digest": "sha256:one", "attempt": 1})
    b = tools.invoke("content", "register_script_candidates", args,
                     {"work_item_id": "wi_in", "work_id": "w_test",
                      "input_digest": "sha256:two", "attempt": 1})
    assert a["replayed"] is False and b["replayed"] is False
    assert len(calls) == 2, "换输入必须真执行，不能命中上一轮的幂等键"


def test_same_input_same_attempt_still_replays(monkeypatch):
    """同输入同尝试仍然只执行一次——保护付费与外部副作用。"""
    calls: list = []

    def _fake(a, c):
        calls.append(1)
        return {"batch_id": "b_same"}

    monkeypatch.setitem(tools.TOOLS, "register_script_candidates", _fake)
    args = {"work_id": "w_test"}
    ctx = {"work_item_id": "wi_same", "work_id": "w_test",
           "input_digest": "sha256:one", "attempt": 1}
    tools.invoke("content", "register_script_candidates", args, dict(ctx))
    b = tools.invoke("content", "register_script_candidates", args, dict(ctx))
    assert b["replayed"] is True
    assert len(calls) == 1


# --------------------------- 结果校验 ---------------------------

def test_result_without_output_refs_rejected():
    item = ac.AgentWorkItem(id="wi1", task_id="t", stage="script", role="content", objective="x",
                            input_digest="sha256:a")
    r = ac.AgentResult(id="r1", work_item_id="wi1", input_digest="sha256:a", state="succeeded",
                       output_refs=[], result_digest="sha256:b")
    with pytest.raises(res.ResultRejected) as e:
        res.validate_result(r, item)
    assert e.value.code == "RESULT_INCOMPLETE"


def test_agent_cannot_forge_selection_in_result():
    item = ac.AgentWorkItem(id="wi1", task_id="t", stage="script", role="content", objective="x",
                            input_digest="sha256:a")
    # 把选择结果夹带进产物引用
    r = ac.AgentResult(id="r1", work_item_id="wi1", input_digest="sha256:a", state="succeeded",
                       output_refs=[{"kind": "selection", "id": "script_1"}], result_digest="d",
                       tool_calls=[], error=None)
    with pytest.raises(res.ResultRejected) as e:
        res.validate_result(r, item)
    assert e.value.code == "SELECTION_FORGERY"

    # 直接调用选择类工具
    r2 = ac.AgentResult(id="r2", work_item_id="wi1", input_digest="sha256:a", state="succeeded",
                        output_refs=[{"kind": "batch_id", "id": "b1"}], result_digest="d",
                        tool_calls=[{"tool": "select_script", "outcome": "ok"}], error=None)
    with pytest.raises(res.ResultRejected) as e2:
        res.validate_result(r2, item)
    assert e2.value.code == "SELECTION_FORGERY"


def test_hard_gate_cannot_be_offset_by_score():
    checks = [{"code": "FACT", "outcome": "fail", "hard_gate": True},
              {"code": "STYLE", "outcome": "pass", "hard_gate": False},
              {"code": "HOOK", "outcome": "pass", "hard_gate": False}]
    out = res.recompute_hard_gates(checks)
    assert out["passed"] is False and out["hard_failures"] == ["FACT"]


# --------------------------- Skill 绑定与角色 ---------------------------

def test_skill_refs_are_bound_to_real_files():
    ref = load_skill_ref("S02")
    assert ref is not None and ref.version == "0.2.0" and ref.digest.startswith("sha256:")


def test_each_role_has_own_tool_scope():
    assert "submit_tts" not in ROLES["content"].tools
    assert "submit_tts" in ROLES["supervisor"].tools
    assert "audio" not in ROLES and "avatar" not in ROLES
    assert "register_script_candidates" not in ROLES["quality"].tools


# --------------------------- 编排：选择门 ---------------------------

def test_orchestration_stops_at_script_selection_gate():
    work = _work()
    task = _task()
    task["stage"] = "topic"
    task["state"] = "queued"
    st.put("tasks", task["id"], task, {"work_id": "w_test"})

    asyncio.run(supervisor.advance("t_test"))

    t = st.get("tasks", "t_test")
    assert t["state"] == "waiting_selection"
    assert "sel_script" in t["pending_selection_ids"]
    # 三稿已审核：quality 结论存在且至少一稿有硬门禁失败
    qds = st.all_rows("quality_decisions")
    assert len(qds) >= 3
    # 质量通过不能代替采用
    assert (st.get("works", "w_test") or {}).get("script_version_id") is None


def test_work_items_are_persisted_and_resumable():
    task = st.get("tasks", "t_test")
    items = st.all_rows("agent_work_items", "task_id=?", ["t_test"])
    assert items, "工作项必须落库，才能跨进程恢复"
    done = [i for i in items if i["state"] == "succeeded"]
    assert done, "已完成的工作项应被记录"
    # 重新推进不应重复执行已完成项（幂等恢复）
    before = {i["id"]: i["state"] for i in items}
    asyncio.run(supervisor.advance("t_test"))
    after = {i["id"]: i["state"] for i in st.all_rows("agent_work_items", "task_id=?", ["t_test"])}
    for k in before:
        if before[k] == "succeeded":
            assert after[k] == "succeeded"


def test_media_stage_does_not_run_deepagents():
    """Selecting a script hands media work to APIs and workers."""
    work = _work()
    # 模拟用户已采用一稿（写入采用即视为用户决定）
    batch = st.get("script_batches", "w_test")
    good = next(c for c in batch["candidates"]
                if not [x for x in c["quality_report"]["checks"] if x["hard_gate"] and x["outcome"] != "pass"])
    work["script_version_id"] = good["id"]
    st.put("works", "w_test", work)

    task = st.get("tasks", "t_test")
    task["stage"] = "avatar_variant"
    task["state"] = "queued"
    task["pending_selection_ids"] = []
    st.put("tasks", task["id"], task, {"work_id": "w_test"})

    asyncio.run(supervisor.advance("t_test"))

    t = st.get("tasks", "t_test")
    assert t["state"] == "queued"
    assert t["pending_selection_ids"] == []
    assert not [i for i in st.all_rows("agent_work_items", "task_id=?", ["t_test"])
                if i["stage"] in ("avatar_variant", "tts", "talking_head")]


def test_talking_head_is_not_a_deepagents_stage():
    task = st.get("tasks", "t_test")
    task["stage"] = "talking_head"
    task["state"] = "queued"
    task["pending_selection_ids"] = []
    task["route_confirmed"] = False
    st.put("tasks", task["id"], task, {"work_id": "w_test"})

    asyncio.run(supervisor.advance("t_test"))
    t = st.get("tasks", "t_test")
    assert t["state"] == "queued"
    assert "talking_head" not in supervisor.STAGE_PLAN


def test_offline_model_is_deterministic_and_offline():
    m1 = build_model("content", {"topic": "RAG", "work_id": "w_test", "script_id": ""})
    m2 = build_model("content", {"topic": "RAG", "work_id": "w_test", "script_id": ""})
    seq1 = [m1.decide() for _ in range(4)]
    seq2 = [m2.decide() for _ in range(4)]
    names1 = [s["tool"] for s in seq1 if s]
    names2 = [s["tool"] for s in seq2 if s]
    assert names1 == names2, "同输入必须同输出"
    assert m1.model_ref()["backend"] == "deterministic"
