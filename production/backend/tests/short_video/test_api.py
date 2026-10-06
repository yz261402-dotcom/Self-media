"""Storyboard HTTP operations are explicit, bounded, and read-only on GET."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.short_video.test_storyboard import prepared


def test_http_graph_creation_and_readback(prepared):
    _, graph, _ = prepared
    client = TestClient(app)
    got = client.get(f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}")
    assert got.status_code == 200
    assert got.json()["plan_digest"] == graph["plan_digest"]
    listed = client.get(f"/api/v1/works/{graph['work_id']}/wan/storyboards")
    assert [g["id"] for g in listed.json()["graphs"]] == [graph["id"]]
    new = client.post(f"/api/v1/works/{graph['work_id']}/wan/storyboards", json={"image_ceiling_fen_per_shot": 100})
    assert new.status_code == 200 and new.json()["id"] == graph["id"]


def test_http_paid_submit_rejects_without_user_ack(prepared):
    _, graph, _ = prepared
    client = TestClient(app)
    base = f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}"
    response = client.post(base + "/images/generate", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STORYBOARD_IMAGE_APPROVAL_REQUIRED"


def test_http_worker_only_queues_stitch(prepared):
    _, graph, _ = prepared
    client = TestClient(app)
    base = f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}"
    response = client.post(base + "/stitch", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STORYBOARD_CLIP_REVIEW_REQUIRED"


def test_http_image_recovery_requires_receipt_and_is_bound_to_work(prepared):
    from tests.short_video.test_storyboard import approve_images
    service, graph, _ = prepared
    approve_images(service, graph)
    service.generate_images(graph["id"], transport=lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    client = TestClient(app)
    path = f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}/shots/1/image/recover"
    response = client.post(path, json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STORYBOARD_IMAGE_RECEIPT_MISSING"
    assert client.post(path.replace(graph["work_id"], "different-work"), json={}).status_code == 404


def test_http_recovery_restores_original_image_and_get_never_recovers(prepared, monkeypatch):
    from tests.short_video.test_storyboard import approve_images, image_transport
    from app import jobs
    service, graph, _ = prepared
    approve_images(service, graph)
    original = service._store
    monkeypatch.setattr(service, "_store", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk failed")))
    stalled = service.generate_images(graph["id"], transport=image_transport([]))
    monkeypatch.setattr(service, "_store", original)
    client = TestClient(app)
    path = f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}"
    before = jobs.get_job(stalled["shots"][0]["image_job_id"])
    assert client.get(path).json()["shots"][0]["image_state"] == "unknown"
    assert jobs.get_job(before["id"]) == before
    response = client.post(path + "/shots/1/image/recover", json={})
    assert response.status_code == 200
    image = response.json()["shots"][0]["image"]
    assert response.json()["shots"][1]["image_state"] == "pending"
    assert client.get(image["preview_url"]).status_code == 200
    receipt = jobs.get_job(before["id"])["image_receipt"]
    assert client.get("/api/v1/media", params={"uri": receipt["storage_uri"]}).status_code != 200
