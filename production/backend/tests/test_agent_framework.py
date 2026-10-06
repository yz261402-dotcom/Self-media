"""T00 试验：DeepAgents 框架运行时验证。

验证的是框架委派与权限边界；生产 DAG 的真实框架工作项执行另见
test_deepagents_production_runtime.py（该套测试不会因依赖缺失而跳过）。

已锁定版本：deepagents 0.7.17 / langgraph 1.2.12。
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app import storage as st

fw = pytest.importorskip("app.agents.framework", reason="框架探针不可用（缺少 deepagents/langchain 依赖）")

try:
    import deepagents  # noqa: F401
    _HAS_DEEPAGENTS = True
except Exception:
    _HAS_DEEPAGENTS = False

pytestmark = pytest.mark.skipif(not _HAS_DEEPAGENTS, reason="未安装 deepagents，跳过框架运行时试验")


WORK_ID = "w_fw"
TASK_ID = "t_fw"


@pytest.fixture(autouse=True)
def _seed():
    st.put("works", WORK_ID, {
        "id": WORK_ID, "name": "框架试验",
        "brief": {"topic": "生成一期关于 RAG 的 AI 科普视频"},
        "identity_id": "av_base_b_v1", "voice_asset_id": "v_fw",
        "route": "M2", "script_version_id": None,
        "created_at": "2026-01-01T00:00:00Z",
    })
    yield
    for t in ("works", "script_batches", "agent_tool_audit"):
        try:
            st.delete(t, WORK_ID)
        except Exception:
            pass


def _run(coro):
    return asyncio.run(coro)


def _invoke(agent, text="开始", limit=25):
    return _run(agent.ainvoke({"messages": [{"role": "user", "content": text}]},
                              {"recursion_limit": limit}))


def _called_tools(result) -> list[str]:
    names = []
    for m in (result.get("messages", []) if isinstance(result, dict) else []):
        for c in getattr(m, "tool_calls", None) or []:
            names.append(c.get("name"))
    return names


# ---------------------------------------------------------------------------
# 构造与工具暴露面
# ---------------------------------------------------------------------------

def test_agent_builds_as_compiled_graph():
    agent = fw.build_agent(work_id=WORK_ID, task_id=TASK_ID)
    assert type(agent).__name__ == "CompiledStateGraph"


def test_default_file_and_shell_tools_are_removed():
    """框架默认会挂 ls/read_file/write_file/edit_file/glob/grep/execute/delete。

    会造成真实副作用的必须一个不剩。
    """
    agent = fw.build_agent(work_id=WORK_ID, task_id=TASK_ID)
    r = fw.residual_forbidden_tools(agent)
    assert r["write_or_shell_residual"] == [], f"写入/shell 工具未摘除：{r['write_or_shell_residual']}"
    # 8 个默认工具里至少摘掉 7 个；read_file 是框架强制保留项
    assert len(r["removed"]) >= 7, f"摘除数量不足：{r}"


def test_only_read_file_residual_and_it_is_constrained():
    """残留只能是 read_file，且必须跑在内存后端上（无真实磁盘可见）。"""
    agent = fw.build_agent(work_id=WORK_ID, task_id=TASK_ID)
    r = fw.residual_forbidden_tools(agent)
    assert r["residual"] == ["read_file"], f"残留项超出预期：{r['residual']}"
    # 后端为内存实现，不落磁盘、非沙箱（execute 因此不可用）
    assert type(fw.build_backend()).__name__ == "StateBackend"


def test_business_tools_and_task_delegation_are_bound():
    agent = fw.build_agent(work_id=WORK_ID, task_id=TASK_ID)
    names = fw.available_tool_names(agent)
    assert "task" in names, "缺少子 Agent 委派工具"
    for t in ("plan_work_items", "get_progress", "read_report"):
        assert t in names, f"总控业务工具未绑定：{t}"


def test_skill_binding_requires_real_skill_files():
    """Skill 未定版（文件缺失）时不允许让 Agent 带空白 Skill 上场。"""
    paths = fw.skill_paths_for("content")
    assert paths, "content 角色未绑定任何 Skill"
    for p in paths:
        assert os.path.exists(os.path.join(p, "SKILL.md")), f"Skill 文件缺失：{p}"


# ---------------------------------------------------------------------------
# 委派链路：框架 → task → 子 Agent → 受控工具 → 落库
# ---------------------------------------------------------------------------

def test_supervisor_delegates_to_subagent_and_persists_result():
    """证明框架路径真的驱动了受控工具，而不是只搭了个空壳。"""
    agent = fw.build_agent(
        work_id=WORK_ID, task_id=TASK_ID, work_item_id="wi_fw",
        supervisor_script=[{"tool": "task", "args": {
            "subagent_type": "content",
            "description": "写三个不同角度的候选稿",
        }}],
        role_scripts={"content": [
            {"tool": "register_script_candidates", "args": {"work_id": "{work_id}", "topic": "{topic}"}},
        ]},
    )
    result = _invoke(agent)
    assert "task" in _called_tools(result), "总控未发起委派"

    batch = st.get("script_batches", WORK_ID)
    assert batch, "子 Agent 未产出任何稿件"
    assert len(batch["candidates"]) == 3, f"三稿要求未满足：{len(batch['candidates'])}"


def test_delegated_subagent_still_bound_by_role_permissions():
    """换了执行框架，最小权限不能换：子 Agent 调白名单外工具必须被拒。"""
    from app.agents import tools as tl

    # content 角色没有 build_publish_package 权限
    with pytest.raises(tl.ToolDenied):
        tl.invoke("content", "build_publish_package", {"work_id": WORK_ID}, {"work_item_id": "wi_x"})


def test_framework_path_records_tool_audit():
    """框架调用同样进审计表，可追溯。"""
    agent = fw.build_agent(
        work_id=WORK_ID, task_id=TASK_ID, work_item_id="wi_fw",
        supervisor_script=[{"tool": "task", "args": {
            "subagent_type": "content", "description": "写稿",
        }}],
        role_scripts={"content": [
            {"tool": "register_script_candidates", "args": {"work_id": "{work_id}", "topic": "{topic}"}},
        ]},
    )
    _invoke(agent)
    # 审计按工作项索引；框架路径下子 Agent 的工作项带角色后缀
    rows = st.all_rows("agent_tool_audit", "work_item_id=?", ["wi_fw_content"])
    assert rows, "框架调用未留下审计记录"
    assert any(r.get("tool") == "register_script_candidates" for r in rows), \
        f"审计未记录受控工具调用：{[r.get('tool') for r in rows]}"


# ---------------------------------------------------------------------------
# 离线与确定性
# ---------------------------------------------------------------------------

def test_framework_build_makes_no_network_call():
    """构造全程离线：默认后端为 deterministic，未配置密钥不得静默联网。"""
    assert os.environ.get("AVATAR_STUDIO_MODEL_BACKEND") == "deterministic"
    from app.agents.models import DeepSeekModel, CapabilityNotConfigured

    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        with pytest.raises(CapabilityNotConfigured):
            DeepSeekModel("content")
    finally:
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved


def test_offline_model_is_deterministic_across_runs():
    """同输入同输出：两次独立运行的工具序列必须完全一致。"""
    from app.agents.models import OfflineModel

    def seq():
        m = OfflineModel("content", {"work_id": WORK_ID, "topic": "T"}, script=[
            {"tool": "register_script_candidates", "args": {"work_id": "{work_id}", "topic": "{topic}"}},
        ])
        out = []
        while True:
            d = m.decide()
            if d is None:
                break
            out.append((d["tool"], sorted(d["args"].items())))
        return out

    assert seq() == seq()
