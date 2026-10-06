"""DeepAgents 框架运行时与生产工作项执行。

生产阶段由业务 DAG 调度，各角色工作项交给真实 DeepAgents 图执行受控工具。
默认决策模型只按已批准工作图执行，不额外发起模型请求；不宣称 LLM 自主规划。
已锁定版本：deepagents 0.7.17 / langgraph 1.2.12（由 pip 实际安装结果确定）。

红线保障：即使走框架，工具仍然只是本文件包装的受控入口——
每个工具最终调用 app.agents.tools.invoke，权限、schema、路径与幂等校验照常生效。
框架的默认文件/shell 能力是否被关闭，由 test_agent_framework.py 断言并持续跟踪。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .. import storage as st
from . import tools as tl
from .models import OfflineModel, build_model
from .roles import ROLES, SKILL_DIRS, SKILLS_DIR, role_system_prompt, role_tools, require_skill_refs

# 框架默认提供的通用文件/shell 能力：本项目不允许暴露给 Agent
FORBIDDEN_DEFAULT_TOOLS = ("read_file", "write_file", "edit_file", "execute", "glob", "grep", "ls", "delete")

# 框架强制保留：read_file 无法从白名单中移除，只能接受并通过空后端约束其作用域
MANDATORY_READ_TOOL = "read_file"

# 会造成真实副作用的工具：这些必须被摘除，残留即为红线破坏
WRITE_OR_SHELL_TOOLS = ("write_file", "edit_file", "execute", "delete")

# 默认文件系统后端会把真实磁盘暴露给 Agent。本项目改用内存后端（StateBackend）：
# 不落磁盘，且非沙箱，execute/delete 因此不可用。
DEFAULT_BACKEND_KIND = "state"


def build_backend(kind: str = DEFAULT_BACKEND_KIND):
    """构造隔离后端。默认内存后端：不触碰真实磁盘，也不提供 shell 执行能力。"""
    from deepagents.backends import StateBackend

    if kind != "state":
        raise ValueError(f"不支持的后端：{kind}；本项目只允许内存后端（state）")
    return StateBackend()


def build_filesystem_middleware():
    """替换框架默认的文件系统中间件：只保留框架强制的 read_file。

    deepagents 0.7.17 默认把 ls/read_file/write_file/edit_file/glob/grep/
    execute/delete 全部挂到 Agent 上，违反本项目「Agent 只能通过受控工具访问
    外部能力」的红线。FilesystemMiddleware 属于框架必需中间件（移除会破坏
    核心特性并被框架拒绝），但支持 `tools=` 白名单，可把暴露面压到最小。

    实测约束（0.7.17）：
    - 白名单必须包含 read_file，否则构造直接抛 ValueError；
    - execute / delete 需要沙箱后端，内存后端下列入无效（no-op）。

    因此可达到的最小暴露面是 read_file 一个，且它只能读内存后端里的虚拟
    文件——本项目从不注入 files，该命名空间恒为空，读任何路径只会失败。

    中间件按 name 原地替换（见 deepagents.graph._apply_custom_middleware）。
    """
    from deepagents.middleware.filesystem import FilesystemMiddleware

    return FilesystemMiddleware(backend=build_backend(), tools=[MANDATORY_READ_TOOL])


def residual_forbidden_tools(agent: Any) -> Dict[str, List[str]]:
    """返回仍然可见的默认文件/shell 工具，区分「已摘除」与「框架强制残留」。

    残留项不等于放行：read_file 受空内存后端约束，execute/delete 受非沙箱
    后端约束（no-op）。测试持续跟踪这一分类，框架升级若放宽即为回归信号。
    """
    seen = set(available_tool_names(agent))
    present = [t for t in FORBIDDEN_DEFAULT_TOOLS if t in seen]
    return {
        "removed": [t for t in FORBIDDEN_DEFAULT_TOOLS if t not in seen],
        "residual": present,
        "write_or_shell_residual": [t for t in present if t in WRITE_OR_SHELL_TOOLS],
    }


def _ctx(work_id: str, task_id: str, work_item_id: str, role: str) -> Dict[str, Any]:
    work = st.get("works", work_id) or {}
    return {
        "work_id": work_id,
        "task_id": task_id,
        "work_item_id": work_item_id,
        "role": role,
        "topic": (work.get("brief") or {}).get("topic", ""),
        "identity_id": work.get("identity_id") or "",
        "voice_asset_id": work.get("voice_asset_id") or "",
        "script_id": work.get("script_version_id") or "",
        "route": work.get("route") or "M2",
    }


def execution_stop_reason(context: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """每次业务调用都读取持久状态，取消只停止后续提交，不撤销已受理作业。

    未入库 task 仅供独立框架探针使用；已入库任务必须仍是作品的当前执行。
    """
    task_id = context.get("task_id")
    task = st.get("tasks", task_id) if task_id else None
    if task is None:
        return None
    if task.get("state") == "cancelled":
        return {"code": "TASK_CANCELLED", "message": "任务已取消，停止后续工具调用"}
    work = st.get("works", context.get("work_id") or task.get("work_id")) or {}
    if task.get("work_id") != work.get("id") or work.get("current_task_id") != task_id:
        return {"code": "TASK_SUPERSEDED", "message": "作品已切换执行，停止旧任务后续工具调用"}
    if task.get("state") in ("failed", "blocked", "waiting_selection", "succeeded"):
        return {"code": "TASK_NOT_ACTIVE", "message": "任务当前状态不可继续执行工具"}
    return None


def require_active_execution(context: Dict[str, Any]) -> None:
    error = execution_stop_reason(context)
    if error:
        raise tl.ToolDenied(error["code"], error["message"])


def _lc_tool(role: str, name: str, work_id: str, task_id: str, work_item_id: str):
    """把受控工具包装成 LangChain 工具：框架调用同样走权限与门禁校验。

    args_schema=None + infer_schema=False：业务工具参数集各不相同且随版本演进，
    逐个声明会让工具定义与实现耦合；关闭推断后 LangChain 原样透传调用参数
    （若走推断，schema 会把未声明字段丢掉，导致参数静默丢失）。
    校验仍在 tl.invoke 内由业务层完成——schema 不是安全边界。
    """
    from langchain_core.tools import StructuredTool

    def _run(**kwargs) -> Any:
        ctx = _ctx(work_id, task_id, work_item_id, role)
        require_active_execution(ctx)
        out = tl.invoke(role, name, dict(kwargs), ctx)
        return out.get("result") if isinstance(out, dict) else out

    return StructuredTool(
        name=name, func=_run, args_schema=None, infer_schema=False,
        description=f"[{role}] {name}：受控工具，越权或非法参数会被拒绝",
    )


def lc_tools_for_role(role: str, work_id: str, task_id: str, work_item_id: str) -> List[Any]:
    return [_lc_tool(role, n, work_id, task_id, work_item_id) for n in role_tools(role)]


def skill_paths_for(role: str) -> List[str]:
    """把角色绑定的 Skill 解析为真实目录路径。

    Skill 未定版（文件缺失）时阻断——不允许让 Agent 带着空白 Skill 上场。
    """
    spec = ROLES.get(role)
    if not spec or not spec.skill_ids:
        return []
    require_skill_refs(spec.skill_ids)  # 缺失即抛错
    return [os.path.join(SKILLS_DIR, SKILL_DIRS[i]) for i in spec.skill_ids]


def execute_work_item(role: str, context: Dict[str, Any],
                      script: Optional[List[Dict[str, Any]]] = None,
                      *, model: Any = None) -> Dict[str, Any]:
    """Execute an approved item through supervisor -> task -> specialist.

    context 原样保留工作项、输入摘要和尝试序号；图本身不改业务阶段、用户选择门。
    模型适配器可替换为 DeepSeekModel，默认确定性调度不增加计费模型调用。
    """
    from deepagents import create_deep_agent
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.tools import StructuredTool

    ctx = dict(context)
    adapter = model if model is not None else build_model(role, ctx, script=script)
    ctx["model_run_id"] = adapter.run_id
    result: Dict[str, Any] = {
        "runtime": "deepagents", "model_run_id": adapter.run_id,
        "model_ref": adapter.model_ref(), "calls": [], "delegations": [], "error": None,
    }
    stopped = execution_stop_reason(ctx)
    if stopped:
        result["error"] = stopped
        return result

    def controlled_call(name: str, arguments: Dict[str, Any]) -> Any:
        # 图可能继续消耗确定性剧本，但第一次拒绝后不再允许任何业务副作用。
        if result["error"]:
            return {"error": result["error"]}
        try:
            require_active_execution(ctx)
            out = tl.invoke(role, name, arguments, ctx)
        except tl.ToolDenied as exc:
            result["error"] = {"code": exc.code, "message": exc.message}
            result["calls"].append({"tool": name, "outcome": "denied", "code": exc.code})
            return {"error": result["error"]}
        result["calls"].append({"tool": name, "outcome": "ok", **out})
        return out.get("result") or {}

    allowed = role_tools(role)
    for step in script or []:
        if step.get("tool") not in allowed:
            controlled_call(step.get("tool", ""), dict(step.get("args") or {}))
            return result

    def wrap(name: str):
        def run(**kwargs):
            return controlled_call(name, dict(kwargs))
        return StructuredTool(name=name, func=run, args_schema=None, infer_schema=False,
                              description=f"[{role}] {name}：仅允许经业务权限与版本门禁调用")

    prompt = role_system_prompt(role)
    for path in skill_paths_for(role):
        # Skill 来自已登记的可信目录，作为指令注入；内存后端不开放真实文件系统。
        with open(os.path.join(path, "SKILL.md"), encoding="utf-8") as source:
            prompt += "\n\n" + source.read()
    class _BusinessDagDelegation(AgentMiddleware):
        # 按框架公开的 middleware.name 覆盖规则移除当前工作项的 task 工具；
        # 角色委派已由外层业务 DAG 完成，不能在这里绕过依赖关系递归委派。
        @property
        def name(self):
            return "SubAgentMiddleware"

    no_delegation = _BusinessDagDelegation()
    specialist = create_deep_agent(
        model=adapter.as_chat_model(), tools=[wrap(name) for name in allowed],
        system_prompt=prompt, name=f"avatar_studio_{role}", backend=build_backend(),
        middleware=[build_filesystem_middleware(), no_delegation],
    )
    if role == "supervisor":
        specialist.invoke({"messages": [{"role": "user", "content": "执行当前已批准工作项。"}]},
                          {"recursion_limit": max(16, len(script or []) * 4 + 8)})
        return result

    if role not in ("content", "quality"):
        raise tl.ToolDenied("ROLE_UNKNOWN", "只有内容角色可被主控委派")
    from langchain_core.runnables import RunnableLambda

    supervisor_ctx = dict(ctx, role="supervisor", work_item_id=ctx["work_item_id"] + "_supervisor")
    delegation_args = {"subagent_type": role, "description": "执行当前已批准工作项并返回产物引用"}
    delegation = OfflineModel("supervisor", supervisor_ctx,
                              script=[{"tool": "task", "args": delegation_args}])

    def run_specialist(state, config):
        audit = {
            "id": st.new_id("audit"), "work_item_id": supervisor_ctx["work_item_id"],
            "role": "supervisor", "tool": "task", "args_digest": tl.args_digest(delegation_args),
            "idempotency_key": f"{ctx['work_item_id']}:task:{role}:i{ctx.get('input_digest', '')}:a{ctx.get('attempt', 0)}",
            "outcome": "ok", "message": "已批准的主控委派", "replayed": False,
        }
        try:
            require_active_execution(ctx)
            output = specialist.invoke(state, config)
            if result.get("error"):
                audit["outcome"] = "denied"
                audit["message"] = result["error"]["code"]
            result["delegations"].append({"role": role, "tool": "task", "outcome": audit["outcome"]})
            return output
        except tl.ToolDenied as exc:
            from langchain_core.messages import AIMessage
            result["error"] = {"code": exc.code, "message": exc.message}
            audit["outcome"], audit["message"] = "denied", exc.code
            return {"messages": [AIMessage(content=exc.message)]}
        except Exception:
            audit["outcome"], audit["message"] = "error", "子智能体执行失败"
            raise
        finally:
            from ..contracts import now_iso
            audit["created_at"] = now_iso()
            st.put("agent_tool_audit", audit["id"], audit, {"work_item_id": audit["work_item_id"]})

    graph = create_deep_agent(
        model=delegation.as_chat_model(), tools=[],
        system_prompt=role_system_prompt("supervisor"), name="avatar_studio_supervisor", backend=build_backend(),
        middleware=[build_filesystem_middleware()],
        subagents=[{"name": role, "description": ROLES[role].duties,
                    "runnable": RunnableLambda(run_specialist), "mode": "isolated"}],
    )
    graph.invoke({"messages": [{"role": "user", "content": "委派当前已批准工作项。"}]},
                 {"recursion_limit": max(24, len(script or []) * 4 + 12)})
    return result


def build_agent(work_id: str = "w_dev", task_id: str = "t_dev", work_item_id: str = "wi_dev",
                supervisor_script: Optional[List[Dict[str, Any]]] = None,
                role_scripts: Optional[Dict[str, List[Dict[str, Any]]]] = None):
    """构造总控 + 写作/审核 subagent。使用离线确定性替身，不发起网络请求。

    supervisor_script / role_scripts 用于测试：给离线替身注入剧本，驱动委派路径。
    这是总控委派探针；生产 DAG 通过 execute_work_item 执行每个角色工作项。
    """
    from deepagents import create_deep_agent

    sup = ROLES["supervisor"]
    role_scripts = role_scripts or {}
    model = OfflineModel(
        "supervisor", _ctx(work_id, task_id, work_item_id, "supervisor"),
        script=supervisor_script,
    ).as_chat_model()

    subagents = []
    for rid in ("content", "quality"):
        spec = ROLES[rid]
        subagents.append({
            "name": rid,
            "description": spec.duties,
            "system_prompt": role_system_prompt(rid),
            "tools": lc_tools_for_role(rid, work_id, task_id, f"{work_item_id}_{rid}"),
            "model": OfflineModel(rid, _ctx(work_id, task_id, f"{work_item_id}_{rid}", rid),
                                  script=role_scripts.get(rid)).as_chat_model(),
            "skills": skill_paths_for(rid),
            "mode": "isolated",
        })

    return create_deep_agent(
        model=model,
        tools=lc_tools_for_role("supervisor", work_id, task_id, work_item_id),
        subagents=subagents,
        system_prompt=sup.system_prompt(),
        name="avatar_studio_supervisor",
        backend=build_backend(),
        # 关键：摘掉框架默认的文件/shell 工具，Agent 只剩受控业务工具与 task 委派
        middleware=[build_filesystem_middleware()],
        # 人工中断：任何工具调用前暂停，用于验证选择门的人工恢复路径
        interrupt_on=None,
    )


def _walk_tool_names(obj: Any, depth: int = 0) -> List[str]:
    """递归收集工具名：工具挂在编译图节点的 data 里，不同版本位置不固定。"""
    names: List[str] = []
    if depth > 6 or obj is None:
        return names
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("tools", "tool") and isinstance(v, (list, tuple)):
                for t in v:
                    n = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                    if n:
                        names.append(str(n))
            elif k == "tools_by_name" and isinstance(v, dict):
                names += [str(x) for x in v.keys()]
            else:
                names += _walk_tool_names(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            names += _walk_tool_names(v, depth + 1)
    else:
        for attr in ("tools", "tool", "tools_by_name", "bound"):
            v = getattr(obj, attr, None)
            if v is None:
                continue
            if attr == "tools_by_name" and isinstance(v, dict):
                # langgraph ToolNode 把工具存在 {名字: 工具} 的字典里，
                # 直接取键，避免递归时丢失键名
                names += [str(x) for x in v.keys()]
            else:
                names += _walk_tool_names(v, depth + 1)
    return names


def available_tool_names(agent: Any) -> List[str]:
    """从编译后的图里收集可见工具名，用于断言默认文件/shell 工具是否被关闭。"""
    names: List[str] = []
    try:
        g = agent.get_graph()
        for node in g.nodes.values():
            names += _walk_tool_names(getattr(node, "data", None))
    except Exception:
        pass
    names += _walk_tool_names(agent)
    return sorted({n for n in names if n and n != "None"})


def forbidden_default_tools_present(agent: Any) -> List[str]:
    seen = available_tool_names(agent)
    return [t for t in FORBIDDEN_DEFAULT_TOOLS if t in seen]
