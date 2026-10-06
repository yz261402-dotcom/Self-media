"""参考选择与写作上下文：按内容路线门禁；视觉观察只是灵感，不进事实资料。"""
from uuid import uuid4

import pytest

from app import storage as st
from app.content import context as cx
from app.content import contracts as cc
from app.content import references
from app.content import prompts
from app.content import repository as repo
from app.content import visual as visual_lib
from app.content.errors import ContentRejected

SHA = "sha256:" + "b" * 64


def visual(state="ready"):
    v = {"state": state, "provider": "qwen", "model": "qwen3-vl-flash", "analysis_version": "visual-v1",
         "media_asset_id": "media_1", "media_sha256": SHA, "duration_ms": 2000, "summary": "舞蹈",
         "speech_presence": "none", "music_presence": "music", "created_at": "t",
         "scenes": [{"index": 1, "start_ms": 0, "end_ms": 2000, "description": "正面舞蹈", "actions": ["转身"]}]}
    if state != "ready":
        v = {"state": state, "error": {"code": "X", "message": "m"}}
    return v


def make(route, *, transcript="failed", vis=None):
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "brief": {"topic": "舞蹈", "content_route": route},
                          "version": 1, "content_revision": 0})
    ref = {"id": "ref_" + uuid4().hex[:8], "work_id": wid, "platform_work_id": "1", "fetched_at": "t",
           "extraction_version": "redfox-transcript-v1",
           "transcript": {"state": "ready", "text": "口播", "source": "redfox"} if transcript == "ready"
           else {"state": transcript, "error": "无口播"}}
    if vis:
        ref.update(visual=vis, media_asset_id="media_1")
    with st.transaction() as c:
        saved = repo.save_reference(c, ref)
    return wid, saved


def select(wid, ref):
    return references.select_reference(wid, ref["id"], 0)


def test_visual_only_reference_selectable_for_storyboard_but_not_talking_script():
    wid, ref = make("short_video_storyboard", vis=visual())
    assert select(wid, ref)["reference_snapshot_id"] == ref["id"]
    wid2, ref2 = make("talking_script", vis=visual())
    with pytest.raises(ContentRejected) as e:
        select(wid2, ref2)
    assert e.value.code == "REFERENCE_TRANSCRIPT_REQUIRED"


def test_transcript_only_reference_selectable_for_both_routes():
    for route in ("talking_script", "short_video_storyboard"):
        wid, ref = make(route, transcript="ready")
        assert select(wid, ref)["reference_snapshot_id"] == ref["id"]


def silent_reference(route, *, vis=None):
    wid, ref = make(route, transcript="ready", vis=vis)
    ref["transcript"]["text"] = "静音音频"
    st.put("content_references", ref["id"], ref, {"work_id": wid})
    return wid, ref


@pytest.mark.parametrize("route", ["talking_script", "short_video_storyboard"])
def test_legacy_silent_audio_ready_reference_cannot_bypass_route_gate(route):
    wid, ref = silent_reference(route)
    with pytest.raises(ContentRejected):
        select(wid, ref)
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, reference_snapshot_id=ref["id"])
    with pytest.raises(ContentRejected):
        cx.load_context(wid, require_reference=True)


def test_legacy_silent_reference_needs_visual_and_preserves_raw_snapshot():
    wid, ref = silent_reference("short_video_storyboard")
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, reference_snapshot_id=ref["id"])
    assert visual_lib.reference_needs_visual(st.get("works", wid))["id"] == ref["id"]
    transcript = ref["transcript"]
    projected = cc.transcript_view(transcript)
    assert projected["state"] == "failed" and "有效人声" in projected["error"]
    assert transcript["state"] == "ready" and repo.get_reference(ref["id"])["transcript"] == transcript


def test_old_prompt_cannot_generate_using_a_silent_placeholder_reference():
    wid, ref = silent_reference("short_video_storyboard")
    prompt_id = "prm_" + wid
    st.put("content_prompts", prompt_id, {"id": prompt_id, "work_id": wid}, {"work_id": wid})
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, reference_snapshot_id=ref["id"], writing_prompt_id=prompt_id)
    with pytest.raises(ContentRejected) as exc:
        cx.load_context(wid, require_prompt=True)
    assert exc.value.code == "REFERENCE_VISUAL_REQUIRED"


def test_silent_reference_with_visual_can_prepare_storyboard_without_fake_speech():
    wid, ref = silent_reference("short_video_storyboard", vis=visual())
    assert select(wid, ref)["reference_snapshot_id"] == ref["id"]
    block = prompts.reference_block(ref)
    assert "无有效口播转写" in block
    assert "静音音频" not in block
    assert "正面舞蹈" in block


@pytest.mark.parametrize("source,text", [("user_edit", "静音音频"), ("redfox", "这段静音音频需要后期配音")])
def test_real_speech_or_user_edit_is_not_mistaken_for_provider_placeholder(source, text):
    assert cc.transcript_is_usable({"state": "ready", "source": source, "text": text})


@pytest.mark.parametrize("state", ["none", "failed", "unknown"])
def test_non_ready_visual_is_still_blocked_with_visual_code(state):
    vis = None if state == "none" else visual(state)
    wid, ref = make("short_video_storyboard", vis=vis)
    with pytest.raises(ContentRejected) as e:
        select(wid, ref)
    assert e.value.code == "REFERENCE_VISUAL_REQUIRED"


def test_context_gate_follows_route_and_visual_stays_out_of_materials():
    wid, ref = make("short_video_storyboard", vis=visual())
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, reference_snapshot_id=ref["id"])
    ctx = cx.load_context(wid, require_reference=True)
    assert ctx["reference"]["visual"]["state"] == "ready" and ctx["materials"] == []
    assert ctx["digests"]["reference"] == ref["content_digest"]
    wid2, ref2 = make("talking_script", vis=visual())
    with st.transaction() as c:
        repo.bump_content_revision(c, wid2, reference_snapshot_id=ref2["id"])
    with pytest.raises(ContentRejected) as e:
        cx.load_context(wid2, require_reference=True)
    assert e.value.code == "REFERENCE_TRANSCRIPT_REQUIRED"


def test_visual_change_changes_digest_so_old_prompts_go_stale():
    _, a = make("short_video_storyboard", vis=visual())
    v2 = visual()
    v2["scenes"][0]["description"] = "换了一个动作"
    _, b = make("short_video_storyboard", vis=v2)
    assert a["content_digest"] != b["content_digest"]
