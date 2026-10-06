"""Redfox 适配器：离线契约测试。

FIXTURE 依据官方公开文档与 SDK 源码构造（脱敏示例），**未用本账号实测**，契约状态 unverified。
"""
import json

import pytest

from app.content.providers import redfox
from app.content.tasks import NotSent, ProviderRefused

FETCHED = "2026-09-29T00:00:00Z"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_API_KEY", "rf-test-key")


def raw_item(vid="7599000000000001", likes=1000, **kw):
    d = {"videoId": vid, "content": "示例正文 #话题", "opusUrl": f"https://www.iesdouyin.com/share/video/{vid}",
         "coverUrl": "https://example.com/c.jpeg", "duration": 22334, "publishTime": "2026-03-07 10:54:57",
         "likeCount": likes, "commentCount": 100, "shareCount": 50, "collectCount": 80,
         "authorName": "示例作者", "tagList": [{"tagId": "1", "tagName": "话题"}], "videoType": "0"}
    d.update(kw)
    return {k: v for k, v in d.items() if v is not None}


class Stub:
    def __init__(self, body, status=200):
        self.body, self.status, self.calls = body, status, []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body)))
        return self.status, json.dumps(self.body).encode() if not isinstance(self.body, bytes) else self.body


def ok(data):
    return {"code": 2000, "msg": "成功", "data": data}


def test_search_request_shape_and_normalization():
    stub = Stub(ok({"total": 2, "pageNum": 1, "pageSize": 50, "list": [raw_item("1", 5), raw_item("2", 900)]}))
    res = redfox.search_works(keyword="AI", start_date="2026-09-22", end_date="2026-09-29", page=1,
                              fetched_at=FETCHED, sort="likes", transport=stub)
    url, headers, body = stub.calls[0]
    assert url == "https://redfox.hk/story/api/dy/data/searchWork" and headers["X-API-KEY"] == "rf-test-key"
    assert body["keyword"] == "AI" and body["pageNum"] == 1 and body["pageSize"] == 50 and "sortType" not in body
    assert [i["platform_work_id"] for i in res["items"]] == ["2", "1"]
    assert res["sorted_scope"] == "page" and res["contract"] == "unverified"
    assert res["next_cursor"] is None
    assert res["items"][0]["caption"] == "示例正文 #话题" and res["items"][0]["title"] == ""


def test_missing_metrics_stay_none_not_zero():
    item = raw_item("3", likes=None, commentCount=None, shareCount=None, collectCount=None)
    res = redfox.search_works(keyword="a", start_date="s", end_date="e", page=1, fetched_at=FETCHED,
                              sort="likes", transport=Stub(ok({"total": 1, "list": [item]})))
    assert res["items"][0]["metrics"] == {"likes": None, "comments": None, "shares": None, "collects": None}


def test_missing_likes_sorted_last():
    items = [raw_item("a", likes=None), raw_item("b", likes=3)]
    res = redfox.search_works(keyword="a", start_date="s", end_date="e", page=1, fetched_at=FETCHED, sort="likes",
                              transport=Stub(ok({"total": 2, "list": items})))
    assert [i["platform_work_id"] for i in res["items"]] == ["b", "a"]


def test_duplicate_across_pages_dropped_and_cursor_is_page_number():
    stub = Stub(ok({"total": 120, "pageNum": 2, "pageSize": 50, "list": [raw_item("1"), raw_item("9")]}))
    res = redfox.search_works(keyword="a", start_date="s", end_date="e", page=2, fetched_at=FETCHED,
                              exclude_ids=["1"], transport=stub)
    assert [i["platform_work_id"] for i in res["items"]] == ["9"] and res["duplicates_dropped"] == 1
    assert res["next_cursor"] == "page:3"
    assert redfox.decode_cursor("page:3") == 3 and redfox.decode_cursor(None) == 1


def test_empty_result_and_item_without_id():
    res = redfox.search_works(keyword="a", start_date="s", end_date="e", page=1, fetched_at=FETCHED,
                              transport=Stub(ok({"total": 0, "list": []})))
    assert res["items"] == [] and res["next_cursor"] is None
    res = redfox.search_works(keyword="a", start_date="s", end_date="e", page=1, fetched_at=FETCHED,
                              transport=Stub(ok({"total": 1, "list": [{"content": "无ID"}]})))
    assert res["items"] == []


def test_image_set_is_not_transcribable():
    it = redfox.normalize_item(raw_item("5", videoType="68"), FETCHED)
    assert it["media_kind"] == "image_set" and it["transcribable"] is False


def test_video_url_is_kept_only_from_explicit_fields_and_never_guessed():
    for field in ("videoUrl", "playUrl", "downloadUrl", "video_url"):
        it = redfox.normalize_item(raw_item("6", **{field: "https://cdn.example.com/v.mp4"}), FETCHED)
        assert it["video_url"] == "https://cdn.example.com/v.mp4"
    plain = redfox.normalize_item(raw_item("7"), FETCHED)
    assert plain["video_url"] is None and "iesdouyin.com" in plain["source_url"]  # 页面链接不是视频文件
    bad = redfox.normalize_item(raw_item("8", videoUrl="javascript:alert(1)"), FETCHED)
    assert bad["video_url"] is None
    image_set = redfox.normalize_item(raw_item("9", videoType="68", videoUrl="https://cdn.example.com/v.mp4"), FETCHED)
    assert image_set["video_url"] is None and image_set["transcribable"] is False


def test_bad_cursor_rejected():
    with pytest.raises(Exception):
        redfox.decode_cursor("garbage")


@pytest.mark.parametrize("status,body,code,charged", [
    (401, {}, "AUTH_FAILED", False), (429, {}, "RATE_LIMITED", False), (503, {}, "PROVIDER_ERROR", None),
    (200, {"code": 3201, "msg": "积分不足"}, "QUOTA_EXHAUSTED", False),
    (200, {"code": 4004, "msg": "频繁"}, "RATE_LIMITED", False),
    (200, {"code": 9999, "msg": "?"}, "PROVIDER_ERROR", None),
])
def test_errors_mapped(status, body, code, charged):
    with pytest.raises(ProviderRefused) as e:
        redfox.post("/x", {}, transport=Stub(body, status))
    assert e.value.code == code and e.value.charged is charged
    assert "rf-test-key" not in e.value.message


def test_malformed_response_is_value_error_not_refusal():
    with pytest.raises(ValueError):
        redfox.post("/x", {}, transport=Stub(b"<html>"))


def test_missing_key_is_not_sent(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_REDFOX_API_KEY")
    stub = Stub(ok({}))
    with pytest.raises(NotSent):
        redfox.post("/x", {}, transport=stub)
    assert not stub.calls


def test_transcript_submit_and_query_states():
    assert redfox.submit_transcript("https://www.douyin.com/video/1", transport=Stub(ok({"taskId": "T1"}))) == "T1"
    with pytest.raises(ValueError):
        redfox.submit_transcript("u", transport=Stub(ok({})))
    done = redfox.query_transcript("T1", transport=Stub(ok({"status": "succeeded", "text": "口播", "stampSents": [{"textSeg": "口播", "start": 0, "end": 1}]})))
    assert done["state"] == "succeeded" and done["text"] == "口播"
    assert redfox.query_transcript("T1", transport=Stub(ok({"status": "processing"})))["state"] == "processing"
    assert redfox.query_transcript("T1", transport=Stub(ok({"status": "pending?"})))["state"] == "processing"
    failed = redfox.query_transcript("T1", transport=Stub(ok({"status": "failed", "failReason": "无音轨"})))
    assert failed["state"] == "failed" and failed["fail_reason"] == "无音轨"


def test_video_id_from_url():
    assert redfox.video_id_from_url("https://www.douyin.com/video/7599000000000001?x=1") == "7599000000000001"
    assert redfox.video_id_from_url("https://v.douyin.com/abc/") is None
