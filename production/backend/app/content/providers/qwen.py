"""通义千问（百炼 OpenAI 兼容模式）适配：只负责请求/响应映射，不含业务门禁与重试。"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..errors import ContentRejected

PROTOCOL = "openai-chat-compatible"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ALLOWED_PARAMS = {"max_tokens", "response_format", "enable_thinking", "stop"}


def endpoint(base_url: str) -> str:
    return (base_url or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"


def build_request(*, base_url: str, api_key: str, model: str, system: str, user: str, max_tokens: int,
                  extra: Optional[Dict[str, Any]] = None):
    bad = sorted(set(extra or {}) - ALLOWED_PARAMS)
    if bad:
        raise ContentRejected(f"Qwen 适配器不支持参数：{'、'.join(bad)}", "PARAM_UNSUPPORTED")
    # 兼容模式的 json_object 要求提示里出现 JSON 字样
    sys_text = system if "JSON" in system.upper() else system + "\n请以 JSON 对象格式输出。"
    body = {"model": model, "max_tokens": int(max_tokens),
            "messages": [{"role": "system", "content": sys_text}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}}
    body.update(extra or {})
    headers = {"content-type": "application/json", "authorization": f"Bearer {api_key}"}
    return endpoint(base_url), headers, json.dumps(body, ensure_ascii=False).encode("utf-8")


def parse_response(raw: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
        choice = (data.get("choices") or [None])[0]
        text = ((choice or {}).get("message") or {}).get("content") or ""
    except (ValueError, AttributeError, UnicodeDecodeError, TypeError):
        raise ContentRejected("模型响应不是合法的 chat/completions 结构", "MODEL_OUTPUT_INVALID") from None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    norm_usage = None
    if usage and usage.get("prompt_tokens") is not None and usage.get("completion_tokens") is not None:
        norm_usage = {"input_tokens": int(usage["prompt_tokens"]), "output_tokens": int(usage["completion_tokens"])}
    return {"text": text, "model": data.get("model"), "finish_reason": (choice or {}).get("finish_reason"),
            "usage": norm_usage, "provider_request_id": data.get("id") or ""}
