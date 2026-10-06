"""Wan 路线的离线契约、费用和前置门禁；不使用真实凭据或供应商。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from app import config, contracts, storage as st
from app.digital_human import cost, routes
from app.digital_human.errors import TalkingHeadRejected
from app.tts import audio as tts_audio
from dh_helpers import allow_billable, prepare_work


WAN = "WAN_3_0"


def test_wan_contract_preserves_legacy_defaults_without_authorizing_them():
    assert WAN in get_args(contracts.GenerationRoute)
    assert contracts.Work.model_fields["route"].default == "M2"
    assert contracts.Task.model_fields["route"].default == "M2"
    assert contracts.Task.model_fields["route_confirmed"].default is False
    with pytest.raises(TalkingHeadRejected) as error:
        routes.assert_route_selected({"route": "M2", "route_confirmed": False})
    assert error.value.code == "ROUTE_NOT_CONFIRMED"


def test_wan_capability_is_independent_and_unverified():
    view = routes.route_view(WAN)
    assert routes.ROUTE_WAN == WAN
    assert WAN in routes.ROUTES
    assert view["provider"] == "dashscope_wan"
    assert view["model"] == "wan3.0-video"
    assert view["capability"] == routes.CAP_SHORT_VIDEO_CLOUD == "short_video_cloud"
    assert view["requires_motion_plate"] is False
    assert view["stage_count"] == 1 and view["heavy"] is False
    assert view["declared"]["generated_min_seconds"] == 2
    assert view["declared"]["generated_max_seconds"] == 30
    assert view["declared"]["audio_mode"] == "native_audio"
    assert view["declared"]["resolutions"] == ["480p", "720p", "1080p"]
    assert view["verified_state"] == "unverified" and view["verified"] == {}
    assert view["body_motion"]["verified"] is False
    assert view["cost_notice_required"] is True


@pytest.mark.parametrize("audio_ms,expected", [(1000, 2), (1999, 2), (2000, 2),
                                               (2001, 3), (9360, 10), (15000, 15)])
def test_legacy_wan_audio_helper_uses_integer_generation_seconds(audio_ms, expected):
    validator = getattr(routes, "assert_wan_audio_duration_allowed", None)
    assert callable(validator), "Wan 需要自己的音频时长门禁"
    assert validator(audio_ms)["audio_duration_ms"] == audio_ms
    assert routes.wan_generation_seconds(audio_ms) == expected


@pytest.mark.parametrize("audio_ms,code", [(0, "WAN_AUDIO_TOO_SHORT"),
                                         (999, "WAN_AUDIO_TOO_SHORT"),
                                         (15001, "WAN_AUDIO_TOO_LONG")])
def test_legacy_wan_audio_helper_rejects_out_of_range_without_splitting(audio_ms, code):
    validator = getattr(routes, "assert_wan_audio_duration_allowed", None)
    assert callable(validator), "Wan 需要自己的音频时长门禁"
    with pytest.raises(TalkingHeadRejected) as error:
        validator(audio_ms)
    assert error.value.code == code
    assert "不分段" in error.value.message


@pytest.mark.parametrize("audio_ms", [999, 1000, 15000, 15001, 60000])
def test_wan_native_preflight_resolves_image_independently_of_old_audio(audio_ms):
    work = prepare_work(route=WAN, audio_ms=audio_ms)
    task = st.get("tasks", work["current_task_id"])
    out = routes.preflight(work["id"], task=task, route=WAN)
    codes = [b["code"] for b in out["blockers"]]
    assert out["source"]["kind"] == "variant"
    assert out["source"]["asset_hash"]
    assert out["audio_asset_id"] is None
    assert out["plate_id"] is None and "PLATE_NOT_APPROVED" not in codes
    assert "OmniHuman" not in out["motion_intent"]["reason"]
    assert out["ok"] is True


def test_wan_native_preflight_does_not_require_confirmed_audio():
    work = prepare_work(route=WAN)
    audio = tts_audio.final_audio(work["id"])
    audio["confirmed"] = False
    st.put("audios", audio["id"], audio, {"work_id": work["id"]})
    out = routes.preflight(work["id"], task=st.get("tasks", work["current_task_id"]))
    assert out["ok"] is True
    assert "FINAL_AUDIO_REQUIRED" not in [b["code"] for b in out["blockers"]]


@pytest.mark.parametrize("resolution,rate", [("480p", 30), ("720p", 60), ("1080p", 120)])
@pytest.mark.parametrize("audio_ms,seconds", [(1000, 2), (9360, 10), (15000, 15)])
def test_legacy_wan_estimate_accepts_audio_milliseconds(resolution, rate,
                                                                      audio_ms, seconds):
    out = cost.estimate(WAN, duration_ms=audio_ms, resolution=resolution)
    assert out["unit_fen"] == rate
    assert out["billing_seconds"] == out["generated_seconds"] == seconds
    assert out["audio_seconds"] == audio_ms / 1000
    assert out["estimated_fen"] == out["ceiling_fen"] == seconds * rate
    assert out["billing_verified"] is False
    assert out["segment_count"] == 1
    assert out["monthly_fen"] == seconds * rate * 30


@pytest.mark.parametrize("kwargs,code", [({"resolution": "4k"}, "WAN_RESOLUTION_UNSUPPORTED"),
                                        ({"segment_count": 2}, "WAN_SEGMENTATION_NOT_SUPPORTED"),
                                        ({"duration_ms": 15001}, "WAN_AUDIO_TOO_LONG")])
def test_wan_estimate_refuses_unsupported_request(kwargs, code):
    params = {"duration_ms": 9000, "resolution": "720p", **kwargs}
    with pytest.raises(TalkingHeadRejected) as error:
        cost.estimate(WAN, **params)
    assert error.value.code == code


def test_wan_price_snapshot_identifies_official_price_by_resolution():
    snap = cost.register_price_snapshot(WAN)
    assert snap["provider"] == "dashscope_wan"
    assert snap["unit"] == "fen_per_generated_second"
    assert snap["unit_fen_by_resolution"] == {"480p": 30, "720p": 60, "1080p": 120}
    assert snap["unit_fen"] is None, "多档费率不能伪装成统一单价"
    assert snap["price_source"] == "official_bailian_beijing_list_price"
    assert snap["billing_verified"] is False
    summary = cost.cost_summary("unused_wan_domain_work")
    assert WAN in summary["price_snapshots"]


def test_wan_cost_notice_uses_native_default_with_any_old_audio():
    notice = cost.cost_notice(WAN, duration_ms=60000, resolution="720p")
    assert notice["amount_known"] is True
    assert notice["estimated_fen"] == 300
    assert notice["duration_seconds"] == 5


def test_wan_cost_summary_ignores_unrelated_long_audio():
    work = prepare_work(route=WAN, audio_ms=60000)
    summary = cost.cost_summary(work["id"], route=WAN, duration_ms=60000,
                                prompt="自然讲述", resolution="720p")
    assert summary["estimate"]["estimated_fen"] == 300 and summary["confirmation_valid"] is False
    assert summary["blocker"] is None


def test_native_price_snapshot_supersedes_old_audio_basis_without_overwriting_history():
    old = cost.register_price_snapshot(WAN)
    old.pop("generation_mode", None)
    old["minimum_note"] = "原音频允许 1–15 秒"
    st.put(cost.PRICE_TABLE, old["id"], old, {"route": WAN})
    current = cost.current_price(WAN)
    assert current["id"] != old["id"]
    assert current["generation_mode"] == "image_prompt_native_audio"
    assert "2–30" in current["minimum_note"]
    assert st.get(cost.PRICE_TABLE, old["id"]) == old


def test_wan_resolution_change_invalidates_cost_confirmation(monkeypatch):
    allow_billable(monkeypatch)
    listing = {"route": WAN, "resolution": "720p", "digest": "wan-720p"}
    cost.confirm_cost("wan_domain_confirmation", route=WAN, request_digest=listing["digest"],
                      estimated_fen=600, ceiling_fen=600, generated_seconds=10,
                      resolution="720p", actor_id="user_test", call_list=listing)
    changed = {**listing, "resolution": "1080p", "digest": "wan-1080p"}
    with pytest.raises(TalkingHeadRejected) as error:
        cost.assert_submission_allowed("wan_domain_confirmation", route=WAN,
                                       request_digest=changed["digest"], estimated_fen=1200,
                                       call_list=changed)
    assert error.value.code == "COST_LIST_CHANGED"
    assert error.value.detail["changed_fields"] == ["分辨率"]


@pytest.mark.parametrize("alias", ["qwen", "千问", "百炼"])
def test_wan_secret_alias_is_only_injected_and_public_view_is_redacted(alias, tmp_path, monkeypatch):
    key_env = "AVATAR_STUDIO_QWEN_API_KEY"
    monkeypatch.delenv(key_env, raising=False)
    secret = "fixture_wan_secret_not_a_real_key_19"
    path = Path(tmp_path) / "wan-fixture-secrets.md"
    path.write_text(f"**{alias}**: {secret}\n", encoding="utf-8")
    injected = config.load_secrets_from_file(str(path))
    assert injected == [key_env]
    spec = config.PROVIDERS["dashscope_wan"]
    assert spec.env_prefix == "WAN" and spec.required_env == (key_env,)
    assert spec.model_env == "AVATAR_STUDIO_WAN_MODEL"
    assert spec.base_env == "AVATAR_STUDIO_WAN_BASE_URL"
    assert spec.default_model == "wan3.0-video"
    assert spec.capabilities == ["image_audio_to_video"]
    out = config.public_provider_view("dashscope_wan", "image_audio_to_video")
    assert out["configured"] is True and out["state"] == "unverified"
    assert out["usable"] is False
    assert out["api_key"] == "********19"
    assert secret not in json.dumps(out)
    assert {"deepseek", "minimax", "gpt-image-2", "m2_worker", "volcengine"} <= config.PROVIDERS.keys()


def test_wan_missing_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_QWEN_API_KEY", raising=False)
    out = config.check_provider("dashscope_wan")
    assert out.state == "not_configured" and out.configured is False


def test_wan_model_does_not_inherit_qwen_text_model_settings(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_QWEN_MODEL", "qwen-text-fixture")
    monkeypatch.setenv("AVATAR_STUDIO_QWEN_BASE_URL", "https://text.example.invalid")
    monkeypatch.delenv("AVATAR_STUDIO_WAN_MODEL", raising=False)
    assert config.check_provider("dashscope_wan").model == "wan3.0-video"
