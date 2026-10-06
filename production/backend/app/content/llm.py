"""DeepSeek 结构化生成客户端（真实 HTTP，可注入 transport 便于离线测试）。

凭据来自 `AVATAR_STUDIO_DEEPSEEK_API_KEY`（可由 AVATAR_STUDIO_SECRETS_FILE 注入）。
**缺凭据或模式不允许时抛 ContentBlocked，不返回任何伪装成模型输出的文本。**
真实调用成功才写 provider_evidence：配置项存在只算 unverified（与 config.check_provider 一致）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

from ..config import PROVIDERS, classify_provider_error, record_provider_evidence
from .errors import ContentBlocked, ContentRejected

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT_S = 60


def _spec():
    return PROVIDERS.get("deepseek")


def llm_status() -> Dict[str, Any]:
    """不发起请求地报告可用性。只给状态与是否配置，不含任何密钥片段。"""
    spec = _spec()
    key_env = spec.key_env if spec else "AVATAR_STUDIO_DEEPSEEK_API_KEY"
    configured = bool(os.environ.get(key_env))
    return {
        "provider": "deepseek",
        "model": os.environ.get(spec.model_env, spec.default_model) if spec else "",
        "configured": configured,
        "state": "unverified" if configured else "not_configured",
        "detail": ("已配置密钥，但未经真实调用实测" if configured
                   else f"未配置 {key_env}；不生成稿件"),
    }


def _urlopen(request: urllib.request.Request, timeout: int):
    return urllib.request.urlopen(request, timeout=timeout)


def deepseek_json(
    system: str,
    user: str,
    *,
    model: Optional[str] = None,
    temperature: float = 0.8,
    timeout: int = DEFAULT_TIMEOUT_S,
    transport: Optional[Callable[[urllib.request.Request, int], Any]] = None,
    record_evidence: bool = True,
) -> Dict[str, Any]:
    """调用 DeepSeek 聊天接口并以 JSON 对象模式解析。

    transport 可注入，用于离线测试：注入后不产生任何真实网络副作用。
    """
    spec = _spec()
    if spec is None:
        raise ContentBlocked("供应商 deepseek 不在受支持清单内", code="PROVIDER_UNSUPPORTED")
    api_key = os.environ.get(spec.key_env)
    if not api_key:
        raise ContentBlocked(
            f"未配置 {spec.key_env}，不能生成稿件；不返回伪装成模型输出的内容",
            code="PROVIDER_NOT_CONFIGURED", retryable=False,
        )
    base = (os.environ.get(spec.base_env) or DEFAULT_BASE_URL).rstrip("/")
    use_model = model or os.environ.get(spec.model_env) or spec.default_model
    payload = {
        "model": use_model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    opener = transport or _urlopen
    try:
        with opener(request, timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # 真实 HTTP 失败：按状态归因
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:
            detail = ""
        state = classify_provider_error(exc.code, "", detail)
        raise ContentBlocked(f"DeepSeek 调用失败：{state}", code="PROVIDER_CALL_FAILED",
                             detail=f"HTTP {exc.code}",
                             retryable=state in ("rate_limited", "network_error")) from None
    except Exception as exc:
        raise ContentBlocked(f"DeepSeek 调用异常：{type(exc).__name__}",
                             code="PROVIDER_CALL_FAILED", detail="未取得有效响应，未自动重试") from None

    try:
        content = json.loads(raw)["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, ValueError, TypeError):
        raise ContentRejected("模型输出不是合法 JSON 对象，不能作为稿件",
                              code="MODEL_OUTPUT_INVALID") from None
    if not isinstance(parsed, dict):
        raise ContentRejected("模型输出不是 JSON 对象", code="MODEL_OUTPUT_INVALID")

    if record_evidence and transport is None:
        # 只有真实成功才记证据：让 check_provider 从 unverified 变 ready
        try:
            record_provider_evidence(
                "deepseek", "chat", "succeeded",
                json.dumps({"model": use_model, "mode": "chat_json"},
                           ensure_ascii=False))
        except Exception:
            pass
    return parsed
