"""Anthropic Messages 协议适配：只负责请求/响应映射，不含业务门禁与重试。"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..errors import ContentRejected

PROTOCOL = "anthropic-messages"
API_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"
# 本适配器验证过的请求参数；其余一律拒绝，不假装兼容（尤其 temperature/response_format 等）。
ALLOWED_PARAMS = {"max_tokens", "system", "stop_sequences"}
JSON_INSTRUCTION = "只输出一个合法的 JSON 对象，不要输出任何其他文字，不要使用代码块。"


def endpoint(base_url: str) -> str:
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    return base + ("/messages" if base.endswith("/v1") else "/v1/messages")


def build_request(*, base_url: str, api_key: str, model: str, system: str, user: str, max_tokens: int,
                  extra: Optional[Dict[str, Any]] = None):
    bad = sorted(set(extra or {}) - ALLOWED_PARAMS)
    if bad:
        raise ContentRejected(f"Anthropic 适配器不支持参数：{'、'.join(bad)}", "PARAM_UNSUPPORTED")
    body = {"model": model, "max_tokens": int(max_tokens),
            "system": (system + "\n\n" + JSON_INSTRUCTION).strip(),
            "messages": [{"role": "user", "content": user}]}
    body.update(extra or {})
    headers = {"content-type": "application/json", "x-api-key": api_key, "anthropic-version": API_VERSION}
    return endpoint(base_url), headers, json.dumps(body, ensure_ascii=False).encode("utf-8")


def parse_response(raw: bytes) -> Dict[str, Any]:
    """归一成 {text, model, finish_reason, usage, provider_request_id}；缺字段留 None，不补零。"""
    try:
        data = json.loads(raw.decode("utf-8"))
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    except (ValueError, AttributeError, UnicodeDecodeError):
        raise ContentRejected("模型响应不是合法的 Messages 结构", "MODEL_OUTPUT_INVALID") from None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    norm_usage = None
    if usage and usage.get("input_tokens") is not None and usage.get("output_tokens") is not None:
        cached = int(usage.get("cache_creation_input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
        norm_usage = {"input_tokens": int(usage["input_tokens"]) + cached,
                      "output_tokens": int(usage["output_tokens"])}
    stop = data.get("stop_reason")
    finish = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}.get(stop, stop)
    return {"text": text, "model": data.get("model"), "finish_reason": finish, "usage": norm_usage,
            "provider_request_id": data.get("id") or ""}
