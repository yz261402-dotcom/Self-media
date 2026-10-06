"""自动获取参考视频：RedFox 下载器 → 受控落盘。费用确认门禁、直链 SSRF 防护、未知不重发、复用上传校验。"""
import io
import json
import subprocess
from uuid import uuid4

import pytest

from app import storage as st
from app.content import media, media_fetch, tasks
from app.content import repository as repo
from app.content.errors import ContentRejected
from app.content.providers import redfox
from app.video import ffmpeg
from tests.content._helpers import run_job, set_env

DIRECT = "https://n98-v-ncdnon.douyinvod.com/video.mp4?sign=abc"
PAGE = "https://www.iesdouyin.com/share/video/7654143095876898089/"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_API_KEY", "ak_test")
    monkeypatch.setattr(media, "check_public_url", lambda u: u)


@pytest.fixture(scope="module")
def mp4(tmp_path_factory):
    out = tmp_path_factory.mktemp("mf") / "c.mp4"
    subprocess.run([ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x568:rate=10:duration=3", "-pix_fmt", "yuv420p", str(out)], check=True,
                   capture_output=True)
    return out.read_bytes()


class Resp(io.BytesIO):
    def __init__(self, data, ctype="video/mp4"):
        super().__init__(data)
        self.headers = {"Content-Type": ctype}


class Stub:
    def __init__(self, data=None, status=200, exc=None):
        self.data, self.status, self.exc, self.calls = data, status, exc, []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, json.loads(body)))
        if self.exc:
            raise self.exc
        return self.status, json.dumps({"code": 2000, "msg": "成功", "data": self.data}).encode()


def ok_payload():
    return {"videoUrl": DIRECT, "cover": "https://x/c.jpg", "resources": [{"type": "video", "downloadUrl": DIRECT}]}


def make_ref():
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "version": 1, "content_revision": 0,
                          "brief": {"topic": "舞蹈", "content_route": "short_video_storyboard"}})
    ref = {"id": "ref_" + uuid4().hex[:8], "work_id": wid, "platform_work_id": "7654143095876898089",
           "source_url": PAGE, "fetched_at": "t", "extraction_version": "redfox-transcript-v1",
           "transcript": {"state": "failed", "error": "无口播"}}
    with st.transaction() as c:
        repo.save_reference(c, ref)
    return wid, ref["id"]


def submit(wid, rid, fen=100):
    conf = media_fetch.create_confirmation(wid, rid, ceiling_fen=fen)
    return media_fetch.submit_media_fetch(wid, rid, confirmation_id=conf["id"])


def run(job, stub, opener):
    tasks.set_progress(job["id"], state="running")
    try:
        media_fetch.handle_media_fetch(job, transport=stub, opener=opener)
    except Exception as exc:  # noqa: BLE001
        tasks.run_job_error(job, exc)
    return tasks.get_job(job["id"])


# ---------------------------------------------------------------- RedFox 适配

def test_download_video_parses_direct_url_and_rejects_missing_or_non_http():
    stub = Stub(ok_payload())
    assert redfox.download_video(PAGE, transport=stub) == {"video_url": DIRECT}
    assert stub.calls[0][0].endswith("/story/api/parseWork/videoDownload/douyin") and stub.calls[0][1]["url"] == PAGE
    for bad in ({"videoUrl": "", "resources": []}, {"videoUrl": "javascript:alert(1)"}, {"resources": [{"type": "image"}]}):
        with pytest.raises(redfox.ProviderRefused) as e:
            redfox.download_video(PAGE, transport=Stub(bad))
        assert e.value.code == "VIDEO_URL_UNAVAILABLE"


# ---------------------------------------------------------------- 下载与 SSRF

@pytest.mark.parametrize("url", ["http://127.0.0.1/a.mp4", "http://169.254.169.254/latest", "http://10.0.0.5/a.mp4",
                                 "ftp://x/a.mp4", "https://user:pw@example.com/a.mp4", "https://example.com:99999/a.mp4",
                                 "http://localhost:8889/a.mp4", "http://[::1]/a.mp4"])
def test_direct_url_must_be_public_http(url, monkeypatch):
    monkeypatch.undo()  # 恢复真实的 check_public_url
    with pytest.raises(ContentRejected) as e:
        media.check_public_url(url)
    assert e.value.code in ("MEDIA_URL_REJECTED", "MEDIA_DOWNLOAD_FAILED")


def test_download_reuses_upload_validation(mp4):
    wid, rid = make_ref()
    asset = media.download_video(wid, rid, DIRECT, PAGE, opener=lambda u: Resp(mp4))
    assert asset.source_kind == "provider_url" and asset.source_url == PAGE and "sign=" not in asset.source_url
    with pytest.raises(ContentRejected) as e:
        media.download_video(wid, rid, DIRECT, PAGE, opener=lambda u: Resp(b"<html>", "text/html"))
    assert e.value.code == "MEDIA_TYPE_UNSUPPORTED"
    with pytest.raises(ContentRejected) as e:
        media.download_video(wid, rid, DIRECT, PAGE, opener=lambda u: Resp(b"not a video"))
    assert e.value.code == "MEDIA_INVALID"


def test_download_too_large_is_rejected(mp4, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_MAX_BYTES", "1000")
    wid, rid = make_ref()
    with pytest.raises(ContentRejected) as e:
        media.download_video(wid, rid, DIRECT, PAGE, opener=lambda u: Resp(mp4))
    assert e.value.code == "MEDIA_TOO_LARGE"


@pytest.mark.parametrize("header", ["short-length", "long-length", "partial-range", "partial-status",
                                   "malformed-length", "inconsistent-range"])
def test_download_rejects_incomplete_http_body_before_persisting(mp4, header):
    wid, rid = make_ref()
    response = Resp(mp4)
    if header == "short-length":
        response.headers["Content-Length"] = str(len(mp4) - 10)
    elif header == "long-length":
        response.headers["Content-Length"] = str(len(mp4) + 10)
    elif header == "partial-range":
        response.headers["Content-Range"] = f"bytes 0-{len(mp4) - 1}/{len(mp4) * 2}"
        response.status = 206
    elif header == "partial-status":
        response.status = 206
    elif header == "malformed-length":
        response.headers["Content-Length"] = "-1"
    else:
        response.headers["Content-Length"] = str(len(mp4))
        response.headers["Content-Range"] = f"bytes 0-{len(mp4)}/{len(mp4) + 1}"
    with pytest.raises(ContentRejected) as error:
        media.download_video(wid, rid, DIRECT, PAGE, opener=lambda url: response)
    assert error.value.code == "MEDIA_DOWNLOAD_INCOMPLETE"
    assert st.all_rows(media.TABLE, "work_id=?", [wid]) == []
    assert response.closed


def test_download_accepts_complete_range_response(mp4):
    wid, rid = make_ref()
    response = Resp(mp4)
    response.status = 206
    response.headers.update({"Content-Length": str(len(mp4)),
                             "Content-Range": f"bytes 0-{len(mp4) - 1}/{len(mp4)}"})
    asset = media.download_video(wid, rid, DIRECT, PAGE, opener=lambda url: response)
    assert asset.byte_size == len(mp4)
    assert response.closed


# ---------------------------------------------------------------- 任务链路

def test_no_user_confirmation_never_sends():
    wid, rid = make_ref()
    with pytest.raises(ContentRejected) as e:
        media_fetch.submit_media_fetch(wid, rid, confirmation_id="")
    assert e.value.code == "MEDIA_FETCH_CONFIRMATION_REQUIRED"
    for fen in (-1, 0, 49):
        with pytest.raises(ContentRejected) as e:
            media_fetch.create_confirmation(wid, rid, ceiling_fen=fen)
        assert e.value.code == "COST_AMOUNT_INVALID"
    with pytest.raises(ContentRejected) as e:
        media_fetch.create_confirmation(wid, rid, ceiling_fen=100, actor_id="agent")
    assert e.value.code == "COST_ACTOR_INVALID"


@pytest.mark.parametrize("per_flow", ["0", "300"])
def test_quote_has_no_system_confirmation_ceiling(monkeypatch, per_flow):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", per_flow)
    wid, rid = make_ref()
    q = media_fetch.quote(wid, rid)
    assert q["max_ceiling_fen"] is None
    assert q["reserve_fen"] == 50


@pytest.mark.parametrize("per_flow", ["0", "300"])
@pytest.mark.parametrize("ceiling_fen", [500, 10000])
def test_user_confirmation_can_exceed_legacy_flow_budget(monkeypatch, per_flow, ceiling_fen):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", per_flow)
    wid, rid = make_ref()
    conf = media_fetch.create_confirmation(wid, rid, ceiling_fen=ceiling_fen)
    job = media_fetch.submit_media_fetch(wid, rid, confirmation_id=conf["id"])
    assert conf["ceiling_fen"] == ceiling_fen
    assert job["payload"]["total_ceiling_fen"] == ceiling_fen
    assert conf["request_digest"] == media_fetch.quote(wid, rid)["request_digest"]


def test_increased_reserve_requires_new_user_confirmation_before_send(monkeypatch):
    wid, rid = make_ref()
    job = submit(wid, rid, fen=50)
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_RESERVE_FEN_VIDEO_DOWNLOAD", "51")
    stub = Stub(ok_payload())
    with pytest.raises(ContentRejected) as e:
        media_fetch.handle_media_fetch(job, transport=stub)
    assert e.value.code == "COST_AMOUNT_INVALID"
    assert stub.calls == [] and tasks.jobs_calls(job["id"]) == []


def test_success_creates_asset_and_one_ledger_call_and_replay_does_not_resend(mp4):
    wid, rid = make_ref()
    stub = Stub(ok_payload())
    job = submit(wid, rid)
    done = run(job, stub, lambda u: Resp(mp4))
    assert done["state"] == "succeeded", done.get("error")
    assert media.asset_for_work(wid, done["result_refs"]["media_asset_id"]).source_kind == "provider_url"
    calls = tasks.jobs_calls(job["id"])
    assert len(calls) == 1 and calls[0]["provider"] == "redfox" and len(stub.calls) == 1
    assert DIRECT not in json.dumps(done)  # 带签名的直链不进任务结果
    # 同一个 request_id 的重放取回原任务，不再发请求
    assert submit(wid, rid)["id"] == job["id"] and len(stub.calls) == 1


def test_unknown_response_is_not_resent(mp4):
    wid, rid = make_ref()
    stub = Stub(exc=TimeoutError("net"))
    job = submit(wid, rid)
    done = run(job, stub, lambda u: Resp(mp4))
    assert done["state"] == "unknown" and len(stub.calls) == 1
    done = run(tasks.get_job(job["id"]), stub, lambda u: Resp(mp4))  # 再次运行只会撞上未知调用，不会重发
    assert len(stub.calls) == 1 and len(tasks.jobs_calls(job["id"])) == 1


def test_refused_download_link_is_failed_not_unknown(mp4):
    wid, rid = make_ref()
    job = submit(wid, rid)
    done = run(job, Stub({"videoUrl": "", "resources": []}), lambda u: Resp(mp4))
    assert done["state"] == "failed" and done["error"]["code"] == "VIDEO_URL_UNAVAILABLE"


def test_expired_link_keeps_ledger_call_and_fails_without_second_request(mp4):
    wid, rid = make_ref()
    stub = Stub(ok_payload())

    def expired(url):
        raise ContentRejected("直链已过期", "MEDIA_DOWNLOAD_FAILED")

    job = submit(wid, rid)
    done = run(job, stub, expired)
    assert done["state"] == "failed" and done["error"]["code"] == "MEDIA_DOWNLOAD_FAILED"
    assert len(stub.calls) == 1  # 失败后重新执行同一任务，同一幂等键直接取回直链，不再请求 RedFox
    run_again = run(tasks.get_job(job["id"]), stub, lambda u: Resp(mp4))
    assert len(stub.calls) == 1 and len(tasks.jobs_calls(job["id"])) == 1


def test_reference_without_page_url_asks_for_manual_upload():
    wid, rid = make_ref()
    ref = repo.get_reference(rid)
    wid2 = "work_" + uuid4().hex
    st.put("works", wid2, {"id": wid2, "name": "t", "version": 1, "content_revision": 0, "brief": {"topic": "t"}})
    bad = dict(ref, id="ref_nolink", work_id=wid2, source_url="https://example.com/x", content_digest="")
    with st.transaction() as c:
        repo.save_reference(c, bad)
    with pytest.raises(ContentRejected) as e:
        media_fetch.quote(wid2, "ref_nolink")
    assert e.value.code == "REFERENCE_MEDIA_UNAVAILABLE"


# ---------------------------------------------------------------- API

def test_api_requires_confirmation_and_state_exposes_fetched_media(mp4, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    monkeypatch.setattr(tasks.get_scheduler(), "start", lambda: None)
    wid, rid = make_ref()
    url = f"/api/v1/works/{wid}/content/media-fetches"
    body = {"request_id": "r1", "reference_snapshot_id": rid, "expected_content_revision": 0}
    r = client.post(url, json=body)  # 没勾选 paid_ack
    assert r.status_code == 422 and (r.json().get("error") or r.json()["detail"]["error"])["code"] == "MEDIA_FETCH_CONFIRMATION_REQUIRED"
    r = client.post(url, json=dict(body, paid_ack=True, total_ceiling_fen=10))
    assert r.status_code == 422 and (r.json().get("error") or r.json()["detail"]["error"])["code"] == "COST_AMOUNT_INVALID"
    q = client.post(f"/api/v1/works/{wid}/content/media-fetch-quotes", json={"reference_snapshot_id": rid}).json()
    assert q["reserve_fen"] == 50 and q["route"] == "MEDIA_FETCH"
    r = client.post(url, json=dict(body, paid_ack=True, total_ceiling_fen=100))
    assert r.status_code == 202 and r.json()["kind"] == "media_fetch"
    job = tasks.get_job(r.json()["job_id"])
    done = run(job, Stub(ok_payload()), lambda u: Resp(mp4))
    assert done["state"] == "succeeded"
    ref = [x for x in client.get(f"/api/v1/works/{wid}/content/state").json()["references"] if x["id"] == rid][0]
    assert ref["media_asset_id"] == done["result_refs"]["media_asset_id"]
