"""参考流程：搜索缓存、导入/转写恢复、快照与修正、绑定规则。Redfox 用 transport stub（fixture 未实测）。"""
import json
from uuid import uuid4

import pytest

from app import storage as st
from app.content import references, tasks
from app.content import repository as repo
from app.content.errors import ContentRejected


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_API_KEY", "rf-test-key")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", "100000")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_DAILY_FEN", "100000000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "100000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", "100000000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "1000000")
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_POLL_INTERVAL", "0")


def make_work():
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "brief": {"topic": "AI"}, "version": 1, "content_revision": 0})
    return wid


def item(vid, **kw):
    d = {"videoId": vid, "content": "简介文字", "opusUrl": f"https://www.iesdouyin.com/share/video/{vid}",
         "likeCount": 10, "commentCount": None, "shareCount": None, "collectCount": None,
         "publishTime": "2026-09-28 10:00:00", "videoType": "0", "authorName": "作者"}
    d.update(kw)
    return d


class Router:
    """按路径返回预置响应，记录每个路径的调用次数。"""

    def __init__(self, **routes):
        self.routes, self.log = routes, []

    def __call__(self, url, headers, body, timeout):
        path = url.replace("https://redfox.hk", "")
        self.log.append((path, json.loads(body)))
        resp = self.routes[path]
        resp = resp.pop(0) if isinstance(resp, list) else resp
        if isinstance(resp, Exception):
            raise resp
        return 200, json.dumps({"code": 2000, "msg": "成功", "data": resp}).encode()

    def count(self, path):
        return sum(1 for p, _ in self.log if p == path)


def run(handler, job, transport):
    tasks.set_progress(job["id"], state="running")
    try:
        handler(job, transport=transport)
    finally:
        pass
    return tasks.get_job(job["id"])


def test_search_result_cached_within_15_minutes_and_refresh_bypasses():
    wid = make_work()
    r = Router(**{redfox_path("SEARCH"): {"total": 1, "list": [item("1")]}})
    a = tasks.submit_job(wid, "search", "s1", {"query": "AI", "period_days": 7, "sort": "likes"})
    done = run(references.handle_search, a, r)
    assert done["state"] == "succeeded" and done["result_refs"]["items"][0]["platform_work_id"] == "1"
    b = tasks.submit_job(wid, "search", "s2", {"query": "AI", "period_days": 7, "sort": "likes"})
    done_b = run(references.handle_search, b, r)
    assert done_b["result_refs"]["cached_from"] == a["id"] and r.count(redfox_path("SEARCH")) == 1
    c = tasks.submit_job(wid, "search", "s3", {"query": "AI", "period_days": 7, "sort": "likes", "refresh": True})
    run(references.handle_search, c, r)
    assert r.count(redfox_path("SEARCH")) == 2


def redfox_path(name):
    from app.content.providers import redfox
    return {"SEARCH": redfox.PATH_SEARCH, "DETAIL": redfox.PATH_DETAIL, "SUBMIT": redfox.PATH_TRANSCRIPT_SUBMIT,
            "RESULT": redfox.PATH_TRANSCRIPT_RESULT}[name]


def routes(transcript_results):
    return Router(**{redfox_path("DETAIL"): item("77"), redfox_path("SUBMIT"): {"taskId": "T-77"},
                     redfox_path("RESULT"): transcript_results})


def test_import_creates_ready_snapshot_with_separate_caption_and_transcript():
    wid = make_work()
    r = routes([{"status": "processing"}, {"status": "succeeded", "text": "这是口播转写全文"}])
    job = tasks.submit_job(wid, "import", "i1", {"platform_work_id": "77"})
    done = run(references.handle_import, job, r)
    assert done["state"] == "succeeded" and done["result_refs"]["transcript_state"] == "ready"
    ref = repo.get_reference(done["result_refs"]["reference_id"])
    assert ref["caption"] == "简介文字" and ref["transcript"]["text"] == "这是口播转写全文"
    assert ref["metrics"]["comments"] is None and ref["evidence_kind"] == "inspiration"
    assert r.count(redfox_path("SUBMIT")) == 1 and r.count(redfox_path("RESULT")) == 2
    assert st.get("works", wid).get("reference_snapshot_id") is None  # 导入不擅自换参考


def test_reimport_reuses_valid_snapshot_without_paying_again():
    wid = make_work()
    r = routes([{"status": "succeeded", "text": "口播"}])
    first = run(references.handle_import, tasks.submit_job(wid, "import", "i1", {"platform_work_id": "77"}), r)
    again = run(references.handle_import, tasks.submit_job(wid, "import", "i2", {"platform_work_id": "77"}), r)
    assert again["result_refs"]["reused"] is True
    assert again["result_refs"]["reference_id"] == first["result_refs"]["reference_id"]
    assert r.count(redfox_path("SUBMIT")) == 1 and r.count(redfox_path("DETAIL")) == 1


def test_processing_beyond_poll_limit_stays_waiting_and_resume_uses_same_task(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_POLL_MAX", "2")
    wid = make_work()
    r = routes([{"status": "processing"}] * 2 + [{"status": "succeeded", "text": "终于好了"}])
    job = tasks.submit_job(wid, "import", "i1", {"platform_work_id": "77"})
    tasks.set_progress(job["id"], state="running")
    with pytest.raises(tasks.CallPending):
        references.handle_import(job, transport=r)
    tasks.run_job  # noqa: B018
    # 再次执行（如刷新/重启后续跑）：只查询同一 taskId，不重新提交
    done = run(references.handle_import, job, r)
    assert done["state"] == "succeeded"
    assert r.count(redfox_path("SUBMIT")) == 1
    assert {b["taskId"] for p, b in r.log if p == redfox_path("RESULT")} == {"T-77"}


def test_import_rerun_after_refresh_already_finished_the_remote_call_uses_its_result(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_POLL_MAX", "1")
    from app.content.providers import redfox
    wid = make_work()
    r = routes([{"status": "processing"}, {"status": "succeeded", "text": "刷新时才好"}])
    job = tasks.submit_job(wid, "import", "i1", {"platform_work_id": "77"})
    tasks.set_progress(job["id"], state="running")
    with pytest.raises(tasks.CallPending):
        references.handle_import(job, transport=r)
    call = references._find_call(job["id"], "transcript_submit")
    assert call["state"] == "waiting_external"

    def query(tid):
        res = redfox.query_transcript(tid, transport=r)
        return tasks.CallOutcome(response=res, pending=res["state"] == "processing", cost_fen=0)

    tasks.resume_remote(call["id"], query)
    assert references._find_call(job["id"], "transcript_submit")["state"] == "succeeded"
    done = run(references.handle_import, job, r)
    assert done["state"] == "succeeded", done.get("error")
    assert repo.get_reference(done["result_refs"]["reference_id"])["transcript"]["text"] == "刷新时才好"
    assert r.count(redfox_path("SUBMIT")) == 1 and r.count(redfox_path("DETAIL")) == 1


def test_failed_or_empty_transcript_is_not_ready_and_cannot_be_selected():
    wid = make_work()
    r = routes([{"status": "failed", "failReason": "无音轨"}])
    done = run(references.handle_import, tasks.submit_job(wid, "import", "i1", {"platform_work_id": "77"}), r)
    ref = repo.get_reference(done["result_refs"]["reference_id"])
    assert ref["transcript"]["state"] == "failed" and ref["transcript"]["text"] == ""
    with pytest.raises(ContentRejected) as e:
        references.select_reference(wid, ref["id"], 0)
    assert e.value.code == "REFERENCE_TRANSCRIPT_REQUIRED"


def test_empty_transcript_keeps_reference_with_video_url_and_honest_error():
    wid = make_work()
    r = Router(**{redfox_path("DETAIL"): item("66", videoUrl="https://cdn.example.com/dance.mp4"),
                  redfox_path("SUBMIT"): {"taskId": "T-66"},
                  redfox_path("RESULT"): {"status": "succeeded", "text": "  "}})
    done = run(references.handle_import, tasks.submit_job(wid, "import", "i1", {"platform_work_id": "66"}), r)
    assert done["state"] == "succeeded" and done["result_refs"]["transcript_state"] == "failed"
    assert done["result_refs"]["visual_state"] == "none"
    ref = repo.get_reference(done["result_refs"]["reference_id"])
    assert ref["video_url"] == "https://cdn.example.com/dance.mp4" and ref["visual"] == {}
    assert ref["transcript"]["text"] == "" and "纯音乐/无口播" in ref["transcript"]["error"]


@pytest.mark.parametrize("text", ["静音音频", "  静音音频。  "])
def test_redfox_silent_audio_placeholder_is_not_a_ready_transcript(text):
    wid = make_work()
    r = routes([{"status": "succeeded", "text": text}])
    done = run(references.handle_import, tasks.submit_job(wid, "import", "silent", {"platform_work_id": "77"}), r)
    ref = repo.get_reference(done["result_refs"]["reference_id"])
    assert done["state"] == "succeeded"
    assert done["result_refs"]["transcript_state"] == "failed"
    assert ref["transcript"]["text"] == text.strip()
    assert "有效人声" in ref["transcript"]["error"]
    assert ref["transcript"]["task_id"] == "T-77"
    with pytest.raises(ContentRejected) as exc:
        references.select_reference(wid, ref["id"], 0)
    assert exc.value.code == "REFERENCE_TRANSCRIPT_REQUIRED"


def test_image_set_has_no_transcript_state():
    wid = make_work()
    r = Router(**{redfox_path("DETAIL"): item("88", videoType="68")})
    done = run(references.handle_import, tasks.submit_job(wid, "import", "i1", {"platform_work_id": "88"}), r)
    ref = repo.get_reference(done["result_refs"]["reference_id"])
    assert ref["transcript"]["state"] == "none" and r.count(redfox_path("SUBMIT")) == 0


def test_import_requires_id_or_valid_link():
    wid = make_work()
    job = tasks.submit_job(wid, "import", "i1", {"source_url": "https://v.douyin.com/short/"})
    tasks.set_progress(job["id"], state="running")
    with pytest.raises(ContentRejected):
        references.handle_import(job, transport=Router())


def test_select_reference_bumps_revision_and_clears_prompt():
    wid = make_work()
    r = routes([{"status": "succeeded", "text": "口播"}])
    done = run(references.handle_import, tasks.submit_job(wid, "import", "i1", {"platform_work_id": "77"}), r)
    rid = done["result_refs"]["reference_id"]
    with st.transaction() as c:
        w = st.get("works", wid)
        w["writing_prompt_id"] = "prm_old"
        st.insert_row(c, "works", wid, w)
    work = references.select_reference(wid, rid, 0)
    assert work["reference_snapshot_id"] == rid and work["writing_prompt_id"] is None
    assert work["content_revision"] == 1
    with pytest.raises(repo.StaleContentWrite):
        references.select_reference(wid, rid, 0)


def test_transcript_revision_creates_new_snapshot_and_keeps_original():
    wid = make_work()
    r = routes([{"status": "succeeded", "text": "原始转写"}])
    done = run(references.handle_import, tasks.submit_job(wid, "import", "i1", {"platform_work_id": "77"}), r)
    base_id = done["result_refs"]["reference_id"]
    references.select_reference(wid, base_id, 0)
    job = tasks.submit_job(wid, "reference_revision", "rv", {"reference_snapshot_id": base_id, "transcript_text": "用户修正后的转写"})
    tasks.set_progress(job["id"], state="running")
    references.handle_reference_revision(job)
    fin = tasks.get_job(job["id"])
    assert fin["state"] == "succeeded"
    new = repo.get_reference(fin["result_refs"]["reference_id"])
    assert new["parent_id"] == base_id and new["transcript"]["source"] == "user_edit"
    assert new["original_transcript"]["text"] == "原始转写" and new["content_digest"] != repo.get_reference(base_id)["content_digest"]
    assert repo.get_reference(base_id)["transcript"]["text"] == "原始转写"
    assert st.get("works", wid)["reference_snapshot_id"] == new["id"]


def test_editing_a_preview_reference_does_not_replace_the_selected_reference():
    wid = make_work()
    r = routes([{"status": "succeeded", "text": "主参考口播"}])
    first = run(references.handle_import, tasks.submit_job(wid, "import", "main", {"platform_work_id": "77"}), r)
    main_id = first["result_refs"]["reference_id"]
    references.select_reference(wid, main_id, 0)
    main = repo.get_reference(main_id)
    with st.transaction() as c:
        preview = repo.save_reference(c, dict(main, id="ref_preview_" + wid, platform_work_id="88"))
        repo.bump_content_revision(c, wid, writing_prompt_id="prm_existing")
    before = st.get("works", wid)
    job = tasks.submit_job(wid, "reference_revision", "edit-preview", {
        "reference_snapshot_id": preview["id"], "transcript_text": "预览参考的修正转写"})
    tasks.set_progress(job["id"], state="running")
    references.handle_reference_revision(job)
    done = tasks.get_job(job["id"])
    new = repo.get_reference(done["result_refs"]["reference_id"])
    assert done["state"] == "succeeded" and new["parent_id"] == preview["id"]
    assert new["transcript"]["text"] == "预览参考的修正转写"
    after = st.get("works", wid)
    assert after["reference_snapshot_id"] == main_id
    assert after["writing_prompt_id"] == before["writing_prompt_id"]
    assert after["content_revision"] == before["content_revision"]


def test_search_payload_validation():
    with pytest.raises(ContentRejected):
        references.normalize_search_payload({"query": " "})
    with pytest.raises(ContentRejected):
        references.normalize_search_payload({"query": "a", "sort": "hot"})
    with pytest.raises(ContentRejected):
        references.normalize_search_payload({"query": "a", "period_days": 3})
