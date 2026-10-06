"""底片检查使用当前选定源图，并返回可以预览的源图地址。"""
from fastapi.testclient import TestClient

from app import main, storage as st
from app.digital_human.errors import TalkingHeadRejected


def test_plate_preparation_forwards_variant_and_exposes_source_preview(monkeypatch):
    work_id = st.new_id("wplate")
    st.put("works", work_id, {"id": work_id})
    received = []

    def status(wid, *, variant_id=None):
        received.append((wid, variant_id))
        return {"plates": [], "preparation": {"ok": True, "blockers": [],
                "source": {"storage_uri": "assets/人物/原图.png"}}}

    monkeypatch.setattr(main.dh_plates, "plate_status", status)
    response = TestClient(main.app).get(
        f"/api/v1/works/{work_id}/talking-head/plate?variant_id=var_selected")
    assert response.status_code == 200
    assert received == [(work_id, "var_selected")]
    from urllib.parse import parse_qs, urlparse
    uri = parse_qs(urlparse(response.json()["preparation"]["source"]["url"]).query)["uri"][0]
    assert uri == "assets/人物/原图.png", "人物原图是工作区资产，不是运行目录中的后期文件"


def test_explicit_plate_source_is_bound_before_generation(monkeypatch):
    work_id = st.new_id("wplate")
    st.put("works", work_id, {"id": work_id, "identity_id": "av_current", "variant_id": "var_old"})
    old_id = st.new_id("vid")
    st.put("videos", old_id, {"id": old_id, "work_id": work_id, "kind": "plate",
                             "approval_state": "approved", "route": "M2"}, {"work_id": work_id})
    monkeypatch.setattr(main.dh_plates, "plate_status", lambda *a, **kw: {
        "preparation": {"ok": True, "source": {"variant_id": "var_selected", "asset_id": "img_selected"}}})
    def create(*a, **kw):
        assert st.get("works", work_id)["variant_id"] == "var_selected"
        assert st.get("videos", old_id)["approval_state"] == "revoked"
        return {"registered": True}
    monkeypatch.setattr(main.dh_plates, "create_plate", create)
    monkeypatch.setattr(main, "_m2_transport", lambda: None)
    response = TestClient(main.app).post(f"/api/v1/works/{work_id}/talking-head/plate",
                                       json={"variant_id": "var_selected", "dry_run": False})
    assert response.status_code == 200


def test_invalid_explicit_plate_source_never_changes_binding(monkeypatch):
    work_id = st.new_id("wplate")
    st.put("works", work_id, {"id": work_id, "identity_id": "av_current", "variant_id": "var_old"})
    monkeypatch.setattr(main.dh_plates, "plate_status", lambda *a, **kw: {
        "preparation": {"ok": False, "blockers": [{"code": "VARIANT_NOT_APPROVED", "message": "未审核"}]}})
    def create(*a, **kw):
        raise TalkingHeadRejected("VARIANT_NOT_APPROVED", "未审核")
    monkeypatch.setattr(main.dh_plates, "create_plate", create)
    monkeypatch.setattr(main, "_m2_transport", lambda: None)
    response = TestClient(main.app).post(f"/api/v1/works/{work_id}/talking-head/plate",
                                       json={"variant_id": "var_bad", "dry_run": False})
    assert response.status_code in (409, 422)
    assert st.get("works", work_id)["variant_id"] == "var_old"
