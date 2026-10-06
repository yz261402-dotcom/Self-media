"""GPT 文本写稿的 Chat Completions 协议映射；无网络、SDK、重试或模型替换。

max_completion_tokens 包含思考与最终文本。usage 总数原样归一，思考/缓存
明细仅另存，不重复计入总数。明确拒绝先返回结果供调用者结算，再报业务错误。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from ..errors import ContentRejected

PROTOCOL = "openai-chat-completions"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
ALLOWED_PARAMS = {"reasoning_effort"}
REASONING_EFFORTS = {"low", "medium", "high"}


def endpoint(base_url: str) -> str:
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    return base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")


def build_request(*, base_url: str, api_key: str, model: str, system: str, user: str,
                  max_tokens: int, extra: Optional[Dict[str, Any]] = None):
    bad = sorted(set(extra or {}) - ALLOWED_PARAMS)
    if bad:
        raise ContentRejected(f"GPT 适配器不支持参数：{'、'.join(bad)}", "PARAM_UNSUPPORTED")
    effort = (extra or {}).get("reasoning_effort", os.environ.get(
        "AVATAR_STUDIO_CONTENT_GPT_REASONING_EFFORT", "medium").strip())
    if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
        raise ContentRejected("GPT 写稿的 reasoning_effort 只支持 low、medium、high", "PARAM_UNSUPPORTED")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ContentRejected("GPT 写稿输出上限必须是正整数", "VALIDATION_FAILED")
    if not isinstance(model, str) or not model:
        raise ContentRejected("GPT 写稿模型名称不能为空", "VALIDATION_FAILED")
    sys_text = system if "JSON" in system.upper() else system + "\n只输出一个合法的 JSON 对象。"
    body = {"model": model, "messages": [{"role": "developer", "content": sys_text},
                                             {"role": "user", "content": user}],
            "max_completion_tokens": max_tokens, "response_format": {"type": "json_object"},
            "reasoning_effort": effort, "stream": False, "n": 1}
    headers = {"content-type": "application/json", "authorization": f"Bearer {api_key}"}
    return endpoint(base_url), headers, json.dumps(body, ensure_ascii=False).encode("utf-8")


def _token_count(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def parse_response(raw: bytes) -> Dict[str, Any]:
    """统一响应并保留可结算的拒绝；不存响应全文、拒绝原文或任何凭据。"""
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError
        refusal = message.get("refusal")
        if refusal is not None and not isinstance(refusal, str):
            raise ValueError
        refused = bool(refusal) or choice.get("finish_reason") == "content_filter"
        content = message.get("content")
        if content is None and refused:
            content = ""
        if not isinstance(content, str):
            raise ValueError
        model, request_id = data.get("model"), data.get("id")
        if model is not None and not isinstance(model, str):
            raise ValueError
        if request_id is not None and not isinstance(request_id, str):
            raise ValueError
        usage, normalized = data.get("usage"), None
        if usage is not None:
            if not isinstance(usage, dict):
                raise ValueError
            input_count = _token_count(usage.get("prompt_tokens"))
            output_count = _token_count(usage.get("completion_tokens"))
            if input_count is not None and output_count is not None:
                prompt_details = usage.get("prompt_tokens_details") or {}
                completion_details = usage.get("completion_tokens_details") or {}
                if not isinstance(prompt_details, dict) or not isinstance(completion_details, dict):
                    raise ValueError
                cache_write = _token_count(prompt_details.get("cache_write_tokens"))
                cache_creation = _token_count(prompt_details.get("cache_creation_tokens"))
                if cache_write is not None and cache_creation is not None and cache_write != cache_creation:
                    raise ValueError
                normalized = {"input_tokens": input_count, "output_tokens": output_count,
                    "cache_read_input_tokens": _token_count(prompt_details.get("cached_tokens")),
                    "cache_creation_input_tokens": cache_write if cache_write is not None else cache_creation,
                    "reasoning_tokens": _token_count(completion_details.get("reasoning_tokens"))}
        result = {"text": content, "model": model, "finish_reason": choice.get("finish_reason"),
                  "usage": normalized, "provider_request_id": request_id or ""}
        if refused:
            result["refusal"] = "模型拒绝了本次内容请求"
        return result
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError):
        raise ContentRejected("GPT 响应不是合法的 Chat Completions 结构", "MODEL_OUTPUT_INVALID") from None
