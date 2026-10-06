"""复制为新草稿必须复用真实图，不能继承审核或污染旧作品。"""
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app import storage as st
from app.avatar import licensing, person_image, registry


def setup_copy(tmp_path, monkeypatch, scope="commercial_ok"):
    from app.avatar_draft_api import router
    app = FastAPI()
    app.include_router(router)
    path = tmp_path / "人物.png"
    Image.new("RGB", (40, 60), "#775544").save(path)
    monkeypatch.setattr(person_image, "resolve_media_path", lambda _: str(path))
    old = registry.create_identity_candidate("原人物", "专业温柔", origin="generated")
    lic = licensing.register(old["name"], "image_source", scope=scope,
                             evidence="原图来源证据", verified_by="user_local")
    hero = registry.register_asset(old["id"], "hero", "人物.png",
                                   "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                                   "真实来源证据", width=40, height=60)
    hero.update(approval_state="approved", license_id=lic["id"])
    st.put("avatar_assets", hero["id"], hero, {"identity_id": old["id"]})
    old.update(frozen=True, approval_state="approved", hero_asset_id=hero["id"], license_id=lic["id"],
               reference_policy={"mode": "multi_view"})
    st.put("identities", old["id"], old)
    return TestClient(app), old, hero, lic, path


def test_copy_is_a_separate_pending_identity_with_same_real_hero(tmp_path, monkeypatch):
    client, old, hero, lic, _ = setup_copy(tmp_path, monkeypatch)
    work_id = st.new_id("work")
    work = {"id": work_id, "identity_id": old["id"]}
    st.put("works", work_id, work)
    res = client.post(f'/api/v1/identities/{old["id"]}/draft-copy', json={"actor_id": "user_local"})
    assert res.status_code == 200, res.text
    new = res.json()
    copied = registry.get_asset(new["hero_asset_id"])
    assert new["id"] != old["id"]
    assert not new["frozen"] and new["approval_state"] == "pending"
    assert new["reference_policy"] is None and new["reference_asset_ids"] == []
    assert copied["approval_state"] == "pending" and not copied["is_mock"]
    assert copied["storage_uri"] == hero["storage_uri"] and copied["file_hash"] == hero["file_hash"]
    assert old["id"] in copied["source_evidence"] and hero["id"] in copied["source_evidence"]
    assert new["license_id"] != lic["id"]
    assert licensing.get(new["license_id"])["scope"] == "unverified"
    assert registry.get_identity(old["id"]) == old
    assert registry.get_asset(hero["id"]) == hero
    assert licensing.get(lic["id"]) == lic
    assert st.get("works", work_id) == work
    assert st.all_rows("identity_reviews", "asset_id=?", [copied["id"]]) == []


def test_copy_cannot_clear_a_blocked_license(tmp_path, monkeypatch):
    client, old, _, _, _ = setup_copy(tmp_path, monkeypatch, scope="blocked")
    res = client.post(f'/api/v1/identities/{old["id"]}/draft-copy', json={"actor_id": "user_local"})
    assert res.status_code == 200, res.text
    assert licensing.get(res.json()["license_id"])["scope"] == "blocked"


@pytest.mark.parametrize("bad", ["actor", "mock", "hash", "missing", "invalid_image"])
def test_invalid_copy_is_rejected_without_partial_records(tmp_path, monkeypatch, bad):
    client, old, hero, _, path = setup_copy(tmp_path, monkeypatch)
    if bad == "mock":
        hero["is_mock"] = True
    if bad == "hash":
        hero["file_hash"] = "sha256:" + "0" * 64
    if bad == "missing":
        path.unlink()
    if bad == "invalid_image":
        path.write_bytes(b"not an image")
        hero["file_hash"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    st.put("avatar_assets", hero["id"], hero, {"identity_id": old["id"]})
    before = [len(st.all_rows(t)) for t in ("identities", "avatar_assets", "license_records")]
    res = client.post(f'/api/v1/identities/{old["id"]}/draft-copy',
                      json={"actor_id": "agent" if bad == "actor" else "user_local"})
    assert res.status_code in (409, 422), res.text
    assert before == [len(st.all_rows(t)) for t in ("identities", "avatar_assets", "license_records")]
