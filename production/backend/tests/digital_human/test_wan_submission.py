"""Wan single-attempt orchestration, offline provider and storage fixtures only."""
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app import jobs, storage as st
from app.digital_human import cost, submission as sub
from app.digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected
from dh_helpers import allow_billable, binary_response, make_mp4, prepare_work

PROMPT = "人物面对镜头自然讲述，声音温柔，街道有轻微风声。"


@pytest.fixture
def wan():
    assert importlib.util.find_spec("app.digital_human.wan") is not None, "Wan orchestration is missing"
    return importlib.import_module("app.digital_human.wan")


def authorize(work, wan):
    listing = wan.current_call_list(work["id"], prompt=PROMPT)
    est = cost.estimate("WAN_3_0", duration_seconds=5, resolution="720p")
    return cost.confirm_cost(work["id"], route="WAN_3_0", request_digest=listing["digest"],
                             estimated_fen=est["estimated_fen"], ceiling_fen=est["ceiling_fen"],
                             actor_id="user_fixture", call_list=listing)


@pytest.fixture
def ready(wan, monkeypatch):
    work = prepare_work(route="WAN_3_0", audio_ms=9000)
    allow_billable(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_QWEN_API_KEY", "fixture-wan-secret")
    monkeypatch.setenv("AVATAR_STUDIO_WAN_BASE_URL", "https://fixture.cn-beijing.maas.aliyuncs.com/api/v1")
    monkeypatch.setattr(wan, "prepare_native_media", lambda *_args, **_kw: {
        "image_url": "https://media.example/image.png"})
    monkeypatch.setattr(wan, "prepare_original", lambda run, downloaded: dict(downloaded,
        audio_restoration={"raw_file_hash": downloaded["probe"]["file_hash"],
                           "method": "offline_fixture", "lip_sync_verified": False}), raising=False)
    from app.digital_human import wan_native
    def prepare_native(run, downloaded):
        # Container fixtures isolate orchestration. Actual decoding has separate native-output tests.
        probe = downloaded["probe"]
        if not probe.get("has_audio_track"):
            raise TalkingHeadRejected("WAN_OUTPUT_AUDIO_MISSING", "fixture missing native audio")
        if abs(probe["duration_ms"] - run["provider_params"]["generated_seconds"] * 1000) > 250:
            raise TalkingHeadRejected("WAN_NATIVE_DURATION_MISMATCH", "fixture native duration mismatch")
        return dict(downloaded, native_audio={"generation_mode": "image_prompt_native_audio",
            "sound_mode": "native_audio", "raw_storage_uri": f"wan3/{run['id']}/raw.mp4",
            "raw_file_hash": probe["file_hash"], "bytes_preserved": True, "audio_replaced": False})
    monkeypatch.setattr(wan_native, "prepare_original", prepare_native)
    authorize(work, wan)
    return work, st.get("tasks", work["current_task_id"])


def submit(wan, ready, transport, **kwargs):
    work, task = ready
    return wan.submit_wan(work["id"], task=task, prompt=PROMPT, transport=transport, dry_run=False, **kwargs)


def accepted():
    return {"status_code": 200, "json": {"output": {"task_id": "original-wan-task", "task_status": "PENDING"}}}


def test_call_digest_binds_prompt_quality_source_and_integer_duration(wan, ready):
    work, _ = ready
    first = wan.current_call_list(work["id"], prompt=PROMPT)
    assert first["model"] == "wan3.0-video"
    assert first["generated_seconds"] == 5
    assert first["input_mode"] == "reference_image"
    assert first["audio"] is True
    assert first["prompt"] == PROMPT
    assert first["effective_prompt"] == PROMPT
    assert first["generation_mode"] == "image_prompt_native_audio"
    for changes in ({"prompt": PROMPT + "微笑"}, {"resolution": "1080p"}, {"duration_seconds": 30}):
        assert wan.current_call_list(work["id"], **dict({"prompt": PROMPT}, **changes))["digest"] != first["digest"]


def test_intent_and_budget_precede_single_external_post(wan, ready):
    calls = []
    def transport(req):
        runs = sub.runs_for_work(ready[0]["id"])
        assert len(runs) == 1 and runs[0]["state"] == "submitted"
        assert jobs.get_job(runs[0]["job_id"])["state"] == "leased"
        calls.append(req)
        return accepted()
    first = submit(wan, ready, transport)
    again = submit(wan, ready, transport)
    assert first["submitted"] is True
    assert again["runs"][0]["id"] == first["runs"][0]["id"]
    assert len(calls) == 1
    saved = json.dumps(sub.get_run(first["runs"][0]["id"]))
    assert "fixture-wan-secret" not in saved and "Authorization" not in saved


@pytest.mark.parametrize("response", [None, {"status_code": 503}, {"status_code": 200, "json": {"output": []}}])
def test_ambiguous_submission_is_persisted_unknown_and_never_reposted(wan, ready, response):
    calls = []
    def transport(req):
        calls.append(req)
        if response is None:
            raise TimeoutError("fixture-wan-secret")
        return response
    with pytest.raises(TalkingHeadBlocked) as raised:
        submit(wan, ready, transport)
    assert raised.value.code == "WAN_SUBMISSION_UNKNOWN"
    run = sub.runs_for_work(ready[0]["id"])[-1]
    assert run["state"] == "unknown"
    assert jobs.get_job(run["job_id"])["state"] == "unknown"
    assert "fixture-wan-secret" not in json.dumps(run)
    with pytest.raises(TalkingHeadBlocked, match="对账"):
        submit(wan, ready, transport)
    assert len(calls) == 1


def test_explicit_auth_failure_preserved_and_requires_fresh_confirmation(wan, ready):
    with pytest.raises(TalkingHeadBlocked) as raised:
        submit(wan, ready, lambda req: {"status_code": 401})
    assert raised.value.code == "WAN_AUTH_FAILED"
    original = sub.runs_for_work(ready[0]["id"])[-1]
    assert original["state"] == "failed"
    with pytest.raises(TalkingHeadBlocked):
        submit(wan, ready, lambda req: accepted(), replacement_authorized=True)
    authorize(ready[0], wan)
    replacement = submit(wan, ready, lambda req: accepted(), replacement_authorized=True)
    assert replacement["runs"][0]["id"] != original["id"]
    assert sub.get_run(original["id"]) == original


def test_poll_uses_saved_endpoint_and_original_id_then_registers_unreviewed_original(wan, ready, monkeypatch):
    run = submit(wan, ready, lambda req: accepted())["runs"][0]
    monkeypatch.setenv("AVATAR_STUDIO_WAN_BASE_URL", "https://changed.cn-beijing.maas.aliyuncs.com/api/v1")
    calls = []
    def transport(req):
        calls.append(req)
        if "/tasks/" in req["url"]:
            assert req["method"] == "GET"
            assert req["url"] == "https://fixture.cn-beijing.maas.aliyuncs.com/api/v1/tasks/original-wan-task"
            return {"status_code": 200, "json": {"output": {"task_status": "SUCCEEDED", "video_url": "https://media.example/result.mp4"}}}
        assert not req.get("headers", {}).get("Authorization")
        return binary_response(make_mp4(duration_ms=5000, audio=True))
    result = wan.poll_wan_run(run["id"], transport=transport)
    assert result["state"] == "succeeded"
    assert result["video"]["approval_state"] != "approved"
    assert result["video"]["route"] == "WAN_3_0"
    assert result["video"]["native_audio"]["bytes_preserved"] is True
    assert result["video"]["native_audio"]["audio_replaced"] is False
    assert result["video"].get("audio_asset_id") is None
    assert len(calls) == 2


def test_query_transport_loss_and_download_expiry_keep_original_task(wan, ready):
    run = submit(wan, ready, lambda req: accepted())["runs"][0]
    def lost(req):
        raise TimeoutError("private")
    result = wan.poll_wan_run(run["id"], transport=lost)
    assert result["state"] == "waiting_provider" and result["transport_unavailable"]
    def expired(req):
        if "/tasks/" in req["url"]:
            return {"status_code": 200, "json": {"output": {"task_status": "SUCCEEDED", "video_url": "https://media.example/expired.mp4"}}}
        return {"status_code": 403}
    with pytest.raises(TalkingHeadBlocked):
        wan.poll_wan_run(run["id"], transport=expired)
    saved = sub.get_run(run["id"])
    assert saved["state"] == "waiting_provider"
    assert saved["remote_job_id"] == "original-wan-task"


@pytest.mark.parametrize("audio,duration", [(False, 5000), (True, 4000)])
def test_native_output_requires_sound_and_confirmed_generation_duration(wan, ready, audio, duration):
    run = submit(wan, ready, lambda req: accepted())["runs"][0]
    def transport(req):
        if "/tasks/" in req["url"]:
            return {"status_code": 200, "json": {"output": {"task_status": "SUCCEEDED", "video_url": "https://media.example/result.mp4"}}}
        return binary_response(make_mp4(duration_ms=duration, audio=audio))
    with pytest.raises((TalkingHeadBlocked, TalkingHeadRejected)):
        wan.poll_wan_run(run["id"], transport=transport)
    assert sub.get_run(run["id"])["state"] != "succeeded"


def test_media_checks_actual_audio_duration_before_publishing(monkeypatch, tmp_path):
    from pathlib import Path
    from dh_helpers import _wav_bytes
    assert importlib.util.find_spec("app.digital_human.wan_media") is not None
    media = importlib.import_module("app.digital_human.wan_media")
    from app.digital_human import omni_media
    audio = Path(tmp_path) / "full.wav"
    audio.write_bytes(_wav_bytes(duration_ms=16000))
    monkeypatch.setattr(omni_media, "_local_media_path", lambda *_: audio)
    published = []
    monkeypatch.setattr(omni_media, "prepare_media", lambda *a, **k: published.append(1))
    with pytest.raises((TalkingHeadBlocked, TalkingHeadRejected)) as raised:
        media.prepare_media({"storage_uri": "image.png", "asset_hash": "fixture"},
                            {"storage_uri": "full.wav", "file_hash": "fixture", "duration_ms": 9000})
    assert raised.value.code in ("WAN_AUDIO_TOO_LONG", "WAN_AUDIO_DURATION_MISMATCH")
    assert published == []


def test_concurrent_submit_has_one_external_post(wan, ready):
    entered, release = Event(), Event()
    calls = []
    def transport(req):
        calls.append(req)
        entered.set()
        assert release.wait(5)
        return accepted()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit, wan, ready, transport)
        assert entered.wait(5)
        second = submit(wan, ready, transport)
        release.set()
        original = first.result(timeout=5)
    assert second["runs"][0]["id"] == original["runs"][0]["id"]
    assert len(calls) == 1


def test_unknown_replacement_uses_new_authorization_and_preserves_old_run(wan, ready):
    with pytest.raises(TalkingHeadBlocked):
        submit(wan, ready, lambda req: {"status_code": 503})
    original = sub.runs_for_work(ready[0]["id"])[-1]
    authorize(ready[0], wan)
    calls = []
    def transport(req):
        calls.append(req)
        return accepted()
    replacement = submit(wan, ready, transport, replacement_authorized=True)
    repeated = submit(wan, ready, transport, replacement_authorized=True)
    assert replacement["runs"][0]["id"] != original["id"]
    assert repeated["runs"][0]["id"] == replacement["runs"][0]["id"]
    assert sub.get_run(original["id"]) == original
    assert len(calls) == 1


def test_remote_failure_settles_both_run_and_job(wan, ready):
    run = submit(wan, ready, lambda req: accepted())["runs"][0]
    result = wan.poll_wan_run(run["id"], transport=lambda req: {"status_code": 200, "json": {
        "output": {"task_id": run["remote_job_id"], "task_status": "FAILED"}}})
    assert result["state"] == "failed"
    assert jobs.get_job(run["job_id"])["state"] == "failed"


@pytest.mark.parametrize("size,mode", [((239, 320), "RGB"), ((8001, 300), "RGB"),
                                      ((240, 2000), "RGB"), ((320, 320), "RGBA")])
def test_illegal_image_is_blocked_before_publishing(monkeypatch, tmp_path, size, mode):
    from pathlib import Path
    from PIL import Image
    from dh_helpers import _wav_bytes
    from app.digital_human import wan_media as media, omni_media
    audio = Path(tmp_path) / "full.wav"
    image = Path(tmp_path) / "image.png"
    audio.write_bytes(_wav_bytes(duration_ms=9000))
    Image.new(mode, size).save(image)
    monkeypatch.setattr(omni_media, "_local_media_path", lambda field, *_: audio if field == "audio_url" else image)
    published = []
    monkeypatch.setattr(omni_media, "prepare_media", lambda *a, **k: published.append(1))
    with pytest.raises((TalkingHeadBlocked, TalkingHeadRejected)) as raised:
        media.prepare_media({"storage_uri": "image.png", "asset_hash": "fixture"},
                            {"storage_uri": "full.wav", "file_hash": "fixture", "duration_ms": 9000})
    assert raised.value.code == "WAN_IMAGE_INVALID"
    assert published == []


def test_complete_audio_decode_failure_stops_before_publish(monkeypatch, tmp_path):
    from pathlib import Path
    from PIL import Image
    from dh_helpers import _wav_bytes
    from app.digital_human import wan_media as media, omni_media
    from app.video import ffmpeg
    from app.video.errors import VideoBlocked
    audio = Path(tmp_path) / "full.wav"
    image = Path(tmp_path) / "image.png"
    audio.write_bytes(_wav_bytes(duration_ms=9000))
    Image.new("RGB", (320, 320)).save(image)
    monkeypatch.setattr(omni_media, "_local_media_path", lambda field, *_: audio if field == "audio_url" else image)
    def broken(path):
        raise VideoBlocked("DECODE_ERRORS", "fixture audio decoding error")
    monkeypatch.setattr(ffmpeg, "full_decode_check", broken)
    with pytest.raises((TalkingHeadBlocked, TalkingHeadRejected)) as raised:
        media.prepare_media({"storage_uri": "image.png", "asset_hash": "fixture"},
                            {"storage_uri": "full.wav", "file_hash": "fixture", "duration_ms": 9000})
    assert raised.value.code == "WAN_AUDIO_UNVERIFIABLE"


def test_mp3_encoder_padding_does_not_change_complete_audio_or_cause_false_mismatch(monkeypatch, tmp_path):
    from pathlib import Path
    from PIL import Image
    from app.digital_human import wan_media as media, omni_media
    from app.video import ffmpeg
    audio = Path(tmp_path) / "full.mp3"
    image = Path(tmp_path) / "image.png"
    Image.new("RGB", (320, 320)).save(image)
    cp = ffmpeg.run_ffmpeg(["-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=9",
                            "-c:a", "libmp3lame", "-y", str(audio)])
    assert cp.returncode == 0
    original = audio.read_bytes()
    real_run = ffmpeg._run
    def padded_container(cmd, **kwargs):
        response = real_run(cmd, **kwargs)
        if "-show_format" in cmd:
            doc = json.loads(response.stdout)
            doc["format"]["duration"] = "9.050000"
            response.stdout = json.dumps(doc).encode()
        return response
    monkeypatch.setattr(ffmpeg, "_run", padded_container)
    monkeypatch.setattr(omni_media, "_local_media_path", lambda field, *_: audio if field == "audio_url" else image)
    monkeypatch.setattr(omni_media, "prepare_media", lambda *a, **k: {"image_url": "https://media.example/i.png", "audio_url": "https://media.example/a.mp3"})
    # Use the provider constants: only the publication transport is replaced.
    from app.digital_human import providers
    monkeypatch.setenv(providers.SECRET_ENV_AK, "fixture")
    monkeypatch.setenv(providers.SECRET_ENV_SK, "fixture")
    result = media.prepare_media({"storage_uri": "image.png", "asset_hash": "fixture"},
                                 {"storage_uri": "full.mp3", "file_hash": "fixture", "duration_ms": 9000})
    assert result["actual_audio_duration_ms"] == 9000
    assert audio.read_bytes() == original


def test_generation_duration_change_blocks_before_paid_post(wan, ready, monkeypatch):
    calls = []
    with pytest.raises(TalkingHeadRejected) as raised:
        submit(wan, ready, lambda req: calls.append(req), duration_seconds=6)
    assert raised.value.code == "COST_LIST_CHANGED"
    assert calls == []


def test_preflight_source_changed_before_listing_never_submits_wrong_image(wan, ready, monkeypatch):
    original = wan.routes.require_preflight
    def stale(*args, **kwargs):
        preflight = original(*args, **kwargs)
        preflight["source"] = dict(preflight["source"], asset_hash="sha256:" + "9" * 64)
        return preflight
    monkeypatch.setattr(wan.routes, "require_preflight", stale)
    calls = []
    with pytest.raises(TalkingHeadRejected) as raised:
        submit(wan, ready, lambda req: calls.append(req) or accepted())
    assert raised.value.code == "COST_LIST_CHANGED"
    assert calls == []


def test_changed_input_during_media_publish_stops_before_provider_post(wan, ready, monkeypatch):
    from app.digital_human import plate_source
    resolve = plate_source.resolve_plate_source
    def mutate(*args, **kwargs):
        monkeypatch.setattr(plate_source, "resolve_plate_source", lambda work:
                            dict(resolve(work), asset_hash="sha256:" + "7" * 64))
        return {"image_url": "https://media.example/i.png"}
    monkeypatch.setattr(wan, "prepare_native_media", mutate)
    calls = []
    with pytest.raises(TalkingHeadRejected) as raised:
        submit(wan, ready, lambda req: calls.append(req) or accepted())
    assert raised.value.code == "COST_LIST_CHANGED"
    assert calls == []


def test_query_response_for_different_task_cannot_register_result(wan, ready):
    run = submit(wan, ready, lambda req: accepted())["runs"][0]
    requests = []
    def wrong(req):
        requests.append(req)
        return {"status_code": 200, "json": {"output": {"task_id": "another-task", "task_status": "FAILED"}}}
    result = wan.poll_wan_run(run["id"], transport=wrong)
    assert result["state"] == "waiting_provider" and result["transport_unavailable"] is True
    assert len(requests) == 1
    assert sub.get_run(run["id"])["remote_job_id"] == "original-wan-task"


@pytest.mark.parametrize("changed", ["route", "task", "confirmation"])
def test_selection_change_while_preparing_media_blocks_paid_post(wan, ready, monkeypatch, changed):
    def mutate(*args, **kwargs):
        work, task = ready
        if changed == "confirmation":
            current = st.get("tasks", task["id"])
            current["route_confirmed"] = False
            st.put("tasks", current["id"], current, {"work_id": work["id"]})
        else:
            current = st.get("works", work["id"])
            current["route" if changed == "route" else "current_task_id"] = "M2" if changed == "route" else "new-task"
            st.put("works", work["id"], current)
        return {"image_url": "https://media.example/i.png", "audio_url": "https://media.example/a.wav"}
    monkeypatch.setattr(wan, "prepare_native_media", mutate)
    calls = []
    with pytest.raises((TalkingHeadBlocked, TalkingHeadRejected)):
        submit(wan, ready, lambda req: calls.append(req) or accepted())
    assert calls == []


@pytest.mark.parametrize("changed", ["source", "route", "source_missing"])
def test_returned_old_source_or_route_remains_historical_not_current(wan, ready, monkeypatch, changed):
    run = submit(wan, ready, lambda req: accepted())["runs"][0]
    if changed in ("source", "source_missing"):
        from app.digital_human import plate_source
        old = plate_source.resolve_plate_source
        def changed_source(work):
            if changed == "source_missing":
                raise TalkingHeadBlocked("SOURCE_NOT_FOUND", "fixture missing source")
            return dict(old(work), asset_hash="sha256:" + "6" * 64)
        monkeypatch.setattr(plate_source, "resolve_plate_source", changed_source)
    else:
        work = st.get("works", ready[0]["id"])
        work["route"] = "M2"
        st.put("works", work["id"], work)
    def transport(req):
        if "/tasks/" in req["url"]:
            return {"status_code": 200, "json": {"output": {"task_status": "SUCCEEDED", "video_url": "https://media.example/result.mp4"}}}
        return binary_response(make_mp4(duration_ms=5000))
    result = wan.poll_wan_run(run["id"], transport=transport)
    assert result["state"] == "succeeded"
    assert result["video"]["approval_state"] == "revoked"
    assert result["video"]["source_asset_hash"] == run["provider_params"]["input_snapshot"]["source_asset_hash"]


@pytest.mark.parametrize("late_status", ["RUNNING", "UNKNOWN", "FAILED"])
def test_late_status_cannot_overwrite_success_recorded_during_transport(wan, ready, late_status):
    from app.digital_human import providers, wan_native
    run = submit(wan, ready, lambda req: accepted())["runs"][0]
    completed = {}
    def transport(req):
        downloaded = providers.download_and_verify("https://media.example/raw.mp4",
            lambda _: binary_response(make_mp4(duration_ms=5000)))
        completed.update(wan_native.register_original(run["id"], downloaded))
        return {"status_code": 200, "json": {"output": {
            "task_id": run["remote_job_id"], "task_status": late_status}}}
    result = wan.poll_wan_run(run["id"], transport=transport)
    assert result["state"] == "succeeded"
    assert result["run"]["result_asset_ids"] == [completed["video"]["id"]]
    assert sub.get_run(run["id"])["state"] == "succeeded"
    assert jobs.get_job(run["job_id"])["state"] == "succeeded"


@pytest.mark.parametrize("late_status", ["RUNNING", "UNKNOWN", "FAILED"])
def test_same_run_polls_are_serial_and_never_send_after_terminal_result(wan, ready, late_status):
    run = submit(wan, ready, lambda req: accepted())["runs"][0]
    success_entered, release_success, stale_entered = Event(), Event(), Event()
    def success(req):
        if "/tasks/" in req["url"]:
            success_entered.set()
            assert release_success.wait(5)
            return {"status_code": 200, "json": {"output": {
                "task_status": "SUCCEEDED", "video_url": "https://media.example/result.mp4"}}}
        return binary_response(make_mp4(duration_ms=5000))
    def stale(req):
        stale_entered.set()
        return {"status_code": 200, "json": {"output": {
            "task_id": run["remote_job_id"], "task_status": late_status}}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(wan.poll_wan_run, run["id"], transport=success)
        assert success_entered.wait(5)
        second = pool.submit(wan.poll_wan_run, run["id"], transport=stale)
        was_parallel = stale_entered.wait(.15)
        release_success.set()
        first_result, second_result = first.result(timeout=5), second.result(timeout=5)
    assert was_parallel is False, "the second poll must not issue a GET while the first owns this run"
    assert first_result["state"] == second_result["state"] == "succeeded"
    assert first_result["run"]["result_asset_ids"] == second_result["run"]["result_asset_ids"]
    assert stale_entered.is_set() is False
