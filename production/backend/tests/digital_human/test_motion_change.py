"""更换动作后旧底片/原片不得继续执行，原文件与音频仍可追溯。"""
from pathlib import Path

from fastapi.testclient import TestClient

from dh_helpers import HASH_B, install_motion_ref, install_plate, prepare_work
from app import lineage, storage as st
from app.digital_human import plates, routes, submission, videos
from app.digital_human.errors import TalkingHeadBlocked
from app.tts import audio as tts_audio

import pytest


def _save(work_id, intent):
    from app.main import app
    response = TestClient(app).post(f"/api/v1/works/{work_id}/motion-intent", json=intent)
    assert response.status_code == 200, response.text
    return response.json()


def _other_motion(work):
    ref = install_motion_ref(work["identity_id"], motion_key=work["motion_intent"]["motion_key"])
    return {**work["motion_intent"], "motion_ref_id": ref["id"]}


def test_changing_reference_revokes_m2_outputs_but_preserves_audio_files_and_other_work(m2_work, tmp_path_factory):
    work_id = m2_work["id"]
    old_plate = plates.approved_plate_for(work_id)
    audio_before = tts_audio.final_audio(work_id)
    reviews_before = st.all_rows("plate_reviews", "video_id=?", [old_plate["id"]])
    output = Path(tmp_path_factory.mktemp("motion-change")) / "old.mp4"
    output.write_bytes(b"old verified output retained for audit")
    original = videos.register_video(
        work_id=work_id, kind="original", route="M2", provider="m2_worker", model="MuseTalk-1.5",
        duration_ms=9000, file_hash=HASH_B, storage_uri=str(output), is_mock=False,
        identity_id=m2_work["identity_id"], motion_ref_id=old_plate["motion_ref_id"],
        audio_asset_id=audio_before["id"],
        has_video_track=True, has_audio_track=True,
    )
    original.update(approval_state="approved", confirmed=True)
    st.put("videos", original["id"], original, {"work_id": work_id})
    work = st.get("works", work_id)
    work["video_asset_id"] = original["id"]
    st.put("works", work_id, work)
    package_id = st.new_id("pkg")
    for artifact_id, kind in [(original["id"], "video"), (package_id, "package"), (audio_before["id"], "audio")]:
        lineage.record(artifact_id=artifact_id, kind=kind, work_id=work_id, digest=HASH_B)
    other = prepare_work(route="M2", with_plate=True)
    other_before = plates.approved_plate_for(other["id"])

    saved = _save(work_id, _other_motion(m2_work))

    assert saved["motion_intent"]["motion_ref_id"] != old_plate["motion_ref_id"]
    assert plates.approved_plate_for(work_id) is None
    assert videos.get_video(old_plate["id"])["approval_state"] == "revoked"
    assert videos.get_video(original["id"])["approval_state"] == "revoked"
    assert videos.final_video(work_id) is None
    assert st.get("works", work_id)["video_asset_id"] is None
    assert lineage.get(original["id"])["invalidated_at"]
    assert lineage.get(package_id)["invalidated_at"]
    assert not lineage.get(audio_before["id"])["invalidated_at"]
    assert tts_audio.final_audio(work_id) == audio_before
    assert output.read_bytes() == b"old verified output retained for audit"
    assert videos.get_video(original["id"])["file_hash"] == HASH_B
    assert st.all_rows("plate_reviews", "video_id=?", [old_plate["id"]]) == reviews_before
    assert plates.approved_plate_for(other["id"]) == other_before
    task = st.get("tasks", m2_work["current_task_id"])
    check = routes.preflight(work_id, task=task, route="M2")
    assert "PLATE_NOT_APPROVED" in {item["code"] for item in check["blockers"]}
    with pytest.raises(TalkingHeadBlocked, match="底片"):
        submission.submit_m2(work_id, task=task, dry_run=True)


def test_saving_same_motion_keeps_approved_plate(m2_work):
    before = plates.approved_plate_for(m2_work["id"])
    _save(m2_work["id"], m2_work["motion_intent"])
    assert plates.approved_plate_for(m2_work["id"]) == before


def test_changing_params_also_revokes_existing_plate(m2_work):
    before = plates.approved_plate_for(m2_work["id"])
    _save(m2_work["id"], {**m2_work["motion_intent"], "params": {"amplitude": 0.5}})
    assert plates.approved_plate_for(m2_work["id"]) is None
    assert videos.get_video(before["id"])["approval_state"] == "revoked"


def test_late_old_motion_plate_cannot_displace_current_plate(m2_work):
    work_id = m2_work["id"]
    old_ref_id = m2_work["motion_intent"]["motion_ref_id"]
    new_intent = _other_motion(m2_work)
    _save(work_id, new_intent)
    current = install_plate(work_id, motion_ref_id=new_intent["motion_ref_id"], variant_id=m2_work["variant_id"])
    late = install_plate(work_id, motion_ref_id=old_ref_id, variant_id=m2_work["variant_id"])
    late["created_at"] = "2999-01-01T00:00:00+00:00"
    st.put("videos", late["id"], late, {"work_id": work_id})
    assert plates.approved_plate_for(work_id)["id"] == current["id"]


def test_revoked_import_source_disables_previously_approved_plate(m2_work):
    source = st.get("motion_references", m2_work["motion_intent"]["motion_ref_id"])
    asset = st.get("avatar_assets", source["asset_id"])
    imported = {**source, "id": st.new_id("mot"), "source_reference_id": source["id"],
                "source_asset_hash": asset["file_hash"]}
    st.put("motion_references", imported["id"], imported, {"identity_id": imported["identity_id"]})
    _save(m2_work["id"], {**m2_work["motion_intent"], "motion_ref_id": imported["id"]})
    plate = install_plate(m2_work["id"], motion_ref_id=imported["id"], variant_id=m2_work["variant_id"])
    assert plates.approved_plate_for(m2_work["id"])["id"] == plate["id"]

    source["approval_state"] = "rejected"
    st.put("motion_references", source["id"], source, {"identity_id": source["identity_id"]})

    assert plates.approved_plate_for(m2_work["id"]) is None
    assert videos.get_video(plate["id"])["approval_state"] == "approved"
    check = routes.preflight(m2_work["id"], task=st.get("tasks", m2_work["current_task_id"]), route="M2")
    assert "PLATE_NOT_APPROVED" in {item["code"] for item in check["blockers"]}
