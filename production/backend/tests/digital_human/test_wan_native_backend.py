"""Native Wan contracts and payment safety; local images and offline transport only."""
import hashlib
import importlib
from pathlib import Path

import pytest
from PIL import Image

from app import jobs, storage as st
from app.digital_human import cost, routes, submission, wan, wan_media, wan_provider, worker_client
from app.digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected
from dh_helpers import allow_billable, install_task

PROMPT = "小猫走过雨后街道，说‘你好’，伴随脚步和环境声音。"


@pytest.fixture
def native_work(tmp_path, monkeypatch):
    root = Path(tmp_path)
    image = root / "production/backend/runtime/wan_inputs/fixture/image.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (480, 720), "white").save(image)
    monkeypatch.setattr(worker_client, "_workspace_root", lambda: root)
    wid = st.new_id("native")
    source = {"work_id": wid, "asset_id": st.new_id("wanimage"), "kind": "wan_image",
              "asset_hash": "sha256:" + hashlib.sha256(image.read_bytes()).hexdigest(),
              "storage_uri": image.relative_to(root).as_posix(), "label": "小猫参考图",
              "identity_id": None, "variant_id": None}
    st.put("works", wid, {"id": wid, "name": "原生声音短片", "route": routes.ROUTE_WAN,
                          "wan_image": source})
    task = install_task(wid, route=routes.ROUTE_WAN)
    return st.get("works", wid), task, image


def test_native_preflight_needs_only_selected_route_and_uploaded_image(native_work, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("native route must not inspect TTS, script, plan or voice")
    monkeypatch.setattr(routes, "_final_audio", forbidden)
    monkeypatch.setattr(routes.tts_voices, "voice_ready", forbidden)
    work, task, _ = native_work
    result = routes.preflight(work["id"], task=task)
    assert result["ok"] is True
    assert result["source"]["asset_id"] == work["wan_image"]["asset_id"]
    assert result["audio_asset_id"] is None


def test_native_request_has_only_image_and_unmodified_prompt():
    build = getattr(wan_provider, "build_native_submit_request", None)
    assert callable(build), "native request builder is missing"
    request = build(image_url="https://media.example/image.png", prompt=PROMPT,
                    duration_seconds=30, resolution="720p", api_key="fixture-secret",
                    base_url="https://fixture.cn-beijing.maas.aliyuncs.com/api/v1")
    assert request["json"]["input"] == {"prompt": PROMPT, "media": [
        {"type": "reference_image", "url": "https://media.example/image.png"}]}
    assert request["json"]["parameters"] == {
        "resolution": "720P", "ratio": "9:16", "duration": 30, "audio": True}


@pytest.mark.parametrize("seconds", [1, 31, 2.1, True, "5", None])
def test_native_duration_rejects_non_integer_or_out_of_range(seconds):
    validate = getattr(wan_provider, "assert_native_input", None)
    assert callable(validate), "native duration validator is missing"
    with pytest.raises(TalkingHeadRejected) as error:
        validate(duration_seconds=seconds, prompt=PROMPT, resolution="720p")
    assert error.value.code == "WAN_DURATION_INVALID"


def test_native_digest_binds_duration_prompt_resolution_and_source(native_work):
    work, _, image = native_work
    first = wan.current_call_list(work["id"], prompt=PROMPT, duration_seconds=30)
    assert first["generation_mode"] == "image_prompt_native_audio"
    assert first["sound_mode"] == "native_audio"
    assert first["final_audio_mode"] == "provider_native_audio"
    assert first["input_mode"] == "reference_image"
    assert first["duration_seconds"] == first["generated_seconds"] == 30
    assert not first.get("audio_id") and not first.get("audio_hash")
    assert wan.current_call_list(work["id"], prompt=PROMPT)["generated_seconds"] == 5
    for change in ({"duration_seconds": 5}, {"prompt": PROMPT + "日落"}, {"resolution": "1080p"}):
        assert wan.current_call_list(work["id"], **dict({"prompt": PROMPT, "duration_seconds": 30}, **change)
            )["digest"] != first["digest"]
    Image.new("RGB", (480, 720), "blue").save(image)
    with pytest.raises(TalkingHeadBlocked) as error:
        wan.current_call_list(work["id"], prompt=PROMPT, duration_seconds=30)
    assert error.value.code == "WAN_MEDIA_HASH_MISMATCH"


def test_uploaded_source_must_belong_to_current_work(native_work):
    work, task, _ = native_work
    work["wan_image"]["work_id"] = "another-work"
    st.put("works", work["id"], work)
    result = routes.preflight(work["id"], task=task)
    assert result["ok"] is False
    assert "WAN_IMAGE_WORK_MISMATCH" in {item["code"] for item in result["blockers"]}


@pytest.mark.parametrize("seconds,expected", [(2, 120), (5, 300), (30, 1800)])
def test_native_cost_uses_explicit_seconds(seconds, expected):
    result = cost.estimate(routes.ROUTE_WAN, duration_seconds=seconds, resolution="720p")
    assert result["estimated_fen"] == expected
    assert result["billing_seconds"] == result["generated_seconds"] == seconds


def test_native_notice_ignores_old_audio_duration():
    notice = cost.cost_notice(routes.ROUTE_WAN, duration_ms=75000, resolution="720p")
    assert notice["amount_known"] is True
    assert notice["estimated_fen"] == 300


def _authorize(work, seconds=5):
    listing = wan.current_call_list(work["id"], prompt=PROMPT, duration_seconds=seconds)
    est = cost.estimate(routes.ROUTE_WAN, duration_seconds=seconds, resolution="720p")
    cost.confirm_cost(work["id"], route=routes.ROUTE_WAN, request_digest=listing["digest"],
        estimated_fen=est["estimated_fen"], ceiling_fen=est["ceiling_fen"], actor_id="user_fixture",
        call_list=listing)
    return listing


def test_duration_change_invalidates_cost_confirmation(native_work):
    work, _, _ = native_work
    _authorize(work, 5)
    result = cost.cost_summary(work["id"], route=routes.ROUTE_WAN, prompt=PROMPT,
                               resolution="720p", duration_seconds=6)
    assert result["confirmation_valid"] is False
    assert "生成时长" in result["changed_fields"]
    assert result["estimate"]["estimated_fen"] == 360


@pytest.fixture
def paid_native(native_work, monkeypatch):
    allow_billable(monkeypatch)
    monkeypatch.setenv(wan_provider.SECRET_ENV, "fixture-native-key")
    monkeypatch.setenv(wan_provider.BASE_URL_ENV, "https://fixture.cn-beijing.maas.aliyuncs.com/api/v1")
    monkeypatch.setattr(wan, "prepare_native_media", lambda source: {
        "image_url": "https://media.example/image.png"}, raising=False)
    _authorize(native_work[0])
    return native_work


def test_native_submit_has_one_paid_post_and_reuses_run(paid_native):
    work, task, _ = paid_native
    calls = []
    def transport(request):
        calls.append(request)
        run = submission.runs_for_work(work["id"])[-1]
        assert jobs.get_job(run["job_id"])["state"] == "leased"
        assert run["provider_params"]["input_snapshot"]["audio_asset_id"] is None
        return {"status_code": 200, "json": {"output": {"task_id": "native-original-id"}}}
    first = wan.submit_wan(work["id"], task=task, prompt=PROMPT, dry_run=False, transport=transport)
    second = wan.submit_wan(work["id"], task=task, prompt=PROMPT, dry_run=False, transport=transport)
    assert first["submitted"] is True and second["replayed"] is True
    assert first["runs"][0]["id"] == second["runs"][0]["id"]
    assert len(calls) == 1
    assert len(calls[0]["json"]["input"]["media"]) == 1
    assert first["runs"][0]["provider_params"]["duration_seconds"] == 5


def test_native_unknown_submission_is_not_reposted(paid_native):
    work, task, _ = paid_native
    calls = []
    def transport(request):
        calls.append(request)
        return {"status_code": 503}
    for _ in range(2):
        with pytest.raises(TalkingHeadBlocked):
            wan.submit_wan(work["id"], task=task, prompt=PROMPT, dry_run=False, transport=transport)
    assert len(calls) == 1
    assert submission.runs_for_work(work["id"])[-1]["state"] == "unknown"


def test_native_source_change_during_publish_blocks_post(paid_native, monkeypatch):
    work, task, image = paid_native
    def prepare(source):
        Image.new("RGB", (480, 720), "red").save(image)
        return {"image_url": "https://media.example/image.png"}
    monkeypatch.setattr(wan, "prepare_native_media", prepare)
    calls = []
    with pytest.raises((TalkingHeadBlocked, TalkingHeadRejected)):
        wan.submit_wan(work["id"], task=task, prompt=PROMPT, dry_run=False,
                       transport=lambda request: calls.append(request))
    assert calls == []
    assert submission.runs_for_work(work["id"]) == []


def test_image_validator_uses_content_not_upload_extension(native_work):
    validate = getattr(wan_media, "validate_image", None)
    assert callable(validate), "shared image validator is missing"
    path = native_work[2].with_suffix(".upload")
    path.write_bytes(native_work[2].read_bytes())
    assert validate(path) == {"width": 480, "height": 720, "format": "PNG"}


def test_native_media_publishes_only_validated_image(native_work, monkeypatch):
    prepare = getattr(wan_media, "prepare_native_media", None)
    assert callable(prepare), "image-only media bridge is missing"
    calls = []
    class Publisher:
        def exists(self, key):
            return False
        def ensure_bucket(self):
            pass
        def publish_file(self, path, **kwargs):
            calls.append((path, kwargs))
        def url_for(self, key):
            return "https://media.example/" + key
        def verify_public_file(self, url, digest):
            assert digest == native_work[0]["wan_image"]["asset_hash"]
    monkeypatch.setenv(wan_media.providers.SECRET_ENV_AK, "fixture-ak")
    monkeypatch.setenv(wan_media.providers.SECRET_ENV_SK, "fixture-sk")
    monkeypatch.setattr(wan_media.omni_media, "_publisher", lambda *_: Publisher())
    result = prepare(native_work[0]["wan_image"])
    assert list(result) == ["image_url"]
    assert len(calls) == 1
    assert "wan/image/" in result["image_url"]
