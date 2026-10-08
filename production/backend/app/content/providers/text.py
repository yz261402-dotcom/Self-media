"""角色 → 模型 profile 路由与统一文本调用。

业务层只见 TextResult（字典）：底层供应商对象、原始响应不外泄。
每次调用都经 tasks.run_call 进入 provider_jobs 台账（预留 → 发送前检查点 → 请求一次 → 结算）；
不做 SDK/自动重试，不在错误信息里回显请求、响应原文或凭据。
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from .. import costs, tasks
from ..errors import ContentBlocked, ContentRejected
from ...config import PROVIDERS
from ...jobs import args_digest
from . import anthropic, qwen, openai, claude_compat

Transport = Callable[[str, Dict[str, str], bytes, int], Tuple[int, bytes]]

ROLES = ("planner", "writer", "repair", "reviewer")
_DEFAULTS = {
    "planner": ("openai", "gpt-6-astra"), "reviewer": ("openai", "gpt-6-astra"),
    "writer": ("openai", "gpt-6.1-sol"), "repair": (None, None),  # repair 缺省跟随 writer
}
_MAX_OUTPUT = {"planner": 2048, "reviewer": 4096, "writer": 6144, "repair": 6144}
_ADAPTERS = {"anthropic": anthropic, "claude_compat": claude_compat, "qwen": qwen, "openai": openai}
# 官方标准价（2026-09-29，非代理渠道报价）；渠道价不同时用环境变量覆盖。
_PRICES = {
    "anthropic": {"currency": "USD", "input_per_million": "2", "output_per_million": "10",
                  "price_date": "2026-09-29", "source": "官方标准价"},
    "qwen": {"currency": "CNY", "input_per_million": "2", "output_per_million": "8",
             "price_date": "2026-09-29", "source": "官方标准价（北京地域，单请求输入≤256k）"},
    "openai": {"currency": "USD", "input_per_million": "2", "output_per_million": "10",
               "cached_input_per_million": "0.1", "cache_write_per_million": "2.5",
               "price_date": "2026-10-01", "source": "GPT-6.1 Sol 参考价；GPT-6 Astra 与中转渠道实际费用待核对",
               "price_verified": False, "model": "gpt-6.1-sol"},
    "claude_compat": {"currency": "USD", "input_per_million": "2", "output_per_million": "10",
                       "price_date": "2026-09-29", "source": "Claude 官方标准价；兼容中转渠道实际费用待核对"},
}
DEFAULT_USER_AGENT = "AvatarStudio/0.1"
CLAUDE_CODE_INPUT_OVERHEAD = 4096


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _http_proxy_mode() -> str:
    mode = _env("AVATAR_STUDIO_CONTENT_HTTP_PROXY_MODE", "system")
    if mode not in ("system", "direct"):
        raise ContentBlocked("文本 HTTP 代理方式只支持 system（系统代理）或 direct（直连）",
                             "TEXT_PROXY_MODE_INVALID", retryable=False)
    return mode


def profile(role: str) -> Dict[str, Any]:
    """读取角色配置；模型 ID、供应商、协议都显式，不按相似名字替换。"""
    if role not in ROLES:
        raise ContentRejected(f"未知文本角色：{role}", "VALIDATION_FAILED")
    up = role.upper()
    d_prov, d_model = _DEFAULTS[role]
    if role == "repair":
        d_prov, d_model = profile_defaults_of_writer()
    provider = _env(f"AVATAR_STUDIO_CONTENT_{up}_PROVIDER", d_prov or "")
    model = _env(f"AVATAR_STUDIO_CONTENT_{up}_MODEL", d_model or "")
    if provider not in _ADAPTERS:
        raise ContentBlocked(f"角色 {role} 配置的供应商 {provider or '(空)'} 没有适配器", "PROVIDER_UNSUPPORTED",
                             retryable=False)
    execution_default = (_env("AVATAR_STUDIO_CONTENT_WRITER_EXECUTION_MODE", "http_api")
                         if role == "repair" else "http_api")
    execution_mode = _env(f"AVATAR_STUDIO_CONTENT_{up}_EXECUTION_MODE", execution_default)
    if execution_mode not in ("http_api", "claude_code") or (
            execution_mode == "claude_code" and (provider != "anthropic" or role not in ("writer", "repair"))):
        raise ContentBlocked("Claude Code 执行方式只适用于 Anthropic 写稿与返工角色", "PROVIDER_EXECUTION_UNSUPPORTED",
                             retryable=False)
    wait = _env(f"AVATAR_STUDIO_CONTENT_{up}_TIMEOUT_SECONDS", "600" if execution_mode == "claude_code" else "120")
    if not wait.isdigit() or not 30 <= int(wait) <= 1800:
        raise ContentBlocked("文本调用等待时间须为 30–1800 秒的整数", "TEXT_TIMEOUT_INVALID", retryable=False)
    spec = PROVIDERS[provider]
    price = dict(_PRICES[provider])
    if provider == "openai":
        price["model"] = model
    pe = provider.upper()
    for key, env in (("input_per_million", f"AVATAR_STUDIO_{pe}_PRICE_INPUT"),
                     ("output_per_million", f"AVATAR_STUDIO_{pe}_PRICE_OUTPUT")):
        if _env(env):
            price[key] = _env(env)
    if price["currency"] != "CNY":
        fx = _env("AVATAR_STUDIO_FX_USD_CNY")
        if fx:
            price["fx_cny_per_unit"] = fx
            price["fx_date"] = _env("AVATAR_STUDIO_FX_DATE")
    return {"role": role, "provider": provider, "model": model,
            "protocol": "claude-code-cli" if execution_mode == "claude_code" else _ADAPTERS[provider].PROTOCOL,
            "base_url": _env(spec.base_env), "key_env": spec.key_env,
            "execution_mode": execution_mode, "timeout_seconds": int(wait),
            "http_proxy_mode": _http_proxy_mode() if execution_mode == "http_api" else None,
            "max_output": int(_env(f"AVATAR_STUDIO_CONTENT_{up}_MAX_OUTPUT", str(_MAX_OUTPUT[role]))),
            "price": price}


def profile_defaults_of_writer() -> Tuple[str, str]:
    return (_env("AVATAR_STUDIO_CONTENT_WRITER_PROVIDER", _DEFAULTS["writer"][0]),
            _env("AVATAR_STUDIO_CONTENT_WRITER_MODEL", _DEFAULTS["writer"][1]))


def _runtime_profile(profile_: Dict[str, Any], api_key: str, executable: str = "") -> Dict[str, Any]:
    p = profile_
    identity = {k: p[k] for k in ("provider", "model", "base_url", "execution_mode", "max_output")}
    identity["credential_digest"] = hashlib.sha256(api_key.encode()).hexdigest()
    if p["execution_mode"] == "http_api":
        identity["http_proxy_mode"] = p["http_proxy_mode"]
    if p["execution_mode"] == "claude_code":
        identity["transport_profile"] = "native-cli-append-system-v1"
        identity["context_1m"] = _env("AVATAR_STUDIO_CLAUDE_CODE_CONTEXT_1M", "false").casefold() in ("true", "1", "yes")
    elif p["provider"] == "openai":
        identity["reasoning_effort"] = _env("AVATAR_STUDIO_CONTENT_GPT_REASONING_EFFORT", "medium")
    identity["cli_executable"] = executable or _env("AVATAR_STUDIO_CLAUDE_CODE_EXECUTABLE") if p["execution_mode"] == "claude_code" else ""
    return {"execution_mode": p["execution_mode"], "timeout_seconds": p["timeout_seconds"],
            "http_proxy_mode": p["http_proxy_mode"],
            "profile_id": args_digest(identity)}


def runtime_profile(role: str) -> Dict[str, Any]:
    """前端只读运行身份：配置变化撤销旧意图，不暴露密钥。"""
    p = profile(role)
    return _runtime_profile(p, os.environ.get(p["key_env"], ""))


def capabilities() -> Dict[str, Any]:
    """能力页：只展示配置与协议、价格日期，不含凭据。"""
    out = {}
    for role in ROLES:
        try:
            p = profile(role)
            out[role] = {"provider": p["provider"], "model": p["model"], "protocol": p["protocol"],
                         "execution_mode": p["execution_mode"], "timeout_seconds": p["timeout_seconds"],
                         "http_proxy_mode": p["http_proxy_mode"],
                         "configured": bool(os.environ.get(p["key_env"])),
                         "price": {k: v for k, v in p["price"].items()}}
        except ContentBlocked as exc:
            out[role] = {"error": exc.message}
    return out


def _model_matches(requested: str, actual: Optional[str]) -> bool:
    if not actual:
        return True  # 上游未回报模型名：无法证明不一致，留痕在 result.model=None
    a, r = actual.lower(), requested.lower()
    return a == r or a.startswith(r + "-") or a.startswith(r + "@")


def _estimate_input_tokens(system: str, user: str) -> int:
    # 中文按 1 字≈1 token 偏保守估算，只用于预留上限；真实费用取供应商 usage
    return len(system) + len(user) + 64


def _configured_input_limit() -> Optional[int]:
    """Return an optional local input budget; an empty value or 0 disables it.

    Context windows belong to the configured provider/model, so a universal
    24k gate rejects valid requests before they reach models with larger
    contexts. Deployments can still opt into a local budget when they need one.
    """
    raw = _env("AVATAR_STUDIO_CONTENT_MAX_INPUT_TOKENS")
    if not raw or raw == "0":
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ContentBlocked("AVATAR_STUDIO_CONTENT_MAX_INPUT_TOKENS 须为正整数或 0（表示不限制）",
                             "TEXT_INPUT_LIMIT_INVALID", retryable=False)
    return int(raw)


def _default_transport(url: str, headers: Dict[str, str], body: bytes, timeout: int) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers={"User-Agent": DEFAULT_USER_AGENT, **headers}, method="POST")
    # urllib 在 Windows 会自动读取注册表代理；直连策略必须显式禁用它，且不能失败后换路径重发。
    open_request = (urllib.request.build_opener(urllib.request.ProxyHandler({})).open
                    if _http_proxy_mode() == "direct" else urllib.request.urlopen)
    try:
        with open_request(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _error_kind(status: int, body: bytes) -> Tuple[str, str]:
    """取错误短字段并识别已知上游拒绝；不回显 message 原文。"""
    tag = ""
    upstream_forbidden = False
    client_restricted = False
    try:
        err = (json.loads(body.decode("utf-8", "replace")) or {}).get("error") or {}
        tag = str(err.get("type") or err.get("code") or "")[:60] if isinstance(err, dict) else ""
        upstream_forbidden = (500 <= status < 600 and tag == "upstream_error"
                              and isinstance(err.get("message"), str)
                              and err["message"].strip().casefold().startswith("upstream access forbidden"))
        client_restricted = (500 <= status < 600 and tag == "api_error"
                             and isinstance(err.get("message"), str)
                             and "this group only allows claude code clients" in err["message"].casefold())
    except Exception:  # noqa: BLE001
        pass
    if status in (401, 403):
        return "AUTH_FAILED", tag
    if status == 429:
        return "RATE_LIMITED", tag
    if status == 404:
        return "MODEL_NOT_FOUND", tag
    if upstream_forbidden:
        return "UPSTREAM_ACCESS_FORBIDDEN", "upstream_error"
    if client_restricted:
        return "PROVIDER_CLIENT_RESTRICTED", "api_error"
    return "PROVIDER_ERROR", tag


def _strip_fence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    return m.group(1) if m else t


def call_text(role: str, *, parent: dict, step: str, round_no: int = 0, system: str, user: str,
              transport: Optional[Transport] = None, timeout: Optional[int] = None,
              max_output: Optional[int] = None, extra: Optional[Dict[str, Any]] = None,
              idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    """一次角色文本调用。返回 {json, text, provider, model, provider_request_id, usage, finish_reason,
    response_digest, job_id, cost_fen, replayed}。"""
    prof = profile(role)
    api_key = os.environ.get(prof["key_env"], "")
    if not api_key:
        raise ContentBlocked(f"未配置 {prof['key_env']}，不发起调用，也不换用其他模型", "PROVIDER_NOT_CONFIGURED",
                             retryable=False)
    is_cli = prof["execution_mode"] == "claude_code"
    if is_cli and transport is not None:
        raise ContentRejected("Claude Code 路线不能注入 HTTP 运输层", "TRANSPORT_MODE_CONFLICT")
    response_wait = int(timeout if timeout is not None else prof["timeout_seconds"])
    if not 1 <= response_wait <= 1800:
        raise ContentRejected("文本等待时间不合法", "TEXT_TIMEOUT_INVALID")
    overhead = CLAUDE_CODE_INPUT_OVERHEAD if is_cli else 0
    input_estimate = _estimate_input_tokens(system, user) + overhead
    input_limit = _configured_input_limit()
    if input_limit is not None and input_estimate > input_limit:
        raise ContentRejected(f"输入超过已配置的 {input_limit:,} tokens 上限；系统不会静默截断审核依据",
                              "INPUT_TOO_LARGE")
    adapter = _ADAPTERS[prof["provider"]]
    max_tokens = max_output or prof["max_output"]
    url, headers, body = adapter.build_request(base_url=prof["base_url"], api_key=api_key, model=prof["model"],
                                               system=system, user=user, max_tokens=max_tokens, extra=extra)
    cli_payload = json.loads(body) if is_cli else None
    cli_config = None
    if is_cli:
        from . import claude_code
        if extra and "stop_sequences" in extra:
            raise ContentRejected("Claude Code 路线不支持 stop_sequences 参数", "PARAM_UNSUPPORTED")
        max_tokens = int(cli_payload["max_tokens"])
        input_estimate = _estimate_input_tokens(cli_payload["system"], cli_payload["messages"][0]["content"]) + overhead
        if input_limit is not None and input_estimate > input_limit:
            raise ContentRejected(f"输入超过已配置的 {input_limit:,} tokens 上限，未提交 Claude Code 写稿",
                                  "INPUT_TOO_LARGE")
        try:
            cli_config = claude_code.preflight(base_url=prof["base_url"], api_key=api_key, model=prof["model"],
                                               max_tokens=max_tokens, timeout=response_wait)
        except tasks.NotSent as exc:
            raise ContentBlocked(exc.message, exc.code, retryable=False) from exc
    try:
        reserve = costs.cost_fen(prof["price"], input_estimate, max_tokens)
    except costs.CostError as exc:
        raise ContentBlocked(exc.message + "（请配置汇率 AVATAR_STUDIO_FX_USD_CNY）", exc.code, retryable=False) from exc
    request_identity = {"role": role, "provider": prof["provider"], "model": prof["model"],
                        "max": max_tokens, "system": system, "user": user, "extra": extra or {}}
    if is_cli:
        request_identity.update(execution_mode="claude_code", runtime_profile_id=_runtime_profile(
            prof, api_key, (cli_config or {}).get("executable", ""))["profile_id"])
    request_digest = args_digest(request_identity)
    send_fn = transport or _default_transport

    def send() -> tasks.CallOutcome:
        if is_cli:
            parsed = claude_code.call(base_url=prof["base_url"], api_key=api_key, model=prof["model"],
                                      system=cli_payload["system"], user=cli_payload["messages"][0]["content"],
                                      max_tokens=max_tokens, timeout=response_wait,
                                      executable=(cli_config or {}).get("executable"),
                                      context_1m=(cli_config or {}).get("context_1m"))
        else:
            status, raw = send_fn(url, headers, body, response_wait)
            if status >= 400:
                code, tag = _error_kind(status, raw)
                message = (f"模型 {prof['model']} 的上游访问被拒绝（HTTP {status}）。"
                           "请联系中转管理员开通或恢复这条模型线路；原调用费用尚待核对。"
                           if code == "UPSTREAM_ACCESS_FORBIDDEN" else
                           f"{prof['provider']} 返回 HTTP {status}" + (f"（{tag}）" if tag else ""))
                if code == "PROVIDER_CLIENT_RESTRICTED":
                    message = ("当前密钥所在分组只允许 Claude Code 客户端，不能使用直接 API 生成文案。"
                               "请选择通用 API 分组的密钥，或使用真实 Claude Code 客户端线路。")
                raise tasks.ProviderRefused(message, code, status, charged=False if status < 500 else None)
            parsed = adapter.parse_response(raw)
        cost = None
        if parsed["usage"] and prof["price"].get("price_verified", True):
            cost = costs.cost_fen(prof["price"], parsed["usage"]["input_tokens"], parsed["usage"]["output_tokens"])
        return tasks.CallOutcome(response=parsed, usage=parsed["usage"], cost_fen=cost,
                                 provider_request_id=parsed["provider_request_id"])

    replayed_before = _existing_call(parent["id"], idempotency_key, step, round_no, prof, request_digest)
    job = tasks.run_call(parent=parent, provider=prof["provider"], model=prof["model"], capability="chat",
                         step=step, round_no=round_no, request={"digest": request_digest}, send=send,
                         price_snapshot=prof["price"], reserve_fen=reserve, idempotency_key=idempotency_key,
                         lease_seconds=response_wait + 60)
    parsed = job["result"]
    if (parsed.get("model") != prof["model"] if is_cli else not _model_matches(prof["model"], parsed.get("model"))):
        raise ContentRejected(f"供应商实际返回的模型与配置不一致（配置 {prof['model']}，实际 {parsed.get('model')}）："
                              "不按同名模型接受", "MODEL_MISMATCH")
    if parsed.get("refusal"):
        raise ContentRejected("模型拒绝了本次内容请求；已保留返回用量，不会自动重发", "MODEL_REFUSED")
    if parsed.get("finish_reason") == "length":
        raise ContentRejected("模型输出被截断，未交给任何“修复 JSON”调用", "OUTPUT_TRUNCATED")
    try:
        obj = json.loads(_strip_fence(parsed["text"]))
    except ValueError:
        raise ContentRejected("模型输出不是合法 JSON", "MODEL_OUTPUT_INVALID") from None
    if not isinstance(obj, dict):
        raise ContentRejected("模型输出不是 JSON 对象", "MODEL_OUTPUT_INVALID")
    return {"json": obj, "text": parsed["text"], "provider": prof["provider"],
            "model": parsed.get("model") or prof["model"], "provider_request_id": parsed.get("provider_request_id", ""),
            "usage": parsed.get("usage"), "finish_reason": parsed.get("finish_reason"),
            "response_digest": job.get("result_digest"), "job_id": job["id"],
            "cost_fen": job.get("actual_cost_fen"), "replayed": replayed_before}


def _existing_call(parent_id, explicit_key, step, round_no, prof, request_digest) -> bool:
    from ... import jobs, storage as st
    key = explicit_key or jobs.content_call_key(parent_id, step, round_no, prof["provider"], prof["model"],
                                                args_digest({"digest": request_digest}))
    row = st.conn().execute("SELECT 1 FROM provider_jobs_idem WHERE idem_key=?", [key]).fetchone()
    return row is not None
