"""Reference-image avatar creation does not call a paid provider in tests."""
from __future__ import annotations

import io
import base64
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from app import storage as st

from app.avatar import person_image
from app.avatar import image_reference
from app.avatar_image_api import router


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv(person_image.WORKSPACE_ENV, str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def png_bytes():
    out = io.BytesIO()
    Image.new("RGB", (64, 96), (100, 150, 200)).save(out, format="PNG")
    return out.getvalue()


def upload(client, data=None, content_type="image/png"):
    return client.post("/api/v1/avatar-identities/reference-images", content=data or png_bytes(),
                       headers={"content-type": content_type, "x-filename": "reference.png"})


def test_upload_normalizes_and_rejects_invalid_content(client):
    response = upload(client)
    assert response.status_code == 200
    record = response.json()
    assert record["file_hash"].startswith("sha256:")
    assert (record["width"], record["height"]) == (64, 96)
    assert record["preview_url"].startswith("/api/v1/media?uri=")
    assert upload(client, b"not-an-image").status_code == 422
    assert upload(client, content_type="application/octet-stream").status_code == 422


def test_edit_generation_is_confirmed_idempotent_and_pending(client, monkeypatch):
    uploaded = upload(client)
    assert uploaded.status_code == 200
    reference = uploaded.json()
    calls = []

    def fake_transport(request, timeout=0):
        calls.append(request)
        return {"http_status": 200, "body": {"id": "edit-1", "data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}}

    monkeypatch.setattr(image_reference, "edit_transport", fake_transport)
    body = {"reference_image_id": reference["id"], "appearance": "职业主播", "free_text": "自然微笑",
            "request_id": "test-edit-confirmed-1", "paid_ack": True}
    denied = client.post("/api/v1/avatar-identities/candidates/from-image", json={**body, "paid_ack": False})
    assert denied.status_code == 422
    assert not calls
    first = client.post("/api/v1/avatar-identities/candidates/from-image", json=body)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["identity"]["approval_state"] == "pending"
    assert result["asset"]["approval_state"] == "pending"
    assert result["reference_image"]["id"] == reference["id"]
    assert calls[0]["endpoint"] == "/v1/images/edits"
    assert calls[0]["reference_sha256"] == reference["file_hash"]
    assert "image_bytes" in calls[0]
    replay = client.post("/api/v1/avatar-identities/candidates/from-image", json=body)
    assert replay.status_code == 200
    assert replay.json() == result
    assert len(calls) == 1
    conflict = client.post("/api/v1/avatar-identities/candidates/from-image", json={**body, "appearance": "different"})
    assert conflict.status_code == 409
    queried = client.get("/api/v1/avatar-identities/image-generations/test-edit-confirmed-1")
    assert queried.status_code == 200
    assert queried.json()["state"] == "succeeded"


def test_uncertain_provider_result_is_never_retried(client, monkeypatch):
    uploaded = upload(client)
    assert uploaded.status_code == 200
    reference = uploaded.json()
    calls = []

    def uncertain(request, timeout=0):
        calls.append(request)
        raise TimeoutError("unknown")

    monkeypatch.setattr(image_reference, "edit_transport", uncertain)
    body = {"reference_image_id": reference["id"], "request_id": "test-edit-unknown-1", "paid_ack": True}
    first = client.post("/api/v1/avatar-identities/candidates/from-image", json=body)
    assert first.status_code == 409
    replay = client.post("/api/v1/avatar-identities/candidates/from-image", json=body)
    assert replay.status_code == 409
    assert len(calls) == 1
    job = client.get("/api/v1/avatar-identities/image-generations/test-edit-unknown-1").json()
    assert job["state"] == "unknown"


def test_upload_transposes_exif_and_strips_metadata(client):
    output = io.BytesIO()
    source = Image.new("RGB", (40, 70), (10, 20, 30))
    exif = source.getexif()
    exif[274] = 6
    exif[270] = "private description"
    source.save(output, format="JPEG", exif=exif)
    response = upload(client, output.getvalue(), "image/jpeg")
    assert response.status_code == 200
    record, normalized = image_reference.get_reference(response.json()["id"])
    assert (record["width"], record["height"]) == (70, 40)
    with Image.open(io.BytesIO(normalized)) as image:
        assert not image.getexif()
        assert "exif" not in image.info


def test_upload_limits_animated_and_declared_sizes(client):
    output = io.BytesIO()
    frames = [Image.new("RGB", (40, 40), color) for color in ("red", "blue")]
    frames[0].save(output, format="PNG", save_all=True, append_images=frames[1:], duration=50)
    assert upload(client, output.getvalue()).status_code == 422
    too_big = client.post("/api/v1/avatar-identities/reference-images", content=b"small",
                          headers={"content-type": "image/png", "content-length": str(10 * 1024 * 1024 + 1)})
    assert too_big.status_code == 413


def test_changed_or_missing_source_is_rejected_before_provider(client, monkeypatch):
    reference = upload(client).json()
    calls = []
    monkeypatch.setattr(image_reference, "edit_transport", lambda *a, **k: calls.append(a))
    path = person_image.resolve_media_path(reference["storage_uri"])
    from pathlib import Path
    Path(path).write_bytes(b"changed")
    response = client.post("/api/v1/avatar-identities/candidates/from-image", json={
        "reference_image_id": reference["id"], "request_id": "test-tamper-1", "paid_ack": True})
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "IMAGE_REFERENCE_HASH_MISMATCH"
    assert not calls
    Path(path).unlink()
    assert client.post("/api/v1/avatar-identities/candidates/from-image", json={
        "reference_image_id": reference["id"], "request_id": "test-missing-1", "paid_ack": True}).status_code == 409


def test_edit_transport_uses_multipart_and_normalizes_v1_base(monkeypatch):
    import httpx
    actual_client = httpx.Client
    captured = []
    monkeypatch.setenv(person_image.KEY_ENV, "test-secret")
    monkeypatch.setenv(person_image.BASE_ENV, "https://provider.invalid/v1")

    def handle(request):
        captured.append(request)
        assert request.url.path == "/v1/images/edits"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        content = request.read()
        assert b'name="image"; filename="reference.png"' in content
        assert b'gpt-image-2.5-sunburst' in content
        assert b'name="n"\r\n\r\n1' in content
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: actual_client(transport=httpx.MockTransport(handle), **kwargs))
    data = png_bytes()
    output = person_image.generate(prompt="new anchor", reference_image_bytes=data,
                                   reference_sha256=image_reference._hash(data))
    assert len(captured) == 1
    assert "image_bytes" not in output["request"]


def test_expired_running_job_stays_unknown(client):
    request_id = "test-abandoned-edit-1"
    st.put("avatar_image_generations", request_id, {"request_id": request_id, "state": "running", "input_digest": "unused",
           "started_at": time.time() - 721, "result": None, "error": None})
    first = client.get(f"/api/v1/avatar-identities/image-generations/{request_id}")
    assert first.json()["state"] == "unknown"
    assert st.get("avatar_image_generations", request_id)["state"] == "unknown"


def test_new_reference_route_rejects_existing_identity_id(client):
    reference = upload(client).json()
    response = client.post("/api/v1/avatar-identities/candidates/from-image", json={
        "reference_image_id": reference["id"], "request_id": "test-existing-identity-1",
        "paid_ack": True, "identity_id": "av_base_b_v1"})
    assert response.status_code == 422


def test_concurrent_submissions_send_only_once(client):
    reference = upload(client).json()
    entered, release = Event(), Event()
    calls = []

    def waiting(request, timeout=0):
        calls.append(request)
        entered.set()
        assert release.wait(5)
        return {"http_status": 200, "body": {"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}}

    payload = {"reference_image_id": reference["id"], "request_id": "test-concurrent-edit-1", "paid_ack": True}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(image_reference.generate_from_reference, payload, transport=waiting)
        assert entered.wait(5)
        try:
            with pytest.raises(image_reference.ReferenceImageError) as raised:
                image_reference.generate_from_reference(payload, transport=waiting)
            assert raised.value.code == "IMAGE_GENERATION_RUNNING"
        finally:
            release.set()
        result = first.result(timeout=5)
    assert result["request_id"] == payload["request_id"]
    assert len(calls) == 1


def test_source_path_outside_workspace_is_rejected(client):
    reference = upload(client).json()
    row = st.get("avatar_reference_images", reference["id"])
    row["storage_uri"] = "../outside.png"
    st.put("avatar_reference_images", reference["id"], row)
    with pytest.raises(image_reference.ReferenceImageError) as raised:
        image_reference.get_reference(reference["id"])
    assert raised.value.code == "IMAGE_REFERENCE_PATH_INVALID"


def test_pixel_limit_is_checked_before_full_decode(client, monkeypatch):
    monkeypatch.setattr(image_reference, "MAX_PIXELS", 64 * 96 - 1)
    response = upload(client)
    assert response.status_code == 422


def test_file_write_interruption_is_unknown_not_resent(client, monkeypatch):
    reference = upload(client).json()
    calls = []

    def provider(request, timeout=0):
        calls.append(request)
        return {"http_status": 200, "body": {"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}}

    def interrupted(**kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(image_reference, "edit_transport", provider)
    monkeypatch.setattr(person_image, "save_generated", interrupted)
    body = {"reference_image_id": reference["id"], "request_id": "test-disk-unknown-1", "paid_ack": True}
    assert client.post("/api/v1/avatar-identities/candidates/from-image", json=body).status_code == 409
    assert client.get("/api/v1/avatar-identities/image-generations/test-disk-unknown-1").json()["state"] == "unknown"
    assert client.post("/api/v1/avatar-identities/candidates/from-image", json=body).status_code == 409
    assert len(calls) == 1


def test_successful_source_provenance_and_license_are_preserved(client, monkeypatch):
    reference = upload(client).json()
    monkeypatch.setattr(image_reference, "edit_transport", lambda request, timeout=0: {"http_status": 200,
                       "body": {"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}})
    result = client.post("/api/v1/avatar-identities/candidates/from-image", json={
        "reference_image_id": reference["id"], "request_id": "test-license-edit-1", "paid_ack": True}).json()
    assert reference["file_hash"] in result["asset"]["source_evidence"]
    assert reference["storage_uri"] in result["asset"]["source_evidence"]
    license_row = st.get("license_records", result["asset"]["license_id"])
    assert license_row["scope"] == "unverified"
    assert not result["identity"]["frozen"]


def test_completed_asset_recovers_when_final_job_write_fails(client, monkeypatch):
    reference = upload(client).json()
    monkeypatch.setattr(image_reference, "edit_transport", lambda request, timeout=0: {"http_status": 200,
                       "body": {"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}})
    real_put = st.put
    failed = False

    def fail_success_write(table, key, data, *args, **kwargs):
        nonlocal failed
        if table == "avatar_image_generations" and data.get("state") == "succeeded" and not failed:
            failed = True
            raise OSError("job status write failed")
        return real_put(table, key, data, *args, **kwargs)

    monkeypatch.setattr(st, "put", fail_success_write)
    request_id = "test-final-write-recovery-1"
    payload = {"reference_image_id": reference["id"], "request_id": request_id, "paid_ack": True}
    assert client.post("/api/v1/avatar-identities/candidates/from-image", json=payload).status_code == 409
    status = client.get(f"/api/v1/avatar-identities/image-generations/{request_id}")
    assert status.status_code == 200
    assert status.json()["state"] == "succeeded"
    assert status.json()["result"]["asset"]["source_evidence"].find(request_id) >= 0
    replay = client.post("/api/v1/avatar-identities/candidates/from-image", json=payload)
    assert replay.status_code == 200
    assert replay.json()["asset"]["id"] == status.json()["result"]["asset"]["id"]


def test_successful_replay_does_not_need_original_reference_file(client, monkeypatch):
    reference = upload(client).json()
    calls = []

    def provider(request, timeout=0):
        calls.append(request)
        return {"http_status": 200, "body": {"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}}

    monkeypatch.setattr(image_reference, "edit_transport", provider)
    payload = {"reference_image_id": reference["id"], "request_id": "test-replay-no-source-1", "paid_ack": True}
    first = client.post("/api/v1/avatar-identities/candidates/from-image", json=payload)
    assert first.status_code == 200
    from pathlib import Path
    Path(person_image.resolve_media_path(reference["storage_uri"])).unlink()
    replay = client.post("/api/v1/avatar-identities/candidates/from-image", json=payload)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(calls) == 1
