from pathlib import Path
import importlib.util
import pytest

ROOT=Path(__file__).resolve().parents[1]

def module():
    p=ROOT/'候选执行器.py'
    assert p.exists(), '候选执行器尚未实现'
    spec=importlib.util.spec_from_file_location('candidate_run',p)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m

def test_second_attempt_blocked_after_uncertain_timeout(tmp_path):
    m=module();calls=[]
    def fail():
        calls.append(1)
        raise TimeoutError('unknown server acceptance')
    with pytest.raises(TimeoutError):m.once(tmp_path,'image_A',{},fail)
    with pytest.raises(FileExistsError):m.once(tmp_path,'image_A',{},fail)
    assert len(calls)==1

def test_success_does_not_allow_duplicate(tmp_path):
    m=module();calls=[]
    def call():calls.append(1);return {'path':'real_output.png'}
    m.once(tmp_path,'image_A',{},call)
    with pytest.raises(FileExistsError):m.once(tmp_path,'image_A',{},call)
    assert len(calls)==1

def test_tts_http_or_business_failure_rejected():
    m=module()
    with pytest.raises(ValueError):m.tts_audio(401,{'base_resp':{'status_code':0},'data':{'audio':'00'}})
    with pytest.raises(ValueError):m.tts_audio(200,{'base_resp':{'status_code':1004},'data':{'audio':'00'}})

def test_attempt_scope_is_bounded(tmp_path):
    m=module()
    with pytest.raises(ValueError):m.once(tmp_path,'image_D',{},lambda:None)
