"""模型后端：离线确定性替身 + DeepSeek 适配器。

 T00 要求用确定性模型替身和受限工具构造总控及内容 subagents：
离线替身不发起任何网络请求，按角色剧本返回结构化工具调用，保证同输入同输出。

DeepSeek 适配器只在显式配置密钥时可用；未配置时抛 CAPABILITY_NOT_CONFIGURED，
绝不静默降级或伪造模型响应。默认额度 0，真实调用需先授权预算。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Callable, Dict, List, Optional

MODEL_BACKEND = os.environ.get("AVATAR_STUDIO_MODEL_BACKEND", "deterministic").strip().lower()

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


class CapabilityNotConfigured(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def new_run_id(role: str) -> str:
    return f"run_{role}_{uuid.uuid4().hex[:10]}"


def _try_langchain():
    """LangChain 可选依赖：未安装时框架运行时不可用，但离线替身仍可工作。"""
    try:
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        return BaseChatModel, AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, ChatGeneration, ChatResult
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 角色剧本：离线替身"决策"的唯一来源。每个角色只做自己职责内的工具调用。
# args 中的 {work_id} / {script_id} 等占位符由上下文填充。
# ---------------------------------------------------------------------------
ROLE_SCRIPTS: Dict[str, List[Dict[str, Any]]] = {
    "supervisor": [],  # 总控不直接生产，只委派；由编排器 DAG 驱动
    "content": [
        {"tool": "register_script_candidates", "args": {"work_id": "{work_id}", "topic": "{topic}"}},
    ],
    "quality": [
        {"tool": "submit_quality_report", "args": {"work_id": "{work_id}", "subject_id": "{script_id}"}},
    ],
}


class OfflineModel:
    """离线确定性替身。

    不实现 LangChain 接口也能被业务编排器使用；`as_chat_model()` 在 LangChain
    可用时返回可交给 DeepAgents 的 BaseChatModel 实例。
    """

    def __init__(self, role: str, context: Optional[Dict[str, Any]] = None,
                 script: Optional[List[Dict[str, Any]]] = None):
        self.role = role
        self.context = context or {}
        # 工作项可指定剧本；未指定时用角色默认剧本
        self.script = list(script if script is not None else ROLE_SCRIPTS.get(role, []))
        self.cursor = 0
        self.run_id = new_run_id(role)

    # -- 业务编排器使用的简单接口 --
    def decide(self) -> Optional[Dict[str, Any]]:
        """返回下一个拟执行的工具调用；剧本耗尽返回 None（表示本工作项决策结束）。"""
        if self.cursor >= len(self.script):
            return None
        step = self.script[self.cursor]
        self.cursor += 1
        return {"tool": step["tool"], "args": _fill(step["args"], self.context), "model_run_id": self.run_id}

    def model_ref(self) -> Dict[str, str]:
        return {"backend": "deterministic", "name": "offline-scripted-v1", "run_id": self.run_id}

    # -- 交给 DeepAgents / LangGraph 的形态 --
    def as_chat_model(self):
        pack = _try_langchain()
        if pack is None:
            raise CapabilityNotConfigured("未安装 langchain-core，无法构造框架可用的模型对象")
        BaseChatModel, AIMessage, BaseMessage, _, _, _, ChatGeneration, ChatResult = pack
        outer = self

        class _ScriptedChatModel(BaseChatModel):  # type: ignore[misc,valid-type]
            @property
            def _llm_type(self) -> str:
                return "offline-scripted-v1"

            def bind_tools(self, tools: List[Any], **kwargs: Any):
                """框架会为工具调用绑定 schema；离线替身按剧本出调用，无需真实绑定。

                基类默认实现抛 NotImplementedError，这里显式接受并返回自身。
                """
                return self

            def _generate(self, messages: List[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
                return ChatResult(generations=[ChatGeneration(message=self._next_message())])

            async def _agenerate(self, messages: List[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
                # 图表调度走异步路径；与同步版本共用同一剧本游标，保证确定性
                return ChatResult(generations=[ChatGeneration(message=self._next_message())])

            def _next_message(self):
                step = outer.decide()
                if step is None:
                    return AIMessage(content=json.dumps(
                        {"state": "succeeded", "note": "剧本执行完毕"}, ensure_ascii=False))
                return AIMessage(
                    content="",
                    tool_calls=[{
                        "name": step["tool"],
                        "args": step["args"],
                        "id": f"call_{outer.cursor}_{uuid.uuid4().hex[:8]}",
                    }],
                )

        return _ScriptedChatModel()


def _fill(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
            key = v[1:-1]
            out[k] = ctx.get(key, "")
        else:
            out[k] = v
    return out


class DeepSeekModel:
    """DeepSeek 官方文本决策模型（OpenAI 兼容接口）。

    未配置密钥时不可用：不允许用离线替身冒充真实模型能力。
    """

    def __init__(self, role: str, context: Optional[Dict[str, Any]] = None):
        self.role = role
        self.context = context or {}
        self.run_id = new_run_id(role)
        from ..config import PROVIDERS

        spec = PROVIDERS["deepseek"]
        self.api_key = (os.environ.get(spec.key_env) or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
        self.model = os.environ.get(spec.model_env) or os.environ.get("DEEPSEEK_MODEL") or spec.default_model
        self.base_url = (os.environ.get(spec.base_env) or os.environ.get("DEEPSEEK_BASE_URL")
                         or "https://api.deepseek.com").rstrip("/")
        if not self.api_key:
            raise CapabilityNotConfigured(f"未配置 {spec.key_env}；真实模型不可用")

    def model_ref(self) -> Dict[str, str]:
        return {"backend": "deepseek", "name": self.model, "run_id": self.run_id}

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        import httpx

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(timeout=60) as c:
                r = c.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
        except Exception as exc:
            raise CapabilityNotConfigured(f"DeepSeek 请求失败：{type(exc).__name__}，未自动重试") from None
        if r.status_code >= 400:
            raise CapabilityNotConfigured(f"DeepSeek 调用失败：HTTP {r.status_code}")
        try:
            content = r.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            return content
        except (KeyError, IndexError, TypeError, ValueError):
            raise CapabilityNotConfigured("DeepSeek 返回结构无效，未取得模型结果") from None

    def as_chat_model(self):
        pack = _try_langchain()
        if pack is None:
            raise CapabilityNotConfigured("未安装 langchain-core")
        try:
            from langchain_openai import ChatOpenAI
        except Exception as exc:
            raise CapabilityNotConfigured(f"缺少 langchain-openai：{exc}")
        return ChatOpenAI(model=self.model, api_key=self.api_key, base_url=self.base_url,
                          temperature=0.2, timeout=60, max_retries=0)


def build_model(role: str, context: Optional[Dict[str, Any]] = None, backend: Optional[str] = None,
                script: Optional[List[Dict[str, Any]]] = None):
    """按配置构造模型后端。默认 deterministic（离线、无网络、无费用）。"""
    b = (backend or MODEL_BACKEND).strip().lower()
    if b == "deepseek":
        return DeepSeekModel(role, context)
    if b == "deterministic":
        return OfflineModel(role, context, script=script)
    raise CapabilityNotConfigured("未知模型后端；请配置 deterministic 或 deepseek")


def model_fingerprint(role: str, context: Dict[str, Any]) -> str:
    raw = json.dumps({"role": role, "ctx": context}, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
