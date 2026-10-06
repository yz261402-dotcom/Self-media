"""视觉分析契约：MediaAsset、VisualAnalysis 状态校验、媒体资产表、视觉配置。"""
import pytest
from pydantic import ValidationError

from app import storage as st
from app.content import contracts as cc
from app.content import costs

SHA = "sha256:" + "a" * 64


def asset(**kw):
    d = dict(id="media_1", work_id="w1", kind="reference_video", storage_uri="content-media/w1/x.mp4", sha256=SHA,
             mime_type="video/mp4", byte_size=10, duration_ms=1000, width=720, height=1280,
             source_kind="user_upload", created_at="2026-09-30T00:00:00Z")
    d.update(kw)
    return d


def scene(**kw):
    d = dict(index=1, start_ms=0, end_ms=1000, shot_type="full", camera_motion="static", description="正面舞蹈",
             actions=["抬手"], confidence=0.8)
    d.update(kw)
    return d


def ready(**kw):
    d = dict(state="ready", provider="qwen", model="qwen3-vl-flash", analysis_version="visual-v1",
             media_asset_id="media_1", media_sha256=SHA, duration_ms=1000, summary="s",
             speech_presence="none", music_presence="music", scenes=[scene()], created_at="2026-09-30T00:00:00Z")
    d.update(kw)
    return d


def test_media_asset_rejects_bad_values():
    cc.MediaAsset(**asset())
    for bad in ({"sha256": ""}, {"sha256": "abc"}, {"mime_type": "video/x-flv"}, {"byte_size": 0},
                {"duration_ms": 0}, {"width": 0}, {"kind": "audio"}):
        with pytest.raises(ValidationError):
            cc.MediaAsset(**asset(**bad))


def test_visual_ready_needs_model_scenes_and_valid_timeline():
    cc.VisualAnalysis(**ready())
    for bad in ({"provider": ""}, {"model": ""}, {"scenes": []}, {"media_sha256": ""}, {"duration_ms": 0},
                {"scenes": [scene(start_ms=500, end_ms=400)]}, {"scenes": [scene(end_ms=2000)]},
                {"error": {"code": "X", "message": "m"}}):
        with pytest.raises(ValidationError):
            cc.VisualAnalysis(**ready(**bad))


def test_visual_failed_and_unknown_must_carry_error():
    for state in ("failed", "unknown"):
        with pytest.raises(ValidationError):
            cc.VisualAnalysis(state=state)
        cc.VisualAnalysis(state=state, error={"code": "MODEL_OUTPUT_INVALID", "message": "m"})


def test_reference_snapshot_validates_visual_and_digest_binds_it():
    base = dict(id="r1", work_id="w1", platform_work_id="9", fetched_at="t", extraction_version="v")
    plain = cc.ReferenceSnapshot(**base).model_dump()
    with pytest.raises(ValidationError):
        cc.ReferenceSnapshot(**base, visual={"state": "ready"})
    with_visual = cc.ReferenceSnapshot(**base, visual=ready(), media_asset_id="media_1").model_dump()
    assert cc.reference_digest(plain) != cc.reference_digest(with_visual)
    assert cc.visual_state(plain) == "none" and cc.visual_state(with_visual) == "ready"


def test_media_asset_table_roundtrip():
    st.put("content_media_assets", "media_1", asset(), {"work_id": "w1"})
    assert st.get("content_media_assets", "media_1")["sha256"] == SHA
    assert [a["id"] for a in st.all_rows("content_media_assets", "work_id=?", ["w1"])] == ["media_1"]


def test_visual_limits_defaults_and_env(monkeypatch):
    for k in ("RESERVE_FEN", "PRICE_FEN", "MAX_BYTES", "MAX_DURATION_MS"):
        monkeypatch.delenv(f"AVATAR_STUDIO_CONTENT_VISUAL_{k}", raising=False)
    lim = costs.visual_limits()
    assert (lim.reserve_fen, lim.price_fen, lim.max_bytes, lim.max_duration_ms) == (80, None, 200 * 1024 * 1024, 600000)
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_PRICE_FEN", "12")
    assert costs.visual_limits().price_fen == 12
