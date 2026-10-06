"""确认清单后改输入：界面应提前获知失效，服务端仍阻止未确认的付费请求。"""
import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.main import app
from app.digital_human import cost, submission
from dh_helpers import allow_billable, authorize_cost, prepare_work


@pytest.fixture()
def client():
    return TestClient(app)


def confirm(client, work, prompt="自然讲述", resolution="1080p"):
    response = client.post(f"/api/v1/works/{work['id']}/talking-head/cost-confirmation",
                           json={"route": work["route"], "prompt": prompt,
                                 "resolution": resolution,
                                 "call_list": {"prompt": "客户端伪造的快照"}})
    assert response.status_code == 200, response.text
    return response.json()


def view(client, work, prompt="自然讲述", resolution="1080p"):
    response = client.get(f"/api/v1/works/{work['id']}/talking-head/cost",
                          params={"prompt": prompt, "resolution": resolution})
    assert response.status_code == 200, response.text
    return response.json()


def test_confirmation_stores_server_derived_inputs_and_view_reports_valid(client):
    work = prepare_work()
    rec = confirm(client, work)
    assert rec["call_list"]["prompt"] == "自然讲述"
    assert rec["call_list"]["digest"] == rec["request_digest"]
    assert rec["call_list"]["audio_id"]
    out = view(client, work)
    assert out["confirmation_valid"] is True
    assert out["changed_fields"] == []
    assert out["current_call_list"] == rec["call_list"]


@pytest.mark.parametrize("prompt,resolution,changed", [
    ("挥手讲述", "1080p", ["动作描述"]),
    ("自然讲述", "720p", ["分辨率"]),
    ("挥手讲述", "720p", ["动作描述", "分辨率"]),
])
def test_changed_input_invalidates_confirmation_and_explains_fields(client, monkeypatch,
                                                                   prompt, resolution, changed):
    allow_billable(monkeypatch)
    work = prepare_work()
    rec = confirm(client, work)
    out = view(client, work, prompt, resolution)
    assert out["confirmation"]["id"] == rec["id"], "保留旧确认用于回溯"
    assert out["confirmation_valid"] is False
    assert out["changed_fields"] == changed
    # 真提交也必须在联网前被拦，不能只靠按钮禁用。
    response = client.post(f"/api/v1/works/{work['id']}/talking-head/submissions",
                           json={"prompt": prompt, "resolution": resolution, "dry_run": False})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "COST_LIST_CHANGED"
    assert error["action"] == "confirm_cost"
    assert all(field in error["message"] for field in changed)


def test_reconfirmation_restores_submission_without_overwriting_old_record(client, monkeypatch):
    allow_billable(monkeypatch)
    work = prepare_work()
    old = confirm(client, work)
    new = confirm(client, work, "挥手讲述", "720p")
    assert old["id"] != new["id"]
    assert st.get(cost.CONFIRM_TABLE, old["id"]) == old
    assert view(client, work, "挥手讲述", "720p")["confirmation_valid"] is True
    response = client.post(f"/api/v1/works/{work['id']}/talking-head/submissions",
                           json={"prompt": "挥手讲述", "resolution": "720p", "dry_run": True})
    assert response.status_code == 200, response.text
    assert response.json()["submitted"] is False
    assert response.json()["is_mock"] is True


def test_legacy_confirmation_checks_digest_without_fabricating_snapshot(client):
    work = prepare_work()
    rec = authorize_cost(work["id"], prompt="自然讲述")
    assert not rec.get("call_list")
    assert view(client, work)["confirmation_valid"] is True
    changed = view(client, work, "挥手讲述")
    assert changed["confirmation_valid"] is False
    assert changed["changed_fields"] == ["调用清单"]


def test_missing_audio_returns_invalid_view_instead_of_error(client):
    work = client.post("/api/v1/works", json={"topic": "还未生成音频"}).json()
    out = view(client, work)
    assert out["confirmation_valid"] is False
    assert out["current_call_list"] is None


def test_m2_does_not_invalidate_for_unused_cloud_prompt_or_resolution(client):
    work = prepare_work(route="M2", with_plate=True)
    confirm(client, work)
    out = view(client, work, "不会用于 M2 的描述", "720p")
    assert out["confirmation_valid"] is True
    assert out["current_call_list"]["prompt"] == ""
    assert out["current_call_list"]["resolution"] == "native"


def test_changed_audio_hash_invalidates_even_when_prompt_is_unchanged(client):
    work = prepare_work()
    rec = confirm(client, work)
    audio_id = rec["call_list"]["audio_id"]
    audio = submission.tts_audio.get_audio(audio_id)
    audio["file_hash"] = "sha256:" + "9" * 64
    st.put("audios", audio_id, audio, {"work_id": work["id"]})
    out = view(client, work)
    assert out["confirmation_valid"] is False
    assert out["changed_fields"] == ["配音文件"]


def test_selecting_omnihuman_keeps_work_list_readable_after_refresh(client):
    work = client.post("/api/v1/works", json={"topic": "OmniHuman 选择后刷新"}).json()
    selected = client.post(f"/api/v1/works/{work['id']}/route",
                           json={"route": "OMNIHUMAN_1_5", "cost_notice_ack": True})
    assert selected.status_code == 200, selected.text
    saved = st.get("works", work["id"])["cost_notice_ack"]
    response = client.get("/api/v1/works")
    assert response.status_code == 200, response.text
    actual = next(row for row in response.json() if row["id"] == work["id"])
    assert actual["route"] == "OMNIHUMAN_1_5"
    assert actual["cost_notice_ack"] == saved
    assert st.get("works", work["id"])["cost_notice_ack"] == saved
