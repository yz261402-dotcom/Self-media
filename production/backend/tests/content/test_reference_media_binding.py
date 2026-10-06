"""Reference media associations survive revisions without network calls or snapshot mutation."""
import io
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.content import media, tasks
from app.content import repository as repo
from tests.content._helpers import make_work, set_env


@pytest.fixture(autouse=True)
def isolated_media(tmp_path, monkeypatch):
    from app.content.providers import qwen_vision, redfox, text as tp

    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(media, "probe_video", lambda path: {"duration_ms": 2000, "width": 320, "height": 568})
    monkeypatch.setattr(media, "validate_video_complete", lambda path: None)  # 本组假媒体只用于测试关联。
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)

    def no_provider(*args, **kwargs):
        pytest.fail("Media association lookup must not call providers")

    monkeypatch.setattr(redfox, "post", no_provider)
    monkeypatch.setattr(tp, "call_text", no_provider)
    monkeypatch.setattr(qwen_vision, "call_frames", no_provider)


def reference(wid, *, parent_id=None, media_asset_id=None, reference_id=None):
    snapshot = {"id": reference_id or "ref_" + uuid4().hex, "work_id": wid, "platform_work_id": "77",
                "fetched_at": "t", "extraction_version": "redfox-transcript-v1", "parent_id": parent_id,
                "media_asset_id": media_asset_id, "transcript": {"state": "ready", "source": "user_edit", "text": "Speech"}}
    with st.transaction() as c:
        return repo.save_reference(c, snapshot)


def legacy_asset(wid, *, created_at="2026-09-30T00:00:00Z", reference_ids=None):
    row = {"id": "media_" + uuid4().hex, "work_id": wid, "kind": "reference_video",
           "storage_uri": "content-media/legacy.mp4", "sha256": "sha256:" + "a" * 64,
           "mime_type": "video/mp4", "byte_size": 100, "duration_ms": 2000, "width": 320, "height": 568,
           "source_kind": "provider_url", "source_url": "", "created_at": created_at}
    if reference_ids is not None:
        row["reference_snapshot_ids"] = reference_ids
    st.put(media.TABLE, row["id"], row, {"work_id": wid})
    return row


def fetch_job(wid, rid, asset_id, *, state="succeeded"):
    job = tasks.submit_job(wid, "media_fetch", "fetch-" + uuid4().hex, {"reference_snapshot_id": rid})
    tasks.finish_job(job["id"], state, result_refs={"reference_id": rid, "media_asset_id": asset_id})
    return tasks.get_job(job["id"])


def associated_asset_id(wid, rid):
    assert callable(getattr(media, "reference_media_asset_id", None)), "Missing reference media association lookup"
    return media.reference_media_asset_id(wid, rid)


def upload(wid, rid, content=b"fake-mp4"):
    return media.save_uploaded_video(wid, rid, io.BytesIO(content), "clip.mp4", "video/mp4")


def test_new_upload_records_its_reference_and_can_be_found():
    wid = make_work(with_reference=False)
    ref = reference(wid)
    asset = upload(wid, ref["id"])

    assert st.get(media.TABLE, asset.id).get("reference_snapshot_ids") == [ref["id"]]
    assert associated_asset_id(wid, ref["id"]) == asset.id
    assert repo.get_reference(ref["id"]) == ref


def test_same_hash_reuse_appends_reference_ids_without_changing_asset_metadata():
    wid = make_work(with_reference=False)
    first, second = reference(wid), reference(wid)
    asset = upload(wid, first["id"])
    before = st.get(media.TABLE, asset.id)

    reused = upload(wid, second["id"])
    upload(wid, second["id"])

    assert reused.id == asset.id
    after = st.get(media.TABLE, asset.id)
    assert after.get("reference_snapshot_ids") == [first["id"], second["id"]]
    associations = {"reference_snapshot_ids", "reference_bound_at"}
    assert {k: v for k, v in after.items() if k not in associations} == {
        k: v for k, v in before.items() if k not in associations}
    assert len(st.all_rows(media.TABLE, "work_id=?", [wid])) == 1
    assert associated_asset_id(wid, first["id"]) == associated_asset_id(wid, second["id"]) == asset.id


def test_legacy_assets_without_reference_ids_remain_readable():
    wid = make_work(with_reference=False)
    row = legacy_asset(wid)
    asset = media.get_media_asset(row["id"])

    assert asset.id == row["id"]
    assert asset.model_dump().get("reference_snapshot_ids") == []
    assert st.get(media.TABLE, row["id"]) == row


def test_direct_reference_binding_takes_priority_over_snapshot_and_fetch_records():
    wid = make_work(with_reference=False)
    explicit = legacy_asset(wid)
    ref = reference(wid, media_asset_id=explicit["id"])
    linked = legacy_asset(wid, created_at="2026-09-30T00:00:01Z", reference_ids=[ref["id"]])
    fetch_job(wid, ref["id"], linked["id"])

    assert associated_asset_id(wid, ref["id"]) == linked["id"]


def test_latest_associated_asset_takes_priority_over_legacy_fetch_records():
    wid = make_work(with_reference=False)
    ref = reference(wid)
    old = legacy_asset(wid, reference_ids=[ref["id"]])
    latest = legacy_asset(wid, created_at="2026-09-30T00:00:01Z", reference_ids=[ref["id"]])
    fetch_job(wid, ref["id"], old["id"])

    assert associated_asset_id(wid, ref["id"]) == latest["id"]


def test_successful_fetch_recovers_legacy_asset_for_reference_and_child():
    wid = make_work(with_reference=False)
    parent = reference(wid)
    child = reference(wid, parent_id=parent["id"])
    asset = legacy_asset(wid)
    fetch_job(wid, parent["id"], asset["id"])

    assert associated_asset_id(wid, parent["id"]) == asset["id"]
    assert associated_asset_id(wid, child["id"]) == asset["id"]
    assert repo.get_reference(child["id"]) == child
    assert st.get(media.TABLE, asset["id"]) == asset


@pytest.mark.parametrize("state", ["failed", "unknown", "cancelled"])
def test_unsuccessful_fetch_does_not_bind_media(state):
    wid = make_work(with_reference=False)
    ref = reference(wid)
    asset = legacy_asset(wid)
    fetch_job(wid, ref["id"], asset["id"], state=state)

    assert associated_asset_id(wid, ref["id"]) == ""


def test_child_inherits_uploaded_parent_media_but_its_own_upload_takes_priority():
    wid = make_work(with_reference=False)
    parent = reference(wid)
    asset = upload(wid, parent["id"], b"parent-video")
    child = reference(wid, parent_id=parent["id"])

    assert associated_asset_id(wid, child["id"]) == asset.id
    own = upload(wid, child["id"], b"child-video")
    assert associated_asset_id(wid, child["id"]) == own.id


def test_unrelated_work_media_is_never_used_as_reference_fallback():
    wid = make_work(with_reference=False)
    wanted, unrelated = reference(wid), reference(wid)
    upload(wid, unrelated["id"])

    assert associated_asset_id(wid, wanted["id"]) == ""
    assert associated_asset_id(wid, "ref_missing") == ""


def test_lookup_rejects_cross_work_reference_parent_and_asset():
    wid, other = make_work(with_reference=False), make_work(with_reference=False)
    foreign_asset = legacy_asset(other)
    foreign_ref = reference(other, media_asset_id=foreign_asset["id"])
    child = reference(wid, parent_id=foreign_ref["id"])
    wrong_asset = reference(wid, media_asset_id=foreign_asset["id"])
    fetch_job(wid, wrong_asset["id"], foreign_asset["id"])

    assert associated_asset_id(wid, foreign_ref["id"]) == ""
    assert associated_asset_id(wid, child["id"]) == ""
    assert associated_asset_id(wid, wrong_asset["id"]) == ""


def test_parent_cycle_terminates_and_lookup_does_not_write(monkeypatch):
    wid = make_work(with_reference=False)
    first_id, second_id = "ref_" + uuid4().hex, "ref_" + uuid4().hex
    first = reference(wid, reference_id=first_id, parent_id=second_id)
    second = reference(wid, reference_id=second_id, parent_id=first_id)
    reads = []
    get_reference = repo.get_reference

    def bounded_read(rid):
        reads.append(rid)
        assert len(reads) <= 3, "Reference cycle was not stopped"
        return get_reference(rid)

    def no_write(*args, **kwargs):
        pytest.fail("Association lookup must remain read-only")

    monkeypatch.setattr(repo, "get_reference", bounded_read)
    monkeypatch.setattr(repo, "save_reference", no_write)
    monkeypatch.setattr(st, "put", no_write)

    assert associated_asset_id(wid, first_id) == ""
    assert get_reference(first_id) == first and get_reference(second_id) == second


def test_api_details_and_summaries_recover_child_media_without_mutating_snapshots():
    from app.main import app

    wid = make_work(with_reference=False)
    parent = reference(wid)
    asset = upload(wid, parent["id"])
    child = reference(wid, parent_id=parent["id"])
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, reference_snapshot_id=child["id"])
    work_before = st.get("works", wid)
    client = TestClient(app)
    url = f"/api/v1/works/{wid}/content"

    detail = client.get(url + f"/references/{child['id']}")
    state = client.get(url + "/state")

    assert detail.status_code == state.status_code == 200
    assert detail.json()["media_asset_id"] == asset.id
    assert state.json()["reference"]["media_asset_id"] == asset.id
    summaries = {r["id"]: r for r in state.json()["references"]}
    assert summaries[parent["id"]]["media_asset_id"] == summaries[child["id"]]["media_asset_id"] == asset.id
    assert repo.get_reference(parent["id"]) == parent and repo.get_reference(child["id"]) == child
    assert st.get("works", wid) == work_before
    assert not tasks.list_jobs(wid)


def test_transcript_revision_freezes_parent_media_binding_in_child_snapshot():
    from app.content import references
    from app.main import app

    wid = make_work(with_reference=False)
    parent = reference(wid)
    original_media = upload(wid, parent["id"], b"original-video")
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, reference_snapshot_id=parent["id"])
    job = tasks.submit_job(wid, "reference_revision", "revision-" + uuid4().hex,
                           {"reference_snapshot_id": parent["id"], "transcript_text": "Corrected speech"})
    tasks.set_progress(job["id"], state="running")
    references.handle_reference_revision(job)
    done = tasks.get_job(job["id"])

    assert done["state"] == "succeeded"
    child = repo.get_reference(done["result_refs"]["reference_id"])
    assert child["parent_id"] == parent["id"] and child["media_asset_id"] == original_media.id
    newer_media = upload(wid, parent["id"], b"newer-parent-video")
    assert associated_asset_id(wid, parent["id"]) == newer_media.id
    assert associated_asset_id(wid, child["id"]) == original_media.id

    client = TestClient(app)
    url = f"/api/v1/works/{wid}/content"
    assert client.get(url + f"/references/{child['id']}").json()["media_asset_id"] == original_media.id
    state = client.get(url + "/state").json()
    summary = next(row for row in state["references"] if row["id"] == child["id"])
    assert summary["media_asset_id"] == original_media.id
    assert repo.get_reference(child["id"]) == child and repo.get_reference(parent["id"]) == parent


def test_reuploading_first_asset_restores_latest_reference_choice_without_changing_created_at(monkeypatch):
    from app.main import app

    times = iter(["2026-09-30T00:00:00Z", "2026-09-30T00:00:01Z", "2026-09-30T00:00:02Z"])
    monkeypatch.setattr(media, "now_iso", lambda: next(times))
    wid = make_work(with_reference=False)
    ref = reference(wid)
    first = upload(wid, ref["id"], b"video-a")
    upload(wid, ref["id"], b"video-b")
    chosen = upload(wid, ref["id"], b"video-a")

    assert chosen.id == first.id
    assert associated_asset_id(wid, ref["id"]) == first.id
    assert st.get(media.TABLE, first.id)["created_at"] == first.created_at
    client = TestClient(app)
    url = f"/api/v1/works/{wid}/content"
    assert client.get(url + f"/references/{ref['id']}").json()["media_asset_id"] == first.id
    summary = next(row for row in client.get(url + "/state").json()["references"] if row["id"] == ref["id"])
    assert summary["media_asset_id"] == first.id


def test_direct_child_upload_overrides_frozen_snapshot_media():
    wid = make_work(with_reference=False)
    parent = reference(wid)
    frozen = upload(wid, parent["id"], b"parent-video")
    child = reference(wid, parent_id=parent["id"], media_asset_id=frozen.id)
    assert associated_asset_id(wid, child["id"]) == frozen.id

    replacement = upload(wid, child["id"], b"child-replacement")

    assert associated_asset_id(wid, child["id"]) == replacement.id
    assert repo.get_reference(child["id"]) == child
