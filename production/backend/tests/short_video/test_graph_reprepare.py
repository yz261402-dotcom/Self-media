"""Reprepare a revision without charging again for verified unchanged images."""
import copy
import hashlib

import pytest

from app import jobs, storage as st
from app.avatar import person_image
from tests.short_video.test_storyboard import (
    approve_images, attach_storyboard_identity, image_transport, prepared,
)


def _revision(service, graph):
    work = st.get("works", graph["work_id"])
    work["content_revision"] += 1
    st.put("works", work["id"], work)
    return service.create_graph(work["id"], image_ceiling_fen_per_shot=100)


def _completed(prepared, monkeypatch, *, partial=False):
    service, graph, root = prepared
    approve_images(service, graph)
    if partial:
        original = service._generate_image_job

        def stop_after_first(graph, shot, *args, **kwargs):
            result = original(graph, shot, *args, **kwargs)
            if shot["shot_index"] == 1:
                raise SystemExit("stop between shots")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(service, "_generate_image_job", stop_after_first)
            with pytest.raises(SystemExit):
                service.generate_images(graph["id"], transport=image_transport([]))
    else:
        service.generate_images(graph["id"], transport=image_transport([]))
    return service, service.get_graph(graph["id"]), root


def test_revision_reprepare_reuses_successful_images_with_new_approval(prepared, monkeypatch):
    service, source, _ = _completed(prepared, monkeypatch, partial=True)
    snapshots = {s["image_job_id"]: jobs.get_job(s["image_job_id"])
                 for s in source["shots"] if s.get("image_job_id")}
    job_ids = {j["id"] for j in st.all_rows("provider_jobs")}
    replacement = _revision(service, source)
    raw = service.get_graph(replacement["id"])
    assert replacement["id"] != source["id"]
    assert raw["plan_digest"] != source["plan_digest"]
    assert raw["content_revision"] == source["content_revision"] + 1
    assert replacement["state"] == "waiting_image_approval"
    assert raw["image_approval"] is raw["video_approval"] is raw["clip_approval"] is None
    assert raw["image_reuse"]["source_graph_id"] == source["id"]
    assert raw["image_reuse"]["source_plan_digest"] == source["plan_digest"]
    assert raw["image_reuse"]["shot_indexes"] == [1]
    assert raw["shots"][0]["image"] == source["shots"][0]["image"]
    assert raw["shots"][0]["image_job_id"] == source["shots"][0]["image_job_id"]
    assert raw["shots"][0]["image_state"] == "succeeded"
    assert raw["shots"][1]["image_state"] == "pending"
    assert service.get_graph(source["id"]) == source
    assert {jid: jobs.get_job(jid) for jid in snapshots} == snapshots
    assert {j["id"] for j in st.all_rows("provider_jobs")} == job_ids
    service.assert_current(raw)
    calls = []
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(raw["id"], transport=image_transport(calls))
    assert exc.value.code == "STORYBOARD_IMAGE_APPROVAL_REQUIRED"
    assert calls == []
    approve_images(service, replacement)
    result = service.generate_images(raw["id"], transport=image_transport(calls))
    assert result["state"] == "images_ready"
    assert len(calls) == 1
    assert service.get_graph(source["id"]) == source
    assert {jid: jobs.get_job(jid) for jid in snapshots} == snapshots


def test_all_images_reused_still_wait_for_current_approval(prepared, monkeypatch):
    service, source, _ = _completed(prepared, monkeypatch)
    replacement = _revision(service, source)
    assert replacement["state"] == "waiting_image_approval"
    assert replacement["image_reuse"]["shot_indexes"] == [1, 2]
    approve_images(service, replacement)
    result = service.generate_images(replacement["id"], transport=lambda *a, **k: pytest.fail("No new images"))
    assert result["state"] == "images_ready"


def test_reprepare_keeps_verified_identity_reference_images(prepared, monkeypatch):
    service, graph, root = prepared
    attach_storyboard_identity(graph, root)
    graph = service.create_graph(graph["work_id"], image_ceiling_fen_per_shot=100)
    service, source, _ = _completed((service, graph, root), monkeypatch)
    replacement = service.get_graph(_revision(service, source)["id"])
    assert replacement["image_reuse"]["shot_indexes"] == [1, 2]
    assert replacement["identity_reference"] == source["identity_reference"]
    for shot in replacement["shots"]:
        assert shot["image"]["identity_reference"] == source["identity_reference"]


def test_repeated_reprepare_preserves_original_job_binding_and_new_success(prepared, monkeypatch):
    service, source, _ = _completed(prepared, monkeypatch, partial=True)
    second = _revision(service, source)
    approve_images(service, second)
    service.generate_images(second["id"], transport=image_transport([]))
    second = service.get_graph(second["id"])
    third = service.get_graph(_revision(service, second)["id"])
    assert third["image_reuse"]["source_graph_id"] == second["id"]
    assert third["image_reuse"]["shot_indexes"] == [1, 2]
    assert third["shots"][0]["image_job_id"] == source["shots"][0]["image_job_id"]
    assert third["shots"][1]["image_job_id"] == second["shots"][1]["image_job_id"]
    assert third["shots"][0]["image_reuse"]["job_graph_id"] == source["id"]
    assert third["shots"][1]["image_reuse"]["job_graph_id"] == second["id"]


def test_reused_image_can_be_explicitly_regenerated_once(prepared, monkeypatch):
    service, source, _ = _completed(prepared, monkeypatch)
    source_jobs = {s["image_job_id"]: jobs.get_job(s["image_job_id"]) for s in source["shots"]}
    replacement = _revision(service, source)
    approve_images(service, replacement)
    calls = []
    request = dict(actor_id="user", plan_digest=replacement["plan_digest"], paid_ack=True,
        duplicate_charge_ack=True, expected_image_job_id=source["shots"][0]["image_job_id"],
        request_id="reused-replacement", ceiling_fen=100, transport=image_transport(calls))
    result = service.regenerate_image(replacement["id"], 1, **request)
    service.regenerate_image(replacement["id"], 1, **request)
    raw = service.get_graph(replacement["id"])
    assert len(calls) == 1
    assert result["shots"][0]["image_state"] == "succeeded"
    assert raw["shots"][0]["image_job_id"] != source["shots"][0]["image_job_id"]
    assert "image_reuse" not in raw["shots"][0]
    assert raw["shots"][0]["image_history"][-1]["image_reuse"]["job_graph_id"] == source["id"]
    assert service.get_graph(source["id"]) == source
    assert {jid: jobs.get_job(jid) for jid in source_jobs} == source_jobs


@pytest.mark.parametrize("change", ["graph", "digest", "source_image", "source_prompt"])
def test_reused_image_regeneration_rejects_changed_provenance(prepared, monkeypatch, change):
    service, source, _ = _completed(prepared, monkeypatch)
    replacement = _revision(service, source)
    approve_images(service, replacement)
    raw = service.get_graph(replacement["id"])
    if change == "graph":
        raw["shots"][0]["image_reuse"]["job_graph_id"] = "missing-graph"
    elif change == "digest":
        raw["shots"][0]["image_reuse"]["job_plan_digest"] = "invalid"
    elif change == "source_image":
        source["shots"][0]["image"]["asset_id"] = "changed"
        st.put("media_jobs", source["id"], source, {"work_id": source["work_id"]})
    else:
        source["shots"][0]["image_prompt"] += " changed"
        source["plan_digest"] = service._plan_digest(source)
        st.put("media_jobs", source["id"], source, {"work_id": source["work_id"]})
    st.put("media_jobs", raw["id"], raw, {"work_id": raw["work_id"]})
    with pytest.raises(service.StoryboardError) as exc:
        service.regenerate_image(raw["id"], 1, actor_id="user", plan_digest=raw["plan_digest"], paid_ack=True,
            duplicate_charge_ack=True, expected_image_job_id=raw["shots"][0]["image_job_id"],
            request_id="bad-source", ceiling_fen=100,
            transport=lambda *a, **k: pytest.fail("Invalid source must never be sent"))
    assert exc.value.code == "STORYBOARD_IMAGE_ATTEMPT_CHANGED"


def test_reprepare_keeps_regeneration_attempt_and_history(prepared, monkeypatch):
    service, source, _ = _completed(prepared, monkeypatch)
    shot = source["shots"][0]
    service.regenerate_image(source["id"], 1, actor_id="user", plan_digest=source["plan_digest"],
        paid_ack=True, duplicate_charge_ack=True, expected_image_job_id=shot["image_job_id"],
        request_id="replacement-one", ceiling_fen=100, transport=image_transport([]))
    source = service.get_graph(source["id"])
    raw = service.get_graph(_revision(service, source)["id"])
    for key in ("image_job_id", "image", "image_attempt", "image_history"):
        assert raw["shots"][0][key] == source["shots"][0][key]


@pytest.mark.parametrize("change", ["script_id", "script_digest", "prompt", "identity", "model", "size", "outfit", "mock"])
def test_changed_generation_inputs_do_not_reuse(prepared, monkeypatch, change):
    service, source, root = _completed(prepared, monkeypatch)
    if change == "identity":
        attach_storyboard_identity(source, root)
    elif change in ("script_id", "script_digest", "prompt"):
        version = st.get("script_versions", source["script_version_id"])
        if change == "script_id":
            version["id"] = st.new_id("script")
            work = st.get("works", source["work_id"])
            work["script_version_id"] = version["id"]
            st.put("works", work["id"], work)
        elif change == "script_digest":
            version["text"] += " changed"
        else:
            version["storyboard"][0]["visual"] += " changed"
        st.put("script_versions", version["id"], version, {"work_id": version["work_id"]})
        batch = st.get("script_batches", source["work_id"])
        batch["candidates"][0] = version
        st.put("script_batches", source["work_id"], batch)
    else:
        source[{"model": "image_model", "size": "image_size", "outfit": "outfit_policy", "mock": "is_mock"}[change]] = (
            True if change == "mock" else "different")
        source["plan_digest"] = service._plan_digest(source)
        st.put("media_jobs", source["id"], source, {"work_id": source["work_id"]})
    replacement = service.get_graph(_revision(service, source)["id"])
    assert "image_reuse" not in replacement
    assert all(s["image_state"] == "pending" and not s.get("image_job_id") for s in replacement["shots"])


@pytest.mark.parametrize("change", ["image_state", "job_state", "job_work", "job_capability", "job_model",
    "job_provider", "job_args", "job_result", "job_result_digest", "file_hash", "invalid_image",
    "dimensions", "missing_file", "plan_digest"])
def test_unverified_success_is_not_reused(prepared, monkeypatch, change):
    service, source, root = _completed(prepared, monkeypatch)
    shot = source["shots"][0]
    job = jobs.get_job(shot["image_job_id"])
    if change.startswith("job_"):
        key = {"job_state": "state", "job_work": "work_id", "job_capability": "capability",
               "job_model": "model", "job_provider": "provider", "job_args": "args_digest",
               "job_result_digest": "result_digest"}.get(change)
        if key:
            job[key] = "different"
        else:
            job["result"]["image"]["asset_id"] = "different"
        st.put("provider_jobs", job["id"], job, {"work_id": job["work_id"]})
    elif change == "image_state":
        shot["image_state"] = "failed"
    elif change == "plan_digest":
        source["shots"][0]["image_prompt"] += " corrupt"
    elif change == "file_hash":
        (root / shot["image"]["storage_uri"]).write_bytes(b"changed bytes")
    elif change == "invalid_image":
        data = b"not a decoded image"
        (root / shot["image"]["storage_uri"]).write_bytes(data)
        shot["image"]["asset_hash"] = "sha256:" + hashlib.sha256(data).hexdigest()
        job["result"]["image"] = copy.deepcopy(shot["image"])
        job["result_digest"] = jobs.args_digest(job["result"])
        st.put("provider_jobs", job["id"], job, {"work_id": job["work_id"]})
    elif change == "dimensions":
        shot["image"]["width"] += 1
        job["result"]["image"] = copy.deepcopy(shot["image"])
        job["result_digest"] = jobs.args_digest(job["result"])
        st.put("provider_jobs", job["id"], job, {"work_id": job["work_id"]})
    else:
        (root / shot["image"]["storage_uri"]).unlink()
    st.put("media_jobs", source["id"], source, {"work_id": source["work_id"]})
    replacement = service.get_graph(_revision(service, source)["id"])
    assert replacement["shots"][0]["image_state"] == "pending"
    assert not replacement["shots"][0].get("image_job_id")


@pytest.mark.parametrize("change", ["image_sending", "image_unknown", "image_blocked", "video_state",
    "video_job", "video_intent", "remote", "clip", "output"])
def test_unresolved_or_video_started_graph_is_never_reused(prepared, monkeypatch, change):
    service, source, _ = _completed(prepared, monkeypatch)
    shot = source["shots"][1]
    if change.startswith("image_"):
        shot["image_state"] = change.removeprefix("image_")
    elif change == "video_state":
        shot["video_state"] = "failed"
    elif change == "video_job":
        shot["video_job_id"] = "video-intent"
    elif change == "video_intent":
        jobs.submit(work_id=source["work_id"], capability="wan_video", provider="wan", model="wan",
            args={}, idempotency_key=f"{source['id']}:wan:2")
    elif change == "remote":
        shot["remote_task_id"] = "remote-video"
    elif change == "clip":
        shot["clip"] = {"asset_id": "clip"}
    else:
        source["output"] = {"video_id": "output"}
    st.put("media_jobs", source["id"], source, {"work_id": source["work_id"]})
    replacement = _revision(service, source)
    raw = service.get_graph(replacement["id"])
    assert "image_reuse" not in raw
    assert all(s["image_state"] == "pending" for s in raw["shots"])
    if change in ("image_unknown", "image_sending"):
        approve_images(service, replacement)
        with pytest.raises(service.StoryboardError) as exc:
            service.generate_images(raw["id"], transport=lambda *a, **k: pytest.fail("No resend"))
        assert exc.value.code == "STORYBOARD_RECONCILIATION_REQUIRED"


def test_identical_plan_replay_reads_raw_snapshot_despite_runtime_projection(prepared, monkeypatch):
    service, graph, _ = prepared
    monkeypatch.setenv("AVATAR_STUDIO_WAN_STORYBOARD_BUDGET_UNLIMITED", "1")
    projected = service.project(service.get_graph(graph["id"]))
    assert projected["estimate"]["budget_unlimited"] is True
    monkeypatch.setenv("AVATAR_STUDIO_WAN_STORYBOARD_BUDGET_UNLIMITED", "0")
    monkeypatch.setattr(service, "list_graphs", lambda wid: [projected])
    replay = service.create_graph(graph["work_id"], image_ceiling_fen_per_shot=100)
    assert replay["id"] == graph["id"]
    assert replay["estimate"]["budget_unlimited"] is False
    assert service.get_graph(graph["id"])["estimate"]["budget_unlimited"] is False


@pytest.mark.parametrize("state", ["intent", "queued", "leased", "unknown", "succeeded", "failed",
    "cancelled_unsettled", "cancelled_after_send"])
def test_orphan_image_intent_blocks_cross_graph_automatic_generation(prepared, monkeypatch, state):
    service, source, _ = _completed(prepared, monkeypatch, partial=True)
    pending = source["shots"][1]
    job = jobs.submit(work_id=source["work_id"], capability="storyboard_image", provider=person_image.PROVIDER,
        model=source["image_model"], args=service._image_args(source, pending),
        idempotency_key=f"{source['id']}:image:2", billable=True, amount_fen=100, namespace="wan-storyboard")
    job["state"] = "cancelled" if state.startswith("cancelled_") else state
    if state == "cancelled_unsettled":
        job["settlement_required"] = True
    elif state == "cancelled_after_send":
        job.update(attempts=1, sent_at="2026-10-07T00:00:00", settlement_required=False)
    st.put("provider_jobs", job["id"], job, {"work_id": job["work_id"]})
    snapshot = jobs.get_job(job["id"])
    replacement = _revision(service, source)
    assert "image_reuse" not in replacement
    assert service.get_graph(source["id"]) == source
    approve_images(service, replacement)
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(replacement["id"], transport=lambda *a, **k: pytest.fail("No orphan resend"))
    assert exc.value.code == "STORYBOARD_RECONCILIATION_REQUIRED"
    assert jobs.get_job(job["id"]) == snapshot
    assert service.get_graph(source["id"]) == source


@pytest.mark.parametrize("state", ["blocked", "cancelled_never_sent"])
def test_unbound_never_sent_terminal_image_intent_does_not_block(prepared, monkeypatch, state):
    service, source, _ = _completed(prepared, monkeypatch, partial=True)
    job = jobs.submit(work_id=source["work_id"], capability="storyboard_image", provider=person_image.PROVIDER,
        model=source["image_model"], args=service._image_args(source, source["shots"][1]),
        idempotency_key=f"{source['id']}:image:2", billable=True, amount_fen=100, namespace="wan-storyboard")
    job.update(state="blocked" if state == "blocked" else "cancelled", settlement_required=False, attempts=0)
    st.put("provider_jobs", job["id"], job, {"work_id": job["work_id"]})
    replacement = _revision(service, source)
    assert replacement["image_reuse"]["shot_indexes"] == [1]
    approve_images(service, replacement)
    calls = []
    completed = service.generate_images(replacement["id"], transport=image_transport(calls))
    assert len(calls) == 1 and completed["state"] == "images_ready"


def test_cross_graph_fee_guard_rejects_orphan_before_submission(prepared, monkeypatch):
    service, source, _ = _completed(prepared, monkeypatch, partial=True)
    jobs.submit(work_id=source["work_id"], capability="storyboard_image", provider=person_image.PROVIDER,
        model=source["image_model"], args=service._image_args(source, source["shots"][1]),
        idempotency_key=f"{source['id']}:image:2", billable=True, amount_fen=100, namespace="wan-storyboard")
    with pytest.raises(service.StoryboardError) as exc:
        service._assert_no_other_unresolved_graphs({"id": "replacement", "work_id": source["work_id"]})
    assert exc.value.code == "STORYBOARD_RECONCILIATION_REQUIRED"


@pytest.mark.parametrize("scope", ["other_work", "other_capability", "similar_graph_prefix"])
def test_unbound_job_guard_is_scoped_to_exact_graph_work_and_image_capability(prepared, monkeypatch, scope):
    service, source, _ = _completed(prepared, monkeypatch, partial=True)
    job = jobs.submit(work_id=source["work_id"] if scope != "other_work" else st.new_id("work"),
        capability="storyboard_image" if scope != "other_capability" else "unrelated",
        provider=person_image.PROVIDER, model=source["image_model"], args={},
        idempotency_key=f"{source['id']}{'x' if scope == 'similar_graph_prefix' else ''}:image:2")
    job["state"] = "unknown"
    st.put("provider_jobs", job["id"], job, {"work_id": job["work_id"]})
    replacement = _revision(service, source)
    assert replacement["image_reuse"]["shot_indexes"] == [1]
    approve_images(service, replacement)
    assert service.generate_images(replacement["id"], transport=image_transport([]))["state"] == "images_ready"


def test_user_superseded_unknown_image_history_does_not_block_reprepare(prepared, monkeypatch):
    service, graph, _ = prepared
    approve_images(service, graph)
    stalled = service.generate_images(graph["id"], transport=lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    old_job_id = stalled["shots"][0]["image_job_id"]
    service.regenerate_image(graph["id"], 1, actor_id="user", plan_digest=graph["plan_digest"], paid_ack=True,
        duplicate_charge_ack=True, expected_image_job_id=old_job_id, request_id="replace-unknown",
        ceiling_fen=100, transport=image_transport([]))
    source = service.get_graph(graph["id"])
    replacement = _revision(service, source)
    assert replacement["image_reuse"]["shot_indexes"] == [1]
    approve_images(service, replacement)
    assert service.generate_images(replacement["id"], transport=image_transport([]))["state"] == "images_ready"
    assert jobs.get_job(old_job_id)["state"] == "unknown"
    assert service.get_graph(graph["id"]) == source
