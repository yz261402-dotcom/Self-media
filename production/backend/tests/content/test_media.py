"""参考视频媒体资产：校验、探测、内容寻址去重、作品隔离与上传接口。用真实 ffmpeg 生成的小视频。"""
import io
import subprocess
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.content import media
from app.content import repository as repo
from app.content.errors import ContentRejected
from app.video import ffmpeg


@pytest.fixture(autouse=True)
def isolated_media(tmp_path, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    for k in ("MAX_BYTES", "MAX_DURATION_MS"):
        monkeypatch.delenv(f"AVATAR_STUDIO_CONTENT_VISUAL_{k}", raising=False)


@pytest.fixture(scope="session")
def mp4_bytes(tmp_path_factory):
    out = tmp_path_factory.mktemp("clip") / "c.mp4"
    cp = subprocess.run([ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i",
                         "testsrc=size=320x568:rate=10:duration=2", "-pix_fmt", "yuv420p", str(out)], capture_output=True)
    assert cp.returncode == 0, cp.stderr[:200]
    return out.read_bytes()


@pytest.fixture(scope="session")
def truncated_faststart_mp4(tmp_path_factory, mp4_bytes):
    """保留前置 moov 的声明时长，但截掉后半段媒体数据。"""
    folder = tmp_path_factory.mktemp("broken")
    source, complete = folder / "source.mp4", folder / "complete.mp4"
    source.write_bytes(mp4_bytes)
    subprocess.run([ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-i", str(source),
                    "-c", "copy", "-movflags", "+faststart", str(complete)], check=True, capture_output=True)
    data = complete.read_bytes()
    broken = folder / "broken.mp4"
    broken.write_bytes(data[:len(data) // 2])
    # 复现前提：仅探测头部仍把截断文件视为完整视频。
    assert media.probe_video(str(broken))["duration_ms"] >= 1500
    return broken.read_bytes()


def work_with_ref():
    from app.content import contracts as cc
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "brief": {"topic": "舞蹈"}, "version": 1, "content_revision": 0})
    ref = {"id": "ref_" + uuid4().hex[:8], "work_id": wid, "platform_work_id": "1", "fetched_at": "t",
           "extraction_version": "redfox-transcript-v1"}
    with st.transaction() as c:
        repo.save_reference(c, ref)
    return wid, ref["id"]


def save(wid, rid, data, name="a.mp4", ctype="video/mp4"):
    return media.save_uploaded_video(wid, rid, io.BytesIO(data), name, ctype)


def test_upload_probes_and_dedupes_by_hash(mp4_bytes):
    wid, rid = work_with_ref()
    a = save(wid, rid, mp4_bytes)
    assert (a.width, a.height, a.mime_type, a.source_kind) == (320, 568, "video/mp4", "user_upload")
    assert 1500 <= a.duration_ms <= 2600 and a.sha256.startswith("sha256:") and ".." not in a.storage_uri
    b = save(wid, rid, mp4_bytes, name="other-name.mp4")
    assert b.id == a.id  # 同作品同哈希复用，不重复落盘
    assert media.get_media_asset(a.id).sha256 == a.sha256
    assert media.resolve_path(a).is_file()


def test_upload_rejects_probeable_truncated_video_without_persisting(truncated_faststart_mp4):
    wid, rid = work_with_ref()
    with pytest.raises(ContentRejected) as error:
        save(wid, rid, truncated_faststart_mp4)
    assert error.value.code == "MEDIA_INCOMPLETE"
    assert st.all_rows(media.TABLE, "work_id=?", [wid]) == []
    assert not list(media.media_root().rglob("*.part"))
    assert not list(media.media_root().rglob("*.mp4"))


def test_upload_rejects_decode_error_output_even_with_zero_exit(mp4_bytes, monkeypatch):
    wid, rid = work_with_ref()
    monkeypatch.setattr(ffmpeg, "run_ffmpeg", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, b"", b"Invalid NAL unit size"))
    with pytest.raises(ContentRejected) as error:
        save(wid, rid, mp4_bytes)
    assert error.value.code == "MEDIA_INCOMPLETE"
    assert st.all_rows(media.TABLE, "work_id=?", [wid]) == []


def test_download_rejects_truncated_body_without_length_header(truncated_faststart_mp4):
    wid, rid = work_with_ref()
    response = io.BytesIO(truncated_faststart_mp4)
    response.headers = {"Content-Type": "video/mp4"}
    with pytest.raises(ContentRejected) as error:
        media.download_video(wid, rid, "https://cdn.example/video.mp4", "https://example/page",
                             opener=lambda url: response)
    assert error.value.code == "MEDIA_INCOMPLETE"
    assert response.closed and st.all_rows(media.TABLE, "work_id=?", [wid]) == []


@pytest.mark.parametrize("failure,code", [("FFMPEG_TIMEOUT", "MEDIA_PROCESS_TIMEOUT"),
                                         ("FFMPEG_SPAWN_FAILED", "MEDIA_TOOL_UNAVAILABLE")])
def test_upload_integrity_tool_failures_are_explicit(mp4_bytes, monkeypatch, failure, code):
    from app.video.errors import VideoBlocked
    wid, rid = work_with_ref()

    def fail(*args, **kwargs):
        raise VideoBlocked(failure, "tool failed")

    monkeypatch.setattr(ffmpeg, "run_ffmpeg", fail)
    with pytest.raises(ContentRejected) as error:
        save(wid, rid, mp4_bytes)
    assert error.value.code == code
    assert st.all_rows(media.TABLE, "work_id=?", [wid]) == []


@pytest.mark.parametrize("data,name,ctype,code", [
    (b"", "a.mp4", "video/mp4", "MEDIA_INVALID"),
    (b"not a video at all" * 50, "a.mp4", "video/mp4", "MEDIA_INVALID"),
    (b"xxxx", "a.exe", "video/mp4", "MEDIA_TYPE_UNSUPPORTED"),
    (b"xxxx", "a.mp4", "application/zip", "MEDIA_TYPE_UNSUPPORTED"),
])
def test_rejects_bad_uploads(data, name, ctype, code):
    wid, rid = work_with_ref()
    with pytest.raises(ContentRejected) as e:
        save(wid, rid, data, name, ctype)
    assert e.value.code == code


def test_size_and_duration_limits(mp4_bytes, monkeypatch):
    wid, rid = work_with_ref()
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_MAX_BYTES", "1000")
    with pytest.raises(ContentRejected) as e:
        save(wid, rid, mp4_bytes)
    assert e.value.code == "MEDIA_TOO_LARGE"
    monkeypatch.delenv("AVATAR_STUDIO_CONTENT_VISUAL_MAX_BYTES")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_MAX_DURATION_MS", "500")
    with pytest.raises(ContentRejected) as e:
        save(wid, rid, mp4_bytes)
    assert e.value.code == "MEDIA_TOO_LONG"


def test_reference_must_belong_to_work(mp4_bytes):
    wid, rid = work_with_ref()
    other, _ = work_with_ref()
    with pytest.raises(ContentRejected) as e:
        save(other, rid, mp4_bytes)
    assert e.value.code == "REFERENCE_NOT_FOUND"


def test_asset_is_scoped_to_its_work(mp4_bytes):
    wid, rid = work_with_ref()
    a = save(wid, rid, mp4_bytes)
    assert media.asset_for_work(wid, a.id).id == a.id
    with pytest.raises(ContentRejected) as e:
        media.asset_for_work("work_other", a.id)
    assert e.value.code == "MEDIA_NOT_FOUND"


def test_resolve_path_refuses_escape(mp4_bytes):
    wid, rid = work_with_ref()
    a = save(wid, rid, mp4_bytes)
    with pytest.raises(ContentRejected):
        media.resolve_path(a.model_copy(update={"storage_uri": "../../etc/passwd"}))


def test_upload_endpoint_success_and_error_codes(mp4_bytes):
    from app.main import app
    wid, rid = work_with_ref()
    client = TestClient(app)
    url = f"/api/v1/works/{wid}/content/media"
    ok = client.post(url, data={"reference_snapshot_id": rid}, files={"file": ("d.mp4", mp4_bytes, "video/mp4")})
    assert ok.status_code == 200, ok.text
    asset = ok.json()["asset"]
    assert asset["width"] == 320 and asset["mime_type"] == "video/mp4" and asset["sha256"].startswith("sha256:")
    assert "storage_uri" not in asset
    def code(resp):
        body = resp.json()
        return (body.get("error") or body["detail"]["error"])["code"]

    bad = client.post(url, data={"reference_snapshot_id": rid}, files={"file": ("d.exe", b"xxxx", "video/mp4")})
    assert bad.status_code == 422 and code(bad) == "MEDIA_TYPE_UNSUPPORTED"
    junk = client.post(url, data={"reference_snapshot_id": rid}, files={"file": ("d.mp4", b"junk" * 100, "video/mp4")})
    assert code(junk) == "MEDIA_INVALID"
    missing = client.post(f"/api/v1/works/work_nope/content/media", data={"reference_snapshot_id": rid},
                          files={"file": ("d.mp4", mp4_bytes, "video/mp4")})
    assert missing.status_code == 404
