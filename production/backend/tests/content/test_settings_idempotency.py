"""Saving unchanged creation settings must preserve review and production bindings."""
import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.content import selection, tasks
from app.main import app
from tests.content._helpers import human_approve, make_work, set_env
from tests.content.test_partial_adoption import reviewed_batch


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "0")
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)


def save(wid, revision, **fields):
    return TestClient(app).put(f"/api/v1/works/{wid}/content/settings",
                             json={"expected_content_revision": revision, **fields})


@pytest.mark.parametrize("field", ["content_route", "topic", "materials"])
def test_same_settings_preserve_adopted_storyboard_and_graph(monkeypatch, tmp_path, field):
    from app.avatar import person_image
    from app.short_video import service

    monkeypatch.setenv("AVATAR_STUDIO_MODE", "integration")
    monkeypatch.setenv("AVATAR_STUDIO_ALLOW_INTEGRATION", "1")
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(person_image.KEY_ENV, "test-key")
    monkeypatch.setenv(person_image.BASE_ENV, "https://image.example")
    wid, script, batch = reviewed_batch(storyboard=True, fail=(2, 3))
    human_approve(wid, batch["candidates"][0]["id"])
    selection.adopt(wid, batch["candidates"][0]["id"])
    graph = service.create_graph(wid, image_ceiling_fen_per_shot=100)
    before = st.get("works", wid)
    calls = dict(script.calls)
    value = before["brief"].get(field, [] if field == "materials" else "")
    response = save(wid, before["content_revision"], brief={field: value})
    assert response.status_code == 200, response.text
    assert response.json()["content_revision"] == before["content_revision"]
    assert st.get("works", wid) == before
    assert selection.current_selection_valid(wid)
    service.assert_current(service.get_graph(graph["id"]))
    assert script.calls == calls


def test_unchanged_legacy_brief_does_not_write_defaults_or_clear_prompt():
    wid = make_work()
    work = st.get("works", wid)
    work["writing_prompt_id"] = "existing-prompt"
    st.put("works", wid, work)
    response = save(wid, work["content_revision"], brief={"topic": work["brief"]["topic"]})
    assert response.status_code == 200
    assert st.get("works", wid) == work


def test_unchanged_legacy_work_without_revision_is_reported_as_zero():
    wid = make_work(with_reference=False)
    work = st.get("works", wid)
    work.pop("content_revision")
    st.put("works", wid, work)
    response = save(wid, 0, brief={"topic": work["brief"]["topic"]})
    assert response.status_code == 200
    assert response.json()["content_revision"] == 0
    assert st.get("works", wid) == work


def test_settings_preserve_not_found_error_when_work_is_deleted_between_reads(monkeypatch):
    from app import content_api

    wid = make_work()
    work = st.get("works", wid)
    original = content_api._work
    first = True

    def read_then_delete(work_id):
        nonlocal first
        found = original(work_id)
        if first:
            first = False
            st.put("works", work_id, dict(found, deleted_at="2026-10-07T00:00:00Z"))
        return found

    monkeypatch.setattr(content_api, "_work", read_then_delete)
    response = save(wid, work["content_revision"], brief={"topic": "new topic"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORK_DELETED"
    assert st.get("works", wid)["content_revision"] == work["content_revision"]


def test_unchanged_style_and_writer_do_not_clear_prompt():
    wid = make_work()
    first = save(wid, st.get("works", wid)["content_revision"],
                 style_snapshot={"persona": "steady"}, writer_profile_id="writer-profile")
    assert first.status_code == 200
    work = st.get("works", wid)
    work["writing_prompt_id"] = "existing-prompt"
    st.put("works", wid, work)
    response = save(wid, work["content_revision"], style_snapshot={"persona": "steady"},
                    writer_profile_id="writer-profile")
    assert response.status_code == 200
    assert st.get("works", wid) == work


def test_unchanged_settings_with_stale_revision_still_rejected():
    wid = make_work()
    work = st.get("works", wid)
    response = save(wid, work["content_revision"] - 1, brief={"topic": work["brief"]["topic"]})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONTENT_REVISION_STALE"
    assert st.get("works", wid) == work


def test_real_route_changes_revoke_adoption_and_revision_never_rolls_back():
    wid, _, batch = reviewed_batch(storyboard=True)
    human_approve(wid, batch["candidates"][0]["id"])
    selection.adopt(wid, batch["candidates"][0]["id"])
    before = st.get("works", wid)
    response = save(wid, before["content_revision"], brief={"content_route": "talking_script"})
    assert response.status_code == 200
    changed = st.get("works", wid)
    assert changed["content_revision"] == before["content_revision"] + 1
    assert changed["script_version_id"] is None
    assert changed["writing_prompt_id"] is None
    restored = save(wid, changed["content_revision"], brief={"content_route": "short_video_storyboard"})
    assert restored.status_code == 200
    assert st.get("works", wid)["content_revision"] == before["content_revision"] + 2
    assert not selection.current_selection_valid(wid)
