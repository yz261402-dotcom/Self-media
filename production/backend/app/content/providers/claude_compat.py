"""Claude relay 的 OpenAI 兼容 Chat Completions 协议。

部分 Claude 中转只放行 OpenAI 兼容入口，不接受 Anthropic Messages 请求。
这个适配器只发送通用的 system/user 消息和 ``max_tokens``，不携带 GPT 专用的
``response_format`` 或 ``reasoning_effort`` 参数。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..errors import ContentRejected
from . import openai

PROTOCOL = "openai-chat-completions-claude"


def endpoint(base_url: str) -> str:
    return openai.endpoint(base_url)


def build_request(*, base_url: str, api_key: str, model: str, system: str, user: str,
                  max_tokens: int, extra: Optional[Dict[str, Any]] = None):
    if extra:
        raise ContentRejected("Claude OpenAI 兼容适配器不支持额外请求参数", "PARAM_UNSUPPORTED")
    if not isinstance(model, str) or not model:
        raise ContentRejected("Claude 兼容模型名称不能为空", "VALIDATION_FAILED")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ContentRejected("Claude 兼容输出上限必须是正整数", "VALIDATION_FAILED")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"content-type": "application/json", "authorization": f"Bearer {api_key}"}
    return endpoint(base_url), headers, json.dumps(body, ensure_ascii=False).encode("utf-8")


def parse_response(raw: bytes) -> Dict[str, Any]:
    return openai.parse_response(raw)
