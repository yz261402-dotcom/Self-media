"""固定 Claude 线路单次探针；默认只生成计划，--execute 才提交一次计费请求。

读取现有 backend/.env 与 bootstrap_secrets，不改配置、作品或创作指针。
同一 endpoint / 请求 / 价格计划具有固定幂等键；超时及 5xx 保留待结算状态。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PRODUCTION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCTION / "backend"))
MODEL = "claude-sonnet-5-5"
WORK_ID = "work_9a7bd936e13d"
MAX_TOKENS = 64
TIMEOUT = 30
DEFAULT_DIR = PRODUCTION / "runtime" / "writer-502-20261001"
RESPONSE_LIMIT = 65536
REQUEST_HEADERS = {"request-id", "x-request-id", "anthropic-request-id", "cf-ray", "x-amzn-requestid"}


class ProbeRejected(ValueError):
    pass


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(url, headers, body, timeout):
    """不重试、不跟随重定向；3xx 同样作为明确 HTTP 响应保存。"""
    opener = urllib.request.build_opener(NoRedirects())
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status, response.read(RESPONSE_LIMIT + 1), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(RESPONSE_LIMIT + 1), dict(exc.headers.items())


def _scrub(value, api_key):
    result = str(value)
    secrets = [api_key] + [v for k, v in os.environ.items() if v and len(v) >= 6 and
                           any(part in k.upper() for part in ("API_KEY", "TOKEN", "PASSWORD", "SECRET_KEY"))]
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = re.sub(r"\bsk-[A-Za-z0-9_-]{6,}", "[REDACTED]", result)
    return re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", result)


def _save(path, value, exclusive=False):
    with path.open("x" if exclusive else "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)


def _error_body(raw, api_key):
    decoded = raw.decode("utf-8", "replace")
    try:
        value = json.loads(decoded)
    except ValueError:
        # 无法解析的响应也消除码点转义后再脱敏，避免编码形式保留凭据。
        decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), decoded)
        return _scrub(decoded, api_key)
    def clean(item):
        if isinstance(item, str):
            return _scrub(item, api_key)
        if isinstance(item, list):
            return [clean(x) for x in item]
        if isinstance(item, dict):
            return {_scrub(k, api_key): clean(v) for k, v in item.items()}
        return item
    return json.dumps(clean(value), ensure_ascii=False)


def _settings():
    from app import config
    from app.content import costs
    from app.content.providers import anthropic, text
    config._load_local_env_file()
    config.bootstrap_secrets()
    profile = text.profile("writer")
    if profile["provider"] != "anthropic" or profile["model"] != MODEL or profile["protocol"] != "anthropic-messages":
        raise ProbeRejected("当前 writer 配置与固定探针计划不符；不替换模型。")
    key = os.environ.get(profile["key_env"], "")
    if not key:
        raise ProbeRejected("writer 密钥未配置；未提交。")
    url, headers, body = anthropic.build_request(base_url=profile["base_url"], api_key=key, model=MODEL,
                                                system="只输出指定 JSON。", user='只返回 {"ok":true}。',
                                                max_tokens=MAX_TOKENS)
    headers["user-agent"] = text.DEFAULT_USER_AGENT
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProbeRejected("探针 endpoint 必须为无凭据、无查询参数的 HTTPS 地址。")
    request = json.loads(body)
    reserve = costs.cost_fen(profile["price"], len(request["system"]) + len(request["messages"][0]["content"]) + 64,
                              MAX_TOKENS)
    return profile, key, url, headers, body, reserve


def run_probe(output_dir=DEFAULT_DIR, *, execute=False, transport=None):
    from app import jobs, storage as st
    from app.content import costs, tasks
    from app.content.providers import anthropic, text
    profile, api_key, url, headers, body, reserve = _settings()
    plan = {"endpoint": url, "model": MODEL, "protocol": "anthropic-messages", "max_tokens": MAX_TOKENS,
            "timeout_seconds": TIMEOUT, "request_sha256": hashlib.sha256(body).hexdigest(),
            "request": json.loads(body), "work_id": WORK_ID, "step": "writer_connection_probe",
            "reserve_fen": reserve, "price_snapshot": profile["price"], "maximum_provider_attempts": 1,
            "redirects_allowed": False, "contains_credentials": False}
    plan["plan_id"] = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    out = Path(output_dir)
    plan_file = out / "probe_plan.json"
    if plan_file.exists():
        if json.loads(plan_file.read_text(encoding="utf-8")) != plan:
            raise ProbeRejected("已保存计划与当前 endpoint / model / 请求或价格不一致；未发送。")
    elif execute:
        raise ProbeRejected("请先生成并审查 probe_plan.json；未发送。")
    else:
        out.mkdir(parents=True, exist_ok=True)
        try:
            _save(plan_file, plan, exclusive=True)
        except FileExistsError:
            if json.loads(plan_file.read_text(encoding="utf-8")) != plan:
                raise ProbeRejected("已有其他计划；未发送。") from None
    if not execute:
        return plan

    execution_file = out / "execution.json"
    if execution_file.exists():
        raise ProbeRejected("此计划已执行或结果未知（already attempted）；禁止再次提交。")
    parent_id = "diag_writer_probe_" + plan["plan_id"][:24]
    idem_key = "writer-connection-probe:" + plan["plan_id"]
    prior = st.conn().execute("SELECT job_id FROM provider_jobs_idem WHERE idem_key=?", [idem_key]).fetchone()
    if prior:
        raise ProbeRejected("此计划已有台账（already attempted）；禁止再次提交。")
    try:
        _save(execution_file, {"plan_id": plan["plan_id"], "state": "claimed", "fee_result": "unconfirmed"}, exclusive=True)
    except FileExistsError:
        raise ProbeRejected("此计划已被执行者领取；禁止再次提交。") from None

    def send():
        try:
            status, raw, response_headers = (transport or _transport)(url, headers, body, TIMEOUT)
        except Exception as exc:
            _save(out / "response.json", {"transport_error": type(exc).__name__,
                                           "detail_redacted": _scrub(str(exc), api_key), "fee_result": "unconfirmed"})
            raise
        raw = bytes(raw)
        evidence = {"status": status, "headers": {str(k).lower(): _scrub(v, api_key)[:256]
                                                    for k, v in response_headers.items() if str(k).lower() in REQUEST_HEADERS},
                    "response_sha256": hashlib.sha256(raw).hexdigest(), "captured_bytes": len(raw),
                    "capture_truncated": len(raw) > RESPONSE_LIMIT}
        if status >= 300:
            evidence["error_body_redacted"] = _error_body(raw[:RESPONSE_LIMIT], api_key)
            _save(out / "response.json", evidence)
            code, tag = text._error_kind(status, raw)
            tag = _scrub(tag, api_key)
            if status < 400:
                code, tag = "REDIRECT_BLOCKED", "redirect_blocked"
            raise tasks.ProviderRefused(f"anthropic 返回 HTTP {status}" + (f"（{tag}）" if tag else ""),
                                        code, status, charged=False if 400 <= status < 500 else None)
        _save(out / "response.json", evidence)
        response = anthropic.parse_response(raw)
        for field in ("text", "model", "provider_request_id", "finish_reason"):
            if response.get(field) is not None:
                response[field] = _scrub(response[field], api_key)
        usage = response.get("usage")
        actual_cost = costs.cost_fen(profile["price"], usage["input_tokens"], usage["output_tokens"]) if usage else None
        return tasks.CallOutcome(response=response, usage=usage, cost_fen=actual_cost,
                                 provider_request_id=response.get("provider_request_id", ""))

    error = None
    try:
        call = tasks.run_call(parent={"id": parent_id, "kind": "diagnostic", "work_id": WORK_ID}, provider="anthropic",
                              model=MODEL, capability="chat", step=plan["step"], round_no=0,
                              request={"endpoint": url, "request_sha256": plan["request_sha256"]}, send=send,
                              price_snapshot=profile["price"], reserve_fen=reserve, idempotency_key=idem_key,
                              lease_seconds=90)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message_redacted": _scrub(str(exc), api_key)}
        calls = jobs.content_calls(parent_id)
        call = calls[-1] if calls else {}
    report = {"plan_id": plan["plan_id"], "job_id": call.get("id"), "state": call.get("state", "not_sent"),
              "attempts": call.get("attempts", 0), "settlement_state": call.get("settlement_state"),
              "actual_cost_fen": call.get("actual_cost_fen"), "usage": call.get("usage"), "error": error,
              "provider_request_id": _scrub(call.get("provider_request_id", ""), api_key)}
    if call.get("state") == "succeeded":
        result = call.get("result") or {}
        report["returned_model"] = result.get("model")
        try:
            payload_ok = json.loads(result.get("text", "")) == {"ok": True}
        except (ValueError, TypeError):
            payload_ok = False
        report["probe_ok"] = result.get("model") == MODEL and result.get("finish_reason") != "length" and payload_ok
    _save(execution_file, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="在审查计划且获得一次费用授权后提交；不自动重试")
    args = parser.parse_args()
    try:
        result = run_probe(execute=args.execute)
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": "探针未执行或被保护规则阻止；请检查计划和配置。"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not args.execute or result.get("probe_ok") else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
