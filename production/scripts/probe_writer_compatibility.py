"""Bearer/非流式与 Bearer/流式的 Claude 兼容性探针。默认仅生成计划。

--execute 只能在用户授权所存计划的最多两次收费请求后运行；成功或结果未知立即停止。
不重试、不跟随重定向、不冒充 Claude CLI，不修改作品或生产配置。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

PRODUCTION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCTION / 'backend'))
sys.path.insert(0, str(PRODUCTION / 'scripts'))
import probe_content_writer as original

MODEL = original.MODEL
MAX_TOKENS = 64
TIMEOUT = 30
DEFAULT_DIR = PRODUCTION / 'runtime' / 'writer-compatibility-20261001'
ProbeRejected = original.ProbeRejected


def _plan():
    import anthropic as sdk
    profile, key, url, _, _, reserve = original._settings()
    if os.environ.get('ANTHROPIC_CUSTOM_HEADERS'):
        raise ProbeRejected('ANTHROPIC_CUSTOM_HEADERS 会改变探针；请清除后再审查计划。')
    request = {'model': MODEL, 'max_tokens': MAX_TOKENS, 'system': '只输出指定 JSON。',
               'messages': [{'role': 'user', 'content': '只返回 {"ok":true}。'}]}
    plan = {'endpoint': url, 'model': MODEL, 'authorization': 'Bearer', 'protocol': 'anthropic-messages',
            'credential_fingerprint_sha256': hashlib.sha256(key.encode()).hexdigest(),
            'sdk': 'anthropic', 'sdk_version': sdk.__version__, 'sdk_retries': 0,
            'timeout_seconds': TIMEOUT, 'max_tokens': MAX_TOKENS, 'request': request,
            'cases': [{'id': 'bearer_nonstream', 'stream': False}, {'id': 'bearer_stream', 'stream': True}],
            'maximum_provider_attempts': 2, 'maximum_reserve_fen': reserve * 2,
            'reserve_fen_per_case': reserve, 'price_snapshot': profile['price'],
            'step': 'writer_compatibility_probe', 'stop_after_provider_success': True,
            'advance_only_after_confirmed_http_failure': True,
            'stop_after_unknown_result': True, 'redirects_allowed': False,
            'cli_impersonation': False, 'contains_credentials': False}
    plan['plan_id'] = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return profile, key, plan


def _client(key, url, transport=None):
    import anthropic as sdk
    import httpx2
    from app.content import tasks
    if not url.endswith('/v1/messages'):
        raise ProbeRejected('固定 endpoint 不支持 SDK 路径拼接。')
    def pin(request):
        if str(request.url) != url or request.method != 'POST':
            raise tasks.NotSent('SDK 请求地址与已授权计划不一致；已阻止。')
        if request.headers.get('authorization') != 'Bearer ' + key or request.headers.get('x-api-key'):
            raise tasks.NotSent('SDK 鉴权与已授权 Bearer 计划不一致；已阻止。')
        if 'claude-cli' in request.headers.get('user-agent', '').lower() or request.headers.get('x-app') or request.headers.get('anthropic-beta'):
            raise tasks.NotSent('探针不允许模拟 Claude CLI 或携带未经审查的 beta。')
    http = httpx2.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False,
                         transport=transport, event_hooks={'request': [pin]})
    # SDK 1.7.0: 显式 auth_token 禁止读取凭据环境变量；api_key=None 不发送 X-Api-Key。
    return sdk.Anthropic(api_key=None, auth_token=key, base_url=url[:-len('/v1/messages')],
                         max_retries=0, timeout=TIMEOUT, http_client=http)


def _headers(headers, key):
    return {str(k).lower(): original._scrub(v, key)[:256] for k, v in headers.items()
            if str(k).lower() in original.REQUEST_HEADERS}


def _run_case(out, plan, case, profile, key, parent_id, transport):
    import anthropic as sdk
    import httpx2
    from app import jobs
    from app.content import costs, tasks
    from app.content.providers import anthropic as adapter, text
    evidence_path = out / (case['id'] + '.response.json')
    idem = 'writer-compatibility-probe:' + plan['plan_id'] + ':' + case['id']
    http_status = None
    def send():
        nonlocal http_status
        try:
            with _client(key, plan['endpoint'], transport) as client:
                if case['stream']:
                    with client.messages.stream(**plan['request']) as stream:
                        msg = stream.get_final_message()
                        response_headers = stream.response.headers
                        status = stream.response.status_code
                else:
                    raw_response = client.messages.with_raw_response.create(**plan['request'], stream=False)
                    msg = raw_response.parse()
                    response_headers, status = raw_response.headers, raw_response.status_code
                http_status = status
                body = msg.model_dump(mode='json')
                sanitized = json.loads(original._error_body(json.dumps(body).encode(), key))
                original._save(evidence_path, {'status': status, 'headers': _headers(response_headers, key),
                               'body_representation': 'sdk_assembled_sse_message' if case['stream'] else 'sdk_json_message',
                               'response_redacted': sanitized})
                parsed = adapter.parse_response(json.dumps(sanitized).encode())
                usage = parsed.get('usage')
                cost = costs.cost_fen(profile['price'], usage['input_tokens'], usage['output_tokens']) if usage else None
                return tasks.CallOutcome(response=parsed, usage=usage, cost_fen=cost,
                                         provider_request_id=parsed.get('provider_request_id', ''))
        except sdk.APIStatusError as exc:
            http_status = exc.status_code
            try:
                whole_raw = exc.response.content
            except httpx2.ResponseNotRead:
                # SSE error events are decoded by the SDK before raising; the
                # HTTP response stream itself is deliberately still unread.
                whole_raw = json.dumps(exc.body, ensure_ascii=False).encode()
            raw = whole_raw[:original.RESPONSE_LIMIT]
            original._save(evidence_path, {'status': exc.status_code, 'headers': _headers(exc.response.headers, key),
                           'error_body_redacted': original._error_body(raw, key),
                           'capture_truncated': len(whole_raw) > original.RESPONSE_LIMIT})
            if exc.status_code < 300:
                # An error delivered inside HTTP 200 SSE does not confirm a
                # successful response or a no-charge refusal. Preserve unknown.
                raise
            code, tag = text._error_kind(exc.status_code, raw)
            if exc.status_code < 400: code, tag = 'REDIRECT_BLOCKED', 'redirect_blocked'
            raise tasks.ProviderRefused('SDK HTTP ' + str(exc.status_code) + ' (' + original._scrub(tag, key) + ')',
                                        code, exc.status_code, charged=False if 400 <= exc.status_code < 500 else None) from None
        except Exception as exc:
            original._save(evidence_path, {'transport_error': type(exc).__name__,
                           'detail_redacted': original._scrub(str(exc), key), 'fee_result': 'unconfirmed'})
            raise
    error = None
    try:
        call = tasks.run_call(parent={'id': parent_id, 'kind': 'diagnostic', 'work_id': original.WORK_ID},
                              provider='anthropic', model=MODEL, capability='chat', step=plan['step'],
                              round_no=0 if not case['stream'] else 1,
                              request={'plan_id': plan['plan_id'], 'case_id': case['id'], 'endpoint': plan['endpoint']},
                              send=send, price_snapshot=profile['price'], reserve_fen=plan['reserve_fen_per_case'],
                              idempotency_key=idem, lease_seconds=90)
    except Exception as exc:
        error = {'type': type(exc).__name__, 'detail_redacted': original._scrub(str(exc), key)}
        calls = jobs.content_calls(parent_id)
        call = calls[-1] if calls else {}
    result = call.get('result') or {}
    try: valid = json.loads(result.get('text', '')) == {'ok': True}
    except (ValueError, TypeError): valid = False
    return {'case_id': case['id'], 'state': call.get('state', 'not_sent'), 'job_id': call.get('id'),
            'http_status': http_status,
            'attempts': call.get('attempts', 0), 'settlement_state': call.get('settlement_state'),
            'actual_cost_fen': call.get('actual_cost_fen'), 'usage': call.get('usage'), 'error': error,
            'returned_model': result.get('model'), 'stop_reason': result.get('finish_reason'),
            'probe_ok': call.get('state') == 'succeeded' and result.get('model') == MODEL and valid
                        and result.get('finish_reason') != 'length'}


def run_probe(output_dir=DEFAULT_DIR, *, execute=False, transport=None):
    from app import storage as st
    profile, key, plan = _plan()
    out = Path(output_dir); plan_file = out / 'probe_plan.json'
    if plan_file.exists():
        if json.loads(plan_file.read_text(encoding='utf-8')) != plan:
            raise ProbeRejected('所存计划与当前配置或 SDK 不一致；未发送。')
    elif execute:
        raise ProbeRejected('必须先生成并审查计划；未发送。')
    else:
        out.mkdir(parents=True, exist_ok=True)
        original._save(plan_file, plan, exclusive=True)
    if not execute: return plan
    execution = out / 'execution.json'
    if execution.exists(): raise ProbeRejected('此计划已执行或结果未知；禁止再次提交。')
    for case in plan['cases']:
        idem = 'writer-compatibility-probe:' + plan['plan_id'] + ':' + case['id']
        if st.conn().execute('SELECT job_id FROM provider_jobs_idem WHERE idem_key=?', [idem]).fetchone():
            raise ProbeRejected('已有诊断台账；禁止重新执行。')
    try: original._save(execution, {'plan_id': plan['plan_id'], 'state': 'claimed'}, exclusive=True)
    except FileExistsError: raise ProbeRejected('计划已被领取；禁止重复提交。') from None
    report = {'plan_id': plan['plan_id'], 'probe_ok': False, 'selected_case': None, 'cases': []}
    parent_id = 'diag_writer_compat_' + plan['plan_id'][:24]
    for case in plan['cases']:
        item = _run_case(out, plan, case, profile, key, parent_id, transport)
        report['cases'].append(item)
        if item['probe_ok']:
            report['probe_ok'], report['selected_case'] = True, case['id']
        original._save(execution, report)
        if item['state'] != 'failed' or not isinstance(item.get('http_status'), int) or item['http_status'] < 300:
            break
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='仅在用户授权所存收费计划后执行；最多两次且每个 case 仅一次')
    args = parser.parse_args()
    try: result = run_probe(execute=args.execute)
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': '探针被保护规则阻止；检查所存计划。'}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not args.execute or result.get('probe_ok') else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
