"""同输入补测一次火山口播；复用已测试的官方签名与限次账本。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
LEGACY = ROOT.parents[1] / '数字人口播选型'
sys.path.insert(0, str(LEGACY))
import 执行首轮 as api
from 试验账本 import TrialLedger

AUTH_ID = 'TH-COMP-002-user-same-avatar-comparison'


def verify_source(path, expected):
    if api.sha256(Path(path)) != expected:
        raise ValueError('素材摘要不一致，停止提交')


def submit_once(root, body, call):
    root = Path(root)
    ledger = TrialLedger(root, AUTH_ID, maximum_fen=2000)
    ledger.authorize_submission('volc_video', 2000, api.digest(body))
    api.atomic_json(root / '提交请求.json', {
        'started_at': api.now(), 'request': api.redact(body),
        'request_digest': api.digest(body), 'automatic_retry': False,
    })
    started = time.monotonic()
    response = call(body)
    elapsed = time.monotonic() - started
    api.atomic_json(root / '提交响应.json', api.redact(response))
    task = (response.get('data') or {}).get('task_id')
    accepted = 200 <= response.get('_http_status', 0) < 300 and response.get('code') == 10000 and bool(task)
    result = {
        'finished_at': api.now(), 'state': 'accepted' if accepted else 'unknown',
        'http_status': response.get('_http_status'), 'business_code': response.get('code'),
        'message': api.redact(response.get('message', '')), 'request_id': response.get('request_id'),
        'task_id': task if accepted else None, 'submit_http_seconds': elapsed,
        'generation_seconds': None, 'actual_cost_cny': None,
        'accepted': accepted, 'automatic_retry': False,
    }
    api.atomic_json(root / '补测结果.json', result)
    ledger.record_result('volc_video', result['state'], evidence_path='提交响应.json', remote_id=result['task_id'])
    return result


def inputs():
    # 此 helper 的 RUN 在这里仍指向旧证据，只读校验，不改旧账本。
    original_run = api.RUN
    api.RUN = LEGACY / 'runs' / 'TH-AB-001'
    try:
        image_url, audio_url = api.approved_inputs()
        manifest = json.loads((api.RUN / '输入清单.json').read_text(encoding='utf-8'))
    finally:
        api.RUN = original_run
    checked = []
    for name, url, expected, cap in [
        ('人物.png', image_url, manifest['image_sha256'], 5_000_000),
        ('口播.wav', audio_url, manifest['audio_sha256'], 15_000_000),
    ]:
        path = ROOT / '输入核验' / name
        if not path.exists():
            api.download(url, path, cap)
        verify_source(path, expected)
        checked.append({'file': str(path.relative_to(ROOT)), 'sha256': expected, 'url_sha256': api.hash_url(url)})
    api.atomic_json(ROOT / '同输入核验.json', {
        'verified_at': api.now(), 'source_experiment': 'TH-AB-001',
        'duration_seconds': 9.36, 'input_assets': checked,
        'new_image_generated': False, 'new_tts_generated': False,
    })
    return api.video_requests(image_url, audio_url)[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('step', choices=['submit', 'poll', 'fetch'])
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print('仅提供命令；加 --execute 执行当前已授权步骤。')
        return
    secrets = api.credentials()
    if args.step == 'submit':
        # 先检查旧记录，避免再次提交或自动切换实验号。
        ledger = TrialLedger(ROOT, AUTH_ID, maximum_fen=2000)
        if ledger.read()['stages']:
            raise RuntimeError('second submission forbidden')
        body = inputs()
        api.RUN = ROOT
        result = submit_once(ROOT, body, lambda request: api.volc_request('CVSubmitTask', request, secrets))
        print(json.dumps(result, ensure_ascii=False))
        return
    result = json.loads((ROOT / '补测结果.json').read_text(encoding='utf-8'))
    if not result['accepted'] or not result['task_id']:
        raise api.SafeStop('未受理任务，禁止查询或伪造第三个样片')
    api.RUN = ROOT
    if args.step == 'poll':
        body = {'req_key': api.MODEL_A, 'task_id': result['task_id']}
        response = api.volc_request('CVGetResult', body, secrets)
        stamp = str(time.time_ns())
        api.atomic_json(ROOT / '查询记录' / (stamp + '.json'), api.redact(response))
        api.atomic_json(ROOT / '查询响应.private.json', response)
        state, _ = api.video_status('A', response)
        print(json.dumps({'state': state, 'request_id': response.get('request_id')}, ensure_ascii=False))
    else:
        response = json.loads((ROOT / '查询响应.private.json').read_text(encoding='utf-8'))
        state, url = api.video_status('A', response)
        if state != 'succeeded':
            raise api.SafeStop('第三样片尚未成功')
        target = ROOT / '火山口播原片.mp4'
        api.download(url, target, 250_000_000)
        api.atomic_json(ROOT / '火山原片来源.json', {'sha256': api.sha256(target), 'url_sha256': api.hash_url(url), 'saved_at': api.now()})
        print(json.dumps({'saved': target.name, 'sha256': api.sha256(target)}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps(api.safe_error(error), ensure_ascii=False))
        raise SystemExit(2)
