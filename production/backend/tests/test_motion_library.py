"""动作库只复用已登记的真实驱动，不继承人物身份审批。"""
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.avatar import licensing, motion, registry
from app.contracts import MotionReference


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setattr(st, "_local", threading.local())
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    st.init_db()
    source = registry.create_identity_candidate("来源人物", "动作来源")
    target = registry.create_identity_candidate("我的主播", "新人物")
    path = tmp_path / "assets" / "动作参考" / "lecture.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x00\x00\x00\x20ftypisom" + bytes(range(100)))
    asset = registry.register_asset(
        source["id"], "motion", "https://assets.example/motion/lecture.mp4",
        "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "项目已生成动作驱动参考，仅迁移动作，不使用来源人物的脸",
    )
    ref = motion.register_motion_reference(
        source["id"], "smile", asset["id"], label="自然讲述", description="眨眼、轻微转头",
        loop_seconds=15.24, note="循环参考，需逐片检查循环衔接",
    )
    ref = motion.approve_reference(ref["id"])
    # 从正式应用进入，确保路由真正挂载。
    from app.main import app
    return TestClient(app), source, target, asset, ref, path


def _import(client, target, source, **extra):
    return client.post(f'/api/v1/identities/{target["id"]}/motions/import', json={
        "source_reference_id": source["id"], "actor_id": "user_local", "confirmed": True, **extra,
    })


def test_library_exposes_real_approved_reference_and_no_catalog_placeholders(library):
    client, source, _, asset, ref, _ = library
    assert asset["approval_state"] == "pending"
    res = client.get("/api/v1/motion-templates")
    assert res.status_code == 200, res.text
    item, = res.json()["templates"]
    assert item["id"] == ref["id"] and item["available"] is True
    assert item["source_identity_name"] == source["name"]
    assert item["preview_url"].endswith(f'/{ref["id"]}/preview')
    assert item["loop_seconds"] == 15.24
    assert "循环" in item["note"] and "许可" in item["note"]


def test_import_is_idempotent_preserves_sources_and_does_not_approve_identity(library):
    client, source, target, asset, ref, _ = library
    work = {"id": "work_motion", "identity_id": target["id"]}
    st.put("works", work["id"], work)
    first = _import(client, target, ref)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["identity_id"] == target["id"] and result["id"] != ref["id"]
    assert result["source_reference_id"] == ref["id"]
    assert result["approval_state"] == "approved" and result["asset_id"] == asset["id"]
    assert result["supported_params"] == ref["supported_params"]
    assert ref["note"] in result["note"] and "user_local" in result["note"]
    assert _import(client, target, ref).json() == result
    assert registry.get_identity(target["id"]) == target
    assert registry.get_identity(source["id"]) == source
    assert registry.get_asset(asset["id"]) == asset
    assert st.get("motion_references", ref["id"]) == ref
    assert st.get("works", work["id"]) == work
    assert not st.all_rows("identity_reviews")
    assert [r["id"] for r in client.get("/api/v1/motion-templates").json()["templates"]] == [ref["id"]]
    resolved = client.post(f'/api/v1/identities/{target["id"]}/motions/resolve', json={
        "intent": {"motion_key": "smile", "motion_ref_id": result["id"], "params": {}}, "route": "M2",
    })
    assert resolved.status_code == 200 and resolved.json()["asset_id"] == asset["id"]


def test_preview_streams_registered_local_file_and_supports_range(library):
    client, _, _, _, ref, path = library
    res = client.get(f'/api/v1/motion-templates/{ref["id"]}/preview', headers={"Range": "bytes=0-15"})
    assert res.status_code == 206, res.text
    assert res.content == path.read_bytes()[:16]
    assert res.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize("changes", [{"confirmed": False}, {"confirmed": "true"}, {"actor_id": "agent"}])
def test_import_requires_explicit_user_confirmation(library, changes):
    client, _, target, _, ref, _ = library
    res = _import(client, target, ref, **changes)
    assert res.status_code == 422
    assert motion.list_references(target["id"]) == []


@pytest.mark.parametrize("bad", ["missing", "hash", "mock", "unapproved", "evidence", "asset_rejected"])
def test_invalid_sources_cannot_be_imported_or_previewed(library, bad):
    client, _, target, asset, ref, path = library
    if bad == "missing":
        path.unlink()
    if bad == "hash":
        path.write_bytes(b"changed")
    if bad == "mock":
        asset["is_mock"] = True
    if bad == "evidence":
        asset["source_evidence"] = ""
    if bad == "asset_rejected":
        asset["approval_state"] = "rejected"
    if bad == "unapproved":
        ref["approval_state"] = "pending"
    st.put("avatar_assets", asset["id"], asset, {"identity_id": asset["identity_id"]})
    st.put("motion_references", ref["id"], ref, {"identity_id": ref["identity_id"]})
    assert _import(client, target, ref).status_code == 409
    assert client.get(f'/api/v1/motion-templates/{ref["id"]}/preview').status_code == 409
    assert motion.list_references(target["id"]) == []
    listed = client.get("/api/v1/motion-templates").json()["templates"]
    assert all(item["available"] is False and item["unavailable_reason"] for item in listed)


@pytest.mark.parametrize("location", ["ref", "asset"])
@pytest.mark.parametrize("scope", ["unverified", "blocked"])
def test_license_restrictions_are_checked_on_reference_and_asset(library, location, scope):
    client, _, target, asset, ref, _ = library
    lic = licensing.register("动作来源", "image_source", scope=scope, evidence="来源证据")
    row, table = (ref, "motion_references") if location == "ref" else (asset, "avatar_assets")
    row["license_id"] = lic["id"]
    st.put(table, row["id"], row, {"identity_id": row["identity_id"]})
    res = _import(client, target, ref)
    assert res.status_code == 409
    assert "LICENSE" in res.json()["error"]["code"]


def test_revoked_source_cannot_be_imported_again_even_if_already_copied(library):
    client, _, target, _, ref, _ = library
    assert _import(client, target, ref).status_code == 200
    ref["approval_state"] = "rejected"
    st.put("motion_references", ref["id"], ref, {"identity_id": ref["identity_id"]})
    assert _import(client, target, ref).status_code == 409


def test_simultaneous_imports_create_one_reference(library):
    client, _, target, _, ref, _ = library
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _import(client, target, ref), range(2)))
    assert all(r.status_code == 200 for r in results)
    assert results[0].json()["id"] == results[1].json()["id"]
    assert len(motion.list_references(target["id"])) == 1


def test_preview_rejects_unregistered_paths_and_external_local_files(library):
    client, _, target, asset, ref, path = library
    asset["storage_uri"] = str(path.resolve())
    st.put("avatar_assets", asset["id"], asset, {"identity_id": asset["identity_id"]})
    assert _import(client, target, ref).status_code == 409
    assert client.get("/api/v1/motion-templates/unregistered/preview").status_code == 404


@pytest.mark.parametrize("change", ["source_revoked", "asset_hash", "source_params", "license_revoked"])
def test_already_imported_motion_rechecks_source_at_resolution(library, change):
    client, _, target, asset, ref, _ = library
    imported = _import(client, target, ref).json()
    if change == "source_revoked":
        ref["approval_state"] = "rejected"
    elif change == "asset_hash":
        asset["file_hash"] = "sha256:" + "0" * 64
    elif change == "source_params":
        ref["supported_params"] = {"intensity": {"min": 0.1, "max": 0.2}}
    elif change == "license_revoked":
        asset["license_id"] = licensing.register("撤销的动作来源", "image_source", scope="blocked")["id"]
    st.put("motion_references", ref["id"], ref, {"identity_id": ref["identity_id"]})
    st.put("avatar_assets", asset["id"], asset, {"identity_id": asset["identity_id"]})
    res = client.post(f'/api/v1/identities/{target["id"]}/motions/resolve', json={
        "intent": {"motion_key": "smile", "motion_ref_id": imported["id"], "params": {}}, "route": "M2",
    })
    assert res.status_code in (409, 422), res.text
    assert st.get("motion_references", imported["id"])["approval_state"] == "approved"


def test_imported_reference_conforms_to_shared_strict_contract(library):
    client, _, target, _, ref, _ = library
    imported = _import(client, target, ref).json()
    assert MotionReference.model_validate(imported).model_dump() == imported
