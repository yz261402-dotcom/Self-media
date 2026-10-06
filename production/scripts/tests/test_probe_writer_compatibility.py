import importlib.util
import json
import sys
from pathlib import Path

import httpx2
import pytest

from test_probe_content_writer import isolated, last_call

PRODUCTION = Path(__file__).resolve().parents[2]


def load():
    path = PRODUCTION / 'scripts' / 'probe_writer_compatibility.py'
    assert path.exists(), 'compatibility probe is not implemented'
    sys.path.insert(0, str(PRODUCTION / 'scripts'))
    spec = importlib.util.spec_from_file_location('probe_writer_compatibility', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def message():
    return {'id':'msg_test','type':'message','role':'assistant','model':'claude-sonnet-5-5',
            'content':[{'type':'text','text':'{"ok":true}'}], 'stop_reason':'end_turn',
            'stop_sequence':None,'usage':{'input_tokens':12,'output_tokens':5}}


def sse():
    msg = message(); msg['content'] = []; msg['stop_reason'] = None
    events = [('message_start',{'type':'message_start','message':msg}),
              ('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),
              ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'{"ok":true}'}}),
              ('content_block_stop',{'type':'content_block_stop','index':0}),
              ('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':5}}),
              ('message_stop',{'type':'message_stop'})]
    return ''.join('event: '+name+'\ndata: '+json.dumps(data)+'\n\n' for name,data in events).encode()


def test_preview_is_free_and_two_cases_are_concrete(isolated,tmp_path):
    p=load(); plan=p.run_probe(tmp_path/'probe')
    assert last_call(isolated) is None
    assert plan['maximum_provider_attempts']==2 and plan['max_tokens']==64
    assert [x['stream'] for x in plan['cases']]==[False,True]
    assert plan['authorization']=='Bearer' and plan['sdk_retries']==0
    assert 'sk-test-private' not in json.dumps(plan)


def test_sdk_bearer_only_single_attempt_and_success_stops(isolated,tmp_path,monkeypatch):
    p=load(); seen=[]
    monkeypatch.setenv('ANTHROPIC_API_KEY','sk-unwanted-inherited')
    def handler(req):
        seen.append(req); body=json.loads(req.content)
        assert req.url==httpx2.URL('https://relay.example/v1/messages')
        assert req.headers['authorization']=='Bearer sk-test-private-writer-key'
        assert 'x-api-key' not in req.headers and 'x-app' not in req.headers
        assert 'anthropic-beta' not in req.headers and 'claude-cli' not in req.headers['user-agent']
        assert body['model']==p.MODEL and body['max_tokens']==64 and body['stream'] is False
        return httpx2.Response(200,json=message(),headers={'request-id':'req_ok'})
    out=tmp_path/'probe';p.run_probe(out)
    report=p.run_probe(out,execute=True,transport=httpx2.MockTransport(handler))
    assert len(seen)==1 and report['probe_ok'] and report['selected_case']=='bearer_nonstream'
    assert last_call(isolated)['usage']=={'input_tokens':12,'output_tokens':5}
    with pytest.raises(p.ProbeRejected):p.run_probe(out,execute=True,transport=httpx2.MockTransport(handler))
    assert len(seen)==1


def test_502_then_stream_sdk_assembles_message_without_retry(isolated,tmp_path):
    p=load();seen=[]
    def handler(req):
        body=json.loads(req.content);seen.append(body)
        if not body['stream']:return httpx2.Response(502,json={'error':{'type':'upstream_error','message':'forbidden sk-test-private-writer-key'}})
        return httpx2.Response(200,content=sse(),headers={'content-type':'text/event-stream','request-id':'req_stream'})
    out=tmp_path/'probe';p.run_probe(out)
    report=p.run_probe(out,execute=True,transport=httpx2.MockTransport(handler))
    assert len(seen)==2 and report['selected_case']=='bearer_stream' and report['probe_ok']
    assert last_call(isolated)['result']['model']==p.MODEL
    assert 'sk-test-private' not in ''.join(f.read_text(encoding='utf-8') for f in out.glob('*.json'))


def test_redirect_is_not_followed_and_credentials_stay_at_fixed_host(isolated,tmp_path):
    p=load();seen=[]
    def handler(req):
        seen.append(req);return httpx2.Response(307,headers={'location':'https://other.example/v1/messages'})
    out=tmp_path/'probe';p.run_probe(out)
    report=p.run_probe(out,execute=True,transport=httpx2.MockTransport(handler))
    assert all(str(x.url)=='https://relay.example/v1/messages' for x in seen)
    assert len(seen)<=2 and not report['probe_ok']


def test_unknown_timeout_stops_other_case_and_cannot_replay(isolated,tmp_path):
    p=load();seen=[]
    def handler(req):seen.append(req);raise httpx2.ReadTimeout('uncertain',request=req)
    out=tmp_path/'probe';p.run_probe(out)
    report=p.run_probe(out,execute=True,transport=httpx2.MockTransport(handler))
    assert len(seen)==1 and report['cases'][0]['state']=='unknown'
    with pytest.raises(p.ProbeRejected):p.run_probe(out,execute=True,transport=httpx2.MockTransport(handler))


def test_execution_requires_same_saved_plan(isolated,tmp_path,monkeypatch):
    p=load();out=tmp_path/'probe'
    with pytest.raises(p.ProbeRejected):p.run_probe(out,execute=True)
    p.run_probe(out);monkeypatch.setenv('AVATAR_STUDIO_ANTHROPIC_BASE_URL','https://changed.example')
    with pytest.raises(p.ProbeRejected):p.run_probe(out,execute=True)
    assert last_call(isolated) is None


def test_changed_credential_rejects_saved_plan_before_any_send(isolated,tmp_path,monkeypatch):
    p=load();out=tmp_path/'probe';plan=p.run_probe(out)
    assert plan.get('credential_fingerprint_sha256')
    monkeypatch.setenv('AVATAR_STUDIO_ANTHROPIC_API_KEY','sk-another-private-credential')
    calls=[]
    with pytest.raises(p.ProbeRejected):
        p.run_probe(out,execute=True,transport=httpx2.MockTransport(lambda req:calls.append(req)))
    assert calls==[] and last_call(isolated) is None
    assert not (out/'execution.json').exists()


@pytest.mark.parametrize('pending_state',['leased','waiting_external','blocked','failed'])
def test_settlement_error_with_nonterminal_ledger_stops_other_case(isolated,tmp_path,monkeypatch,pending_state):
    from app import jobs
    from app.content import tasks
    p=load();out=tmp_path/'probe';p.run_probe(out);seen=[]
    def handler(req):
        seen.append(req)
        return httpx2.Response(200,json=message())
    def settle_failure(**kwargs):
        kwargs['send']()
        raise RuntimeError('settlement_db_write_failed')
    monkeypatch.setattr(tasks,'run_call',settle_failure)
    monkeypatch.setattr(jobs,'content_calls',lambda parent_id:[{'id':'pending_diagnostic','state':pending_state,'attempts':1}])
    report=p.run_probe(out,execute=True,transport=httpx2.MockTransport(handler))
    assert len(seen)==1 and len(report['cases'])==1
    assert report['cases'][0]['state']==pending_state and not report['probe_ok']


def test_sse_error_body_is_saved_when_response_stream_cannot_be_read(isolated,tmp_path):
    p=load();out=tmp_path/'probe';p.run_probe(out);seen=[]
    class ErrorStream(httpx2.SyncByteStream):
        def __iter__(self):
            yield b'event: error\ndata: {"type":"error","error":{"type":"upstream_error","message":"SSE upstream forbidden sk-test-private-writer-key"}}\n\n'
    def handler(req):
        seen.append(req)
        if not json.loads(req.content)['stream']:
            return httpx2.Response(502,json={'error':{'type':'upstream_error','message':'nonstream unavailable'}})
        return httpx2.Response(200,stream=ErrorStream(),headers={'content-type':'text/event-stream','request-id':'sse_error_id'})
    report=p.run_probe(out,execute=True,transport=httpx2.MockTransport(handler))
    evidence=json.loads((out/'bearer_stream.response.json').read_text(encoding='utf-8'))
    assert len(seen)==2 and report['cases'][-1]['state']=='unknown'
    assert evidence['status']==200 and evidence['headers']['request-id']=='sse_error_id'
    assert 'SSE upstream forbidden' in evidence['error_body_redacted']
    assert 'sk-test-private' not in evidence['error_body_redacted']
    assert report['cases'][-1]['error']['type']!='ResponseNotRead'
