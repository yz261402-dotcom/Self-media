"""已选人物原图与已确认配音必须真正变成供应商可读输入，不更改本地资产记录。"""
import hashlib
from pathlib import Path

import pytest

from app import storage as st
from app.avatar import licensing, registry
from app.contracts import HERO_ONLY_LIMITATIONS
from app.digital_human import publish, routes, submission
from app.digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected
from app.tts import audio as tts_audio
from dh_helpers import (HASH_B, allow_billable, authorize_cost, install_final_audio,
                        make_transport, prepare_work, volc_submit_ok)


def sha(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


@pytest.fixture()
def local_work(tmp_path, monkeypatch):
    root = Path(tmp_path)
    monkeypatch.setenv('AVATAR_STUDIO_WORKSPACE', str(root))
    monkeypatch.setenv('AVATAR_STUDIO_VOLC_ACCESS_KEY', 'test-ak')
    monkeypatch.setenv('AVATAR_STUDIO_VOLC_SECRET_KEY', 'test-sk')
    allow_billable(monkeypatch)
    work = prepare_work(with_variant=False)
    ident = registry.create_identity_candidate('本地原图测试', '写实主播', origin='generated')
    lic = licensing.register(subject=ident['id'], kind='image_source', scope='commercial_ok',
                             evidence='测试夹具', verified_by='user_test')
    image = root / 'assets' / 'hero.png'
    image.parent.mkdir(parents=True)
    image.write_bytes(b'local-hero-bytes')
    hero = registry.register_asset(ident['id'], 'hero', storage_uri='assets/hero.png',
                                   file_hash=sha(image.read_bytes()), source_evidence='本地测试',
                                   license_id=lic['id'])
    registry.record_review(hero['id'], reviewer='user_test', outcome='pass',
                           identity_digest=HASH_B, method='manual')
    registry.set_reference_policy(ident['id'], 'hero_only', 'user_test', reason='测试',
                                  acknowledged=HERO_ONLY_LIMITATIONS)
    registry.freeze_identity(ident['id'], hero['id'], HASH_B,
                             reviewer='user_test', license_id=lic['id'])
    work['identity_id'] = ident['id']
    st.put('works', work['id'], work)
    uri = f"audio/{work['id']}/final.wav"
    path = root / 'production/backend/runtime' / uri
    path.parent.mkdir(parents=True)
    path.write_bytes(b'local-final-audio-bytes')
    audio = install_final_audio(work['id'], storage_uri=uri, file_hash=sha(path.read_bytes()))
    authorize_cost(work['id'])
    return work, hero, audio, image, path


@pytest.fixture()
def published(monkeypatch):
    objects, uploads, checks = {}, [], []
    class Publisher:
        def __init__(self, *args, **kwargs):
            pass
        def exists(self, key):
            return key in objects
        def ensure_bucket(self):
            pass
        def publish_file(self, path, key=None, content_type=None):
            objects[key] = Path(path).read_bytes()
            uploads.append((key, content_type))
            return self.url_for(key)
        def url_for(self, key):
            return 'https://bucket.tos-cn-beijing.volces.com/' + key
        def verify_public_file(self, url, expected_hash):
            key = url.split('.com/', 1)[1]
            checks.append(key)
            if sha(objects[key]) != expected_hash:
                raise ValueError('wrong bytes')
    monkeypatch.setattr(publish, 'TosPublisher', Publisher)
    return objects, uploads, checks


def submit(work, transport, **kwargs):
    return submission.submit_omnihuman(work['id'], task=st.get('tasks', work['current_task_id']),
                                      transport=transport, dry_run=False, **kwargs)


def test_local_hero_and_audio_are_uploaded_and_original_uris_preserved(local_work, published):
    work, hero, audio, _, _ = local_work
    transport, calls = make_transport(default=volc_submit_ok())
    out = submit(work, transport)
    assert out['submitted'] is True
    assert len(calls) == 1
    assert calls[0]['json']['image_url'].startswith('https://bucket.')
    assert calls[0]['json']['audio_url'].startswith('https://bucket.')
    assert hero['file_hash'][7:] in calls[0]['json']['image_url']
    assert audio['file_hash'][7:] in calls[0]['json']['audio_url']
    assert len(published[1]) == len(published[2]) == 2
    assert registry.get_asset(hero['id'])['storage_uri'] == hero['storage_uri']
    assert tts_audio.get_audio(audio['id'])['storage_uri'] == audio['storage_uri']
    snap = out['runs'][0]['provider_params']['input_snapshot']
    assert snap['source_asset_id'] == hero['id']
    assert snap['source_asset_hash'] == hero['file_hash']
    assert snap['audio_hash'] == audio['file_hash']


def test_omni_preflight_resolves_approved_hero_without_variant(local_work):
    work, hero, *_ = local_work
    preflight = routes.preflight(work['id'], task=st.get('tasks', work['current_task_id']))
    assert preflight['ok']
    assert preflight['source']['asset_id'] == hero['id']


@pytest.mark.parametrize('bad', ['image_hash', 'audio_hash', 'missing_audio'])
def test_all_local_inputs_checked_before_upload_or_billable_submission(local_work, published, bad):
    work, _, _, image, audio_path = local_work
    if bad == 'image_hash':
        image.write_bytes(b'changed-image')
    elif bad == 'audio_hash':
        audio_path.write_bytes(b'changed-audio')
    else:
        audio_path.unlink()
    transport, calls = make_transport(default=volc_submit_ok())
    with pytest.raises(TalkingHeadBlocked) as error:
        submit(work, transport)
    assert error.value.code in ('OMNIHUMAN_MEDIA_HASH_MISMATCH', 'OMNIHUMAN_MEDIA_PATH_INVALID')
    assert published[1] == []
    assert calls == []
    assert not [r for r in submission.runs_for_work(work['id']) if r.get('job_id')]


def test_explicit_invalid_variant_does_not_silently_use_hero(local_work, published):
    work, *_ = local_work
    work['variant_id'] = 'var-missing'
    st.put('works', work['id'], work)
    transport, calls = make_transport(default=volc_submit_ok())
    with pytest.raises(TalkingHeadRejected) as error:
        submit(work, transport)
    assert error.value.code == 'VARIANT_NOT_FOUND'
    assert not calls and not published[1]


def test_upload_failure_is_actionable_and_does_not_start_paid_run(local_work, published, monkeypatch):
    work, *_ = local_work
    def fail(*args, **kwargs):
        raise RuntimeError('sensitive-sdk-message-do-not-display')
    monkeypatch.setattr(publish.TosPublisher, 'publish_file', fail)
    transport, calls = make_transport(default=volc_submit_ok())
    with pytest.raises(TalkingHeadBlocked) as error:
        submit(work, transport)
    assert error.value.code == 'OMNIHUMAN_MEDIA_UPLOAD_FAILED'
    assert 'sensitive-sdk' not in error.value.message
    assert not calls


def test_cached_object_is_checked_and_wrong_bytes_never_reach_provider(local_work, published):
    work, hero, *_ = local_work
    published[0][f"omnihuman/image/{hero['file_hash'][7:]}.png"] = b'wrong-object'
    transport, calls = make_transport(default=volc_submit_ok())
    with pytest.raises(TalkingHeadBlocked) as error:
        submit(work, transport)
    assert error.value.code == 'OMNIHUMAN_MEDIA_NOT_READABLE'
    assert not calls


def test_dry_run_never_uploads_or_submits(local_work, published):
    work, *_ = local_work
    transport, calls = make_transport(default=volc_submit_ok())
    out = submission.submit_omnihuman(work['id'], task=st.get('tasks', work['current_task_id']),
                                     transport=transport, dry_run=True)
    assert out['is_mock'] and not out['submitted']
    assert not calls and not published[1]


def test_hero_identity_and_source_are_bound_to_cost_and_idempotency(local_work, published):
    work, hero, *_ = local_work
    original = submission.current_call_list(work['id'], route='OMNIHUMAN_1_5')
    assert original.get('source_asset_id') == hero['id']
    transport, calls = make_transport(default=volc_submit_ok('first-avatar'))
    submit(work, transport)
    other = registry.create_identity_candidate('另一个基础人物', '写实主播', origin='generated')
    lic = licensing.register(subject=other['id'], kind='image_source', scope='commercial_ok',
                             evidence='测试夹具', verified_by='user_test')
    other_hero = registry.register_asset(other['id'], 'hero', storage_uri='https://cdn.example.com/other.png',
                                         file_hash=HASH_B, source_evidence='另一测试原图', license_id=lic['id'])
    registry.record_review(other_hero['id'], reviewer='user_test', outcome='pass',
                           identity_digest=HASH_B, method='manual')
    registry.set_reference_policy(other['id'], 'hero_only', 'user_test', reason='测试',
                                  acknowledged=HERO_ONLY_LIMITATIONS)
    registry.freeze_identity(other['id'], other_hero['id'], HASH_B,
                             reviewer='user_test', license_id=lic['id'])
    work['identity_id'] = other['id']
    st.put('works', work['id'], work)
    changed = submission.current_call_list(work['id'], route='OMNIHUMAN_1_5')
    assert changed['digest'] != original['digest']
    with pytest.raises(TalkingHeadRejected) as error:
        submit(work, transport)
    assert error.value.code == 'COST_LIST_CHANGED'
    assert len(calls) == 1, '旧确认不能提交新人物，也不能复用旧人物结果'
