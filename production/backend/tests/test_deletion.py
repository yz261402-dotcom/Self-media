"""Deletion changes visibility, while immutable evidence and shared files survive."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.contracts import AvatarIdentity, Work, WorkBrief
from app.main import app


@pytest.fixture
def http():
    return TestClient(app)


def identity(**fields):
    row = AvatarIdentity(id=st.new_id("av"), name="删除测试人物", persona="测试",
                         hero_asset_id="", identity_digest="", **fields).model_dump()
    st.put("identities", row["id"], row)
    return row


def work(**fields):
    row = Work(id=st.new_id("work"), name="删除测试作品", brief=WorkBrief(topic="隔离测试"),
               **fields).model_dump()
    st.put("works", row["id"], row)
    return row


def delete(http, kind, object_id, **payload):
    return http.request("DELETE", f"/api/v1/{kind}/{object_id}", json={"confirmed": True, **payload})


@pytest.mark.parametrize("kind", ["works", "avatar-identities"])
@pytest.mark.parametrize("confirmed", [None, False, 1, "true", [], {}])
def test_delete_requires_literal_confirmation(http, kind, confirmed):
    row = work() if kind == "works" else identity()
    response = delete(http, kind, row["id"], confirmed=confirmed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DELETE_CONFIRMATION_REQUIRED"
    assert not st.get("works" if kind == "works" else "identities", row["id"]).get("deleted_at")


@pytest.mark.parametrize("kind", ["works", "avatar-identities"])
def test_delete_missing_is_404(http, kind):
    assert delete(http, kind, "missing_" + st.new_id("id")).status_code == 404


def test_delete_work_preserves_history_files_and_other_work(http, tmp_path):
    row, other = work(), work()
    shared = tmp_path / "shared.png"
    shared.write_bytes(b"preserved-image")
    records = []
    for table in ("provider_jobs", "videos", "audios", "packages", "cost_confirmations"):
        rec = {"id": st.new_id("evidence"), "work_id": row["id"], "state": "succeeded",
               "storage_uri": str(shared), "actual_cost_fen": 32}
        st.put(table, rec["id"], rec, {"work_id": row["id"]})
        records.append((table, rec))
    response = delete(http, "works", row["id"])
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert set(receipt) == {"id", "deleted_at"}
    assert receipt["deleted_at"] and receipt["id"] == row["id"]
    assert delete(http, "works", row["id"]).json() == receipt
    visible = [w["id"] for w in http.get("/api/v1/works").json()]
    assert row["id"] not in visible and other["id"] in visible
    assert st.get("works", other["id"]) == other
    stored = st.get("works", row["id"])
    assert stored["deleted_by"] == "user_local"
    assert {k: v for k, v in stored.items() if k not in ("deleted_at", "deleted_by")} == {
        k: v for k, v in row.items() if k not in ("deleted_at", "deleted_by")}
    for table, rec in records:
        assert st.get(table, rec["id"]) == rec
    assert shared.read_bytes() == b"preserved-image"


WORK_BLOCKERS = [
    ("tasks", state) for state in ("queued", "running", "waiting_provider", "retry_wait")
] + [("provider_jobs", state) for state in ("queued", "leased", "waiting_external", "waiting_provider", "unknown", "sending")
] + [("provider_runs", state) for state in ("submitted", "waiting_provider", "unknown")
] + [("content_jobs", state) for state in ("queued", "running", "waiting_external", "unknown")
] + [("media_jobs", state) for state in ("stitch_queued", "stitching")
] + [("publish_tasks", state) for state in ("draft", "uploading", "awaiting_login", "ready_to_confirm", "submitting", "needs_verification")]


@pytest.mark.parametrize("table,state", WORK_BLOCKERS)
def test_work_blocks_active_unknown_and_publish_tasks(http, table, state):
    row = work()
    rec = {"id": st.new_id("blocking"), "work_id": row["id"], "state": state}
    extra = {"work_id": row["id"]}
    if table == "content_jobs":
        extra.update(kind="script_generation", request_id=rec["id"], input_digest="fixture", state=state)
    st.put(table, rec["id"], rec, extra)
    response = delete(http, "works", row["id"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["message"]
    assert st.get(table, rec["id"]) == rec
    assert not st.get("works", row["id"]).get("deleted_at")


@pytest.mark.parametrize("table", ["provider_jobs", "provider_runs"])
def test_cancelled_still_needing_settlement_blocks(http, table):
    row = work()
    rec = {"id": st.new_id("bill"), "work_id": row["id"], "state": "cancelled", "settlement_required": True}
    st.put(table, rec["id"], rec, {"work_id": row["id"]})
    assert delete(http, "works", row["id"]).status_code == 409


def test_succeeded_call_waiting_for_cost_settlement_does_not_block_soft_delete(http):
    """已拿到成功结果的调用不会再把作品当成仍在执行；账务记录仍保留。"""
    row = work()
    rec = {
        "id": st.new_id("succeeded_bill"), "work_id": row["id"],
        "state": "succeeded", "billable": True,
        "settlement_required": True, "settlement_state": "held",
        "actual_cost_fen": None, "result": {"file_hash": "sha256:fixture"},
        "result_digest": "sha256:fixture-result",
    }
    st.put("provider_jobs", rec["id"], rec, {"work_id": row["id"]})

    response = delete(http, "works", row["id"])

    assert response.status_code == 200, response.text
    assert st.get("provider_jobs", rec["id"]) == rec


def test_succeeded_call_without_result_still_blocks_delete(http):
    row = work()
    rec = {
        "id": st.new_id("succeeded_incomplete"), "work_id": row["id"],
        "state": "succeeded", "billable": True, "settlement_required": True,
    }
    st.put("provider_jobs", rec["id"], rec, {"work_id": row["id"]})

    response = delete(http, "works", row["id"])

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "WORK_DELETE_BLOCKED"


@pytest.mark.parametrize("field,state", [("image_state", "sending"), ("image_state", "unknown"),
    ("video_state", "sending"), ("video_state", "unknown"), ("video_state", "waiting_provider")])
def test_nested_wan_shots_block_delete(http, field, state):
    row = work()
    rec = {"id": st.new_id("graph"), "work_id": row["id"], "kind": "wan_storyboard_graph",
           "state": "waiting_clip_approval", "shots": [{field: state}]}
    st.put("media_jobs", rec["id"], rec, {"work_id": row["id"]})
    assert delete(http, "works", row["id"]).status_code == 409


def test_uncertain_publish_attempt_blocks_even_failed(http):
    row = work()
    rec = {"id": st.new_id("pub"), "work_id": row["id"], "state": "failed", "attempt_started_at": "now"}
    st.put("publish_tasks", rec["id"], rec, {"work_id": row["id"]})
    assert delete(http, "works", row["id"]).status_code == 409


@pytest.mark.parametrize("table,state", [("tasks", "waiting_review"), ("tasks", "waiting_selection"),
    ("provider_jobs", "cancelled"), ("publish_tasks", "submitted"), ("publish_tasks", "cancelled")])
def test_resolved_or_human_review_records_allow_delete(http, table, state):
    row = work()
    rec = {"id": st.new_id("resolved"), "work_id": row["id"], "state": state}
    if state == "submitted":
        rec["attempt_started_at"] = "historical"
    st.put(table, rec["id"], rec, {"work_id": row["id"]})
    assert delete(http, "works", row["id"]).status_code == 200
    assert st.get(table, rec["id"]) == rec


def test_approved_frozen_identity_can_be_deleted_with_evidence_preserved(http, tmp_path):
    ident = identity(approval_state="approved", frozen=True)
    asset = {"id": st.new_id("img"), "identity_id": ident["id"], "storage_uri": str(tmp_path / "shared.png")}
    st.put("avatar_assets", asset["id"], asset, {"identity_id": ident["id"]})
    response = delete(http, "avatar-identities", ident["id"])
    assert response.status_code == 200, response.text
    assert delete(http, "avatar-identities", ident["id"]).json() == response.json()
    assert ident["id"] not in [i["id"] for i in http.get("/api/v1/avatar-identities?include_discarded=true").json()]
    assert ident["id"] not in http.get("/api/v1/avatar-identities/readiness?include_discarded=true").json()
    assert st.get("avatar_assets", asset["id"]) == asset
    assert st.get("identities", ident["id"])["frozen"] is True


@pytest.mark.parametrize("reference", ["identity", "variant", "storyboard"])
def test_identity_delete_names_every_live_work_reference(http, reference):
    ident = identity(approval_state="approved", frozen=True)
    row = work(identity_id=ident["id"] if reference == "identity" else None)
    if reference == "variant":
        variant = {"id": st.new_id("variant"), "work_id": row["id"], "identity_id": ident["id"]}
        st.put("variants", variant["id"], variant, {"work_id": row["id"]})
    if reference == "storyboard":
        graph = {"id": st.new_id("graph"), "work_id": row["id"], "kind": "wan_storyboard_graph",
                 "identity_reference": {"identity_id": ident["id"]}, "state": "succeeded", "shots": []}
        st.put("media_jobs", graph["id"], graph, {"work_id": row["id"]})
    response = delete(http, "avatar-identities", ident["id"])
    assert response.status_code == 409, response.text
    assert row["name"] in response.json()["error"]["message"]
    assert delete(http, "works", row["id"]).status_code == 200
    assert delete(http, "avatar-identities", ident["id"]).status_code == 200


@pytest.mark.parametrize("suffix,payload", [("identity", {"identity_id": "av_base_b_v1"}),
    ("scripts", {}), ("content/prompts", {}), ("postprocess", {}), ("wan/storyboards", {})])
def test_deleted_work_cannot_start_new_operations(http, suffix, payload):
    row = work()
    assert delete(http, "works", row["id"]).status_code == 200
    response = http.post(f"/api/v1/works/{row['id']}/{suffix}", json=payload)
    assert response.status_code in (404, 409), response.text
    assert "删除" in response.json()["error"]["message"]


def test_deleted_identity_cannot_bind_restore_or_copy(http):
    ident, row = identity(approval_state="approved", frozen=True), work()
    assert delete(http, "avatar-identities", ident["id"]).status_code == 200
    response = http.post(f"/api/v1/works/{row['id']}/identity", json={"identity_id": ident["id"]})
    assert response.status_code in (404, 409)
    assert http.post(f"/api/v1/avatar-identities/{ident['id']}/restore", json={"actor": "user_local"}).status_code in (404, 409)
    assert http.post(f"/api/v1/identities/{ident['id']}/draft-copy", json={"actor_id": "user_local"}).status_code in (404, 409)
    assert st.get("works", row["id"])["identity_id"] is None


def test_create_work_avoids_deleted_default_identity(http):
    original = st.get("identities", "av_base_b_v1")
    try:
        st.put("identities", original["id"], {**original, "deleted_at": "now", "deleted_by": "user_local"})
        response = http.post("/api/v1/works", json={"topic": "删除后新建"})
        assert response.status_code == 200, response.text
        selected = response.json()["identity_id"]
        assert selected != original["id"]
        assert selected is None or not st.get("identities", selected).get("deleted_at")
    finally:
        # Restore only this isolated test fixture; the public write path cannot undelete.
        st.conn().execute("UPDATE identities SET data=? WHERE id=?", [json.dumps(original), original["id"]])


def test_concurrent_delete_is_idempotent(http):
    row = work()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: delete(http, "works", row["id"]), range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json() == responses[1].json()


def test_deleted_identity_rejected_before_image_generation(http, monkeypatch):
    from app.avatar import person_image
    ident = identity()
    assert delete(http, "avatar-identities", ident["id"]).status_code == 200
    monkeypatch.setattr(person_image, "generate", lambda **kwargs: pytest.fail("Deleted identity reached paid image generation"))
    response = http.post("/api/v1/avatar-identities/candidates/with-image", json={
        "identity_id": ident["id"], "appearance": "测试描述"})
    assert response.status_code in (404, 409), response.text
    assert response.json()["error"]["code"] == "IDENTITY_DELETED"


def test_deleted_work_rejected_from_task_body(http):
    row = work()
    assert delete(http, "works", row["id"]).status_code == 200
    response = http.post("/api/v1/tasks", json={"work_id": row["id"]})
    assert response.status_code in (404, 409)
    assert response.json()["error"]["code"] == "WORK_DELETED"
    assert not st.all_rows("tasks", "work_id=?", [row["id"]])


@pytest.mark.parametrize("table", ["works", "identities"])
def test_stale_write_cannot_resurrect_deleted_objects(http, table):
    row = work() if table == "works" else identity()
    kind = "works" if table == "works" else "avatar-identities"
    receipt = delete(http, kind, row["id"]).json()
    with pytest.raises(Exception) as error:
        st.put(table, row["id"], row)
    assert getattr(error.value, "code", None) in ("WORK_DELETED", "IDENTITY_DELETED")
    assert st.get(table, row["id"])["deleted_at"] == receipt["deleted_at"]


@pytest.mark.parametrize("service", ["content", "content_job", "provider_job", "storyboard", "tts", "plate", "publish", "presenter"])
def test_direct_creation_services_reject_deleted_objects(http, service):
    from app import jobs
    from app.agents import tools
    from app.content import context, tasks
    from app.digital_human import plates
    from app.publishing import service as publishing
    from app.short_video import service as storyboard
    from app.tts import synthesis
    ident, row = identity(approval_state="approved"), work()
    assert delete(http, "works", row["id"]).status_code == 200
    assert delete(http, "avatar-identities", ident["id"]).status_code == 200
    calls = {
        "content": lambda: context.load_context(row["id"]),
        "content_job": lambda: tasks.submit_job(row["id"], "generation", st.new_id("req"), {}),
        "provider_job": lambda: jobs.submit(work_id=row["id"], capability="tts", provider="fixture",
            model="fixture", args={}, idempotency_key=st.new_id("idem")),
        "storyboard": lambda: storyboard._selected(row["id"]),
        "tts": lambda: synthesis.synthesize_full(work_id=row["id"]),
        "plate": lambda: plates._deps(row["id"]),
        "publish": lambda: publishing.resolve_publish_package(row["id"]),
        "presenter": lambda: tools.t_get_presenter_profile({"identity_id": ident["id"]}, {}),
    }
    with pytest.raises(Exception) as error:
        calls[service]()
    assert getattr(error.value, "code", None) in ("WORK_DELETED", "IDENTITY_DELETED")
