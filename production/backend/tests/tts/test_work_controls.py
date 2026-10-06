"""作品语音参数的请求、缓存和失效契约；供应商仅使用离线 transport。"""
import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import lineage, storage as st
from app.main import app
from app.digital_human import gates as video_gate
from app.digital_human.errors import TalkingHeadRejected
from app.tts import audio as au, compiler as cp, gate, plan, preview, provider, synthesis, voices
from app.tts.errors import TtsRejected
from tts_helpers import allow_billable, make_transport, new_work, ok_body


CONTROLS = {"emotion": "happy", "speed": 1.3, "vol": 2.0, "pitch": -3,
            "voice_modify": {"pitch": 12, "intensity": -10, "timbre": 30}}


def save(work, controls):
    result = TestClient(app).put(f"/api/v1/works/{work['id']}/tts-controls",
                                 json={"controls": controls})
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.parametrize("profile", [
    {"speed": 0.49}, {"speed": 2.01}, {"speed": True}, {"speed": float("nan")},
    {"speed": "1.2"}, {"vol": 0}, {"vol": 10.01}, {"pitch": 1.2}, {"pitch": 13},
    {"pitch": False}, {"emotion": "neutral"}, {"emotion": "whisper"},
    {"voice_modify": {"timbre": 101}}, {"voice_modify": {"pitch": 0.5}},
    {"voice_modify": {"intensity": True}}, {"voice_modify": {"unknown": 2}},
])
def test_profile_rejects_invalid_scalar_values(profile):
    with pytest.raises(TtsRejected):
        voices.normalize_profile(profile)


def test_auto_emotion_omitted_and_voice_modify_is_top_level(voice):
    params = {**CONTROLS, "emotion": "auto"}
    body = provider.build_request(voice=voice, provider_params=params, text="短句。")['json']
    assert "emotion" not in body["voice_setting"]
    assert body["voice_modify"] == CONTROLS["voice_modify"]
    assert body["voice_setting"]["pitch"] == -3


def test_controls_persist_per_work_and_preserve_voice(work, voice):
    other = new_work()
    before = copy.deepcopy(voice)
    saved = save(work, CONTROLS)
    assert saved["tts_controls"] == CONTROLS
    assert st.get("works", other["id"]).get("tts_controls", {}) == {}
    assert voices.get_voice(voice['id']) == before
    response = TestClient(app).get(f"/api/v1/works/{work['id']}/tts-controls")
    assert response.status_code == 200
    assert response.json()['controls']['emotion'] == 'happy'
    assert response.json()['digest'].startswith('sha256:')


def test_preview_and_full_send_same_work_controls(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    save(work, CONTROLS)
    transport, calls = make_transport(body=ok_body(duration_ms=1800))
    p = preview.create_preview(work_id=work['id'], voice_id=voice['id'],
                               sample_text='参数一致测试。', transport=transport)
    full = synthesis.synthesize_full(work_id=work['id'], transport=transport)
    assert calls[0]['json']['voice_setting'] == calls[1]['json']['voice_setting']
    assert calls[1]['json']['voice_setting']['emotion'] == 'happy'
    assert calls[1]['json']['voice_setting']['speed'] == 1.3
    assert calls[1]['json']['voice_modify'] == CONTROLS['voice_modify']
    assert all(s['emotion_applied'] == 'happy' and s['speed'] == 1.3
               for s in full['compiled']['segments'])
    assert p['audio']['tts_controls_digest'] == full['audio']['tts_controls_digest']
    assert full['audio']['synthesis_profile']['emotion'] == 'happy'


def test_cached_preview_is_owned_by_requesting_work(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    other = new_work()
    save(work, CONTROLS)
    save(other, CONTROLS)
    transport, calls = make_transport(body=ok_body())
    first = preview.create_preview(work_id=work['id'], voice_id=voice['id'],
                                    sample_text='缓存所属作品测试。', transport=transport)
    second = preview.create_preview(work_id=other['id'], voice_id=voice['id'],
                                     sample_text='缓存所属作品测试。', transport=transport)
    assert second['cached']
    assert len(calls) == 1
    assert second['audio']['work_id'] == other['id']
    assert second['audio']['id'] != first['audio']['id']
    save(other, {**CONTROLS, 'speed': 1.5})
    third = preview.create_preview(work_id=other['id'], voice_id=voice['id'], transport=transport)
    assert not third['cached']
    assert len(calls) == 2


def test_cross_work_cache_rejects_changed_local_copy(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    other = new_work()
    transport, calls = make_transport(body=ok_body())
    preview.create_preview(work_id=work['id'], voice_id=voice['id'],
                            sample_text='本地缓存完整性。', transport=transport)
    cached = preview.create_preview(work_id=other['id'], voice_id=voice['id'],
                                    sample_text='本地缓存完整性。', transport=transport)
    Path(au.runtime_path(cached['audio']['storage_uri'])).write_bytes(b'changed')
    refreshed = preview.create_preview(work_id=other['id'], voice_id=voice['id'], transport=transport)
    data = Path(au.runtime_path(refreshed['audio']['storage_uri'])).read_bytes()
    assert provider.file_hash(data) == refreshed['audio']['file_hash']
    assert len(calls) == 1


def test_control_change_invalidates_history_and_cannot_reconfirm(monkeypatch, work):
    allow_billable(monkeypatch)
    transport, calls = make_transport(body=ok_body())
    full = synthesis.synthesize_full(work_id=work['id'], transport=transport)['audio']
    gate.confirm_final_audio(work['id'], full['id'], actor_id='user', audition_ack=True)
    video_id = st.new_id('video')
    st.put('videos', video_id, {'id': video_id, 'work_id': work['id'],
                               'kind': 'original', 'confirmed': True, 'approval_state': 'approved',
                               'has_video_track': True, 'file_hash': 'sha256:verified_video',
                               'duration_ms': 1000, 'audio_asset_id': full['id']},
           {'work_id': work['id']})
    st.put('video_reviews', video_id, {'video_hash': 'sha256:verified_video', 'outcome': 'pass',
           'dimensions': {key: 'pass' for key in video_gate.DIMENSIONS}}, {'video_id': video_id})
    st.put('audio_track_checks', video_id, {'video_hash': 'sha256:verified_video', 'outcome': 'pass',
           'audio_asset_id': full['id']}, {'video_id': video_id})
    assert video_gate.video_gate(video_id)['ok']
    w = st.get('works', work['id'])
    w['video_asset_id'] = video_id
    st.put('works', work['id'], w)
    for kind in ('subtitle', 'video', 'package'):
        lineage.record(artifact_id=f'{kind}-{work["id"]}', kind=kind,
                       work_id=work['id'], digest='sha256:test')
    saved = save(work, CONTROLS)
    for key in ('final_audio_id', 'final_audio_duration_ms', 'performance_plan_id', 'video_asset_id'):
        assert saved[key] is None
    assert au.get_audio(full['id'])['invalidated']
    assert not st.get('videos', video_id)['confirmed']
    with pytest.raises(TalkingHeadRejected):
        video_gate.confirm_final_video(work['id'], video_id, actor_id='user', review_ack=True)
    assert all(r['invalidated_at'] for r in lineage.rows_for_work(work['id']))
    with pytest.raises(TtsRejected):
        gate.confirm_final_audio(work['id'], full['id'], actor_id='user', audition_ack=True)
    fresh = synthesis.synthesize_full(work_id=work['id'], transport=transport)['audio']
    assert fresh['id'] != full['id']
    gate.confirm_final_audio(work['id'], fresh['id'], actor_id='user', audition_ack=True)
    save(work, CONTROLS)
    assert au.get_audio(fresh['id'])['confirmed']
    assert len(calls) == 2


def test_late_synthesis_after_control_change_is_preserved_but_invalid(monkeypatch, work):
    allow_billable(monkeypatch)
    def transport(request):
        save(work, CONTROLS)
        return {'status_code': 200, 'body': ok_body(), 'raw': b''}
    result = synthesis.synthesize_full(work_id=work['id'], transport=transport)
    assert result['audio']['invalidated']
    assert result['audio']['storage_uri']
    with pytest.raises(TtsRejected):
        gate.confirm_final_audio(work['id'], result['audio']['id'], actor_id='user', audition_ack=True)


def test_frozen_plan_captures_controls_and_old_plan_rejected(work):
    old = plan.ensure_plan(work['id'])
    save(work, CONTROLS)
    latest = plan.ensure_plan(work['id'])
    assert latest['id'] != old['id']
    assert latest['revision_of'] == old['id']
    assert latest['tts_controls_digest']
    with pytest.raises(TtsRejected):
        cp.compile_speech(old['id'], work['voice_asset_id'])


def test_pending_clone_baseline_uses_actual_work_audition(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    clone_id = st.new_id('voice_clone')
    clone = {**voice, 'id': clone_id, 'approval_state': 'pending', 'is_preset': False,
             'remote_voice_id': clone_id, 'baseline_audio_asset_id': None, 'baseline_audio_hash': '',
             'sample_text': '克隆默认短句。', 'clone_metadata': {'activation_state': 'active'}}
    st.put('voices', clone_id, clone)
    preview.resolve_audition_sentence(voice, work['id'], '作品之前已经使用的短句。')
    save(work, CONTROLS)
    # Provider生命周期另有注入transport的合同测试；这里只替换克隆计费状态检查。
    from app.tts import cloning
    monkeypatch.setattr(cloning, 'synthesis_charge', lambda *_: 0)
    monkeypatch.setattr(cloning, 'begin_activation', lambda *_: None)
    monkeypatch.setattr(cloning, 'mark_activated', lambda *_: None)
    transport, _ = make_transport(body=ok_body())
    result = preview.create_preview(work_id=work['id'], voice_id=clone_id, transport=transport)
    baseline = voices.attach_baseline_from_preview(clone_id, work['id'])
    assert baseline['voice']['sample_text'] == '作品之前已经使用的短句。'
    assert baseline['voice']['synthesis_profile'] == result['audio']['synthesis_profile']
    assert baseline['source_audio_id'] == result['audio']['id']
    assert voices.approve_voice(clone_id)['approval_state'] == 'approved'


def test_changed_and_reverted_controls_do_not_reactivate_late_audio(monkeypatch, work):
    allow_billable(monkeypatch)
    def transport(_):
        save(work, CONTROLS)
        save(work, {})
        return {'status_code': 200, 'body': ok_body(), 'raw': b''}
    result = synthesis.synthesize_full(work_id=work['id'], transport=transport)
    assert result['audio']['invalidated']
    assert result['audio']['tts_controls_revision'] == 0
    assert st.get('works', work['id'])['tts_controls_revision'] == 2


def test_preview_records_actual_sentence_for_baseline(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body())
    result = preview.create_preview(work_id=work['id'], voice_id=voice['id'],
                                    sample_text='实际合成的短句。', transport=transport)
    assert result['audio']['sample_text'] == '实际合成的短句。'
