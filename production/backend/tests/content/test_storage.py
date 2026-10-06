"""任务 1：不可变存储、版本单调与 CAS。所有用例用 conftest 隔离库。"""
from __future__ import annotations

import psycopg
from uuid import uuid4

import pytest

from app import storage as st
from app.content import repository as repo
from app.content.contracts import text_digest

NEW_TABLES = ("content_references", "content_prompts", "content_jobs",
              "script_versions", "content_style_profiles", "content_batches")


def _work(**extra) -> str:
    wid = "work_" + uuid4().hex[:10]
    st.put("works", wid, {"id": wid, "name": "t", "brief": {"topic": "AI"},
                          "script_version_id": None, "current_task_id": None,
                          "version": 1, **extra})
    return wid


def _ref(wid: str, rid: str = None, **over) -> dict:
    return {"id": rid or "ref_" + uuid4().hex[:8], "work_id": wid, "platform_work_id": "p1",
            "source_url": "https://www.douyin.com/video/p1", "title": "T", "caption": "C",
            "fetched_at": "2026-09-29T00:00:00Z", "extraction_version": "redfox-transcript-v1",
            "transcript": {"state": "ready", "text": "转写"}, **over}


def _version(wid: str, vid: str = None, slot: int = 0, text: str = "正文", **over) -> dict:
    return {"id": vid or "script_" + uuid4().hex[:8], "work_id": wid, "slot": slot,
            "angle": f"角度{slot}", "title": "标题", "description": "说明", "tags": [],
            "text": text, "duration_estimate_ms": 1000,
            "quality_report": {"checks": [], "overall": "pass", "review_required": False},
            "claims": [], **over}


def test_init_db_creates_new_tables_and_is_idempotent():
    st.init_db()
    st.init_db()
    names = {r["name"] for r in st.conn().execute("SELECT table_name AS name FROM information_schema.tables WHERE table_schema=current_schema()")}
    assert set(NEW_TABLES) <= names


def test_content_jobs_unique_key_is_enforced_by_the_schema():
    wid = _work()
    c = st.conn()
    sql = ("INSERT INTO content_jobs (id, work_id, kind, request_id, input_digest, state, data) "
           "VALUES (?,?,?,?,?,?,?)")
    c.execute(sql, ["cj_1", wid, "search", "req_1", "sha256:a", "queued", "{}"])
    with pytest.raises(psycopg.errors.IntegrityError):
        c.execute(sql, ["cj_2", wid, "search", "req_1", "sha256:b", "queued", "{}"])
    c.rollback()


def test_generic_put_cannot_silently_bypass_job_columns():
    with pytest.raises(psycopg.errors.IntegrityError):
        st.put("content_jobs", "cj_bad", {"id": "cj_bad"})  # 缺 kind/request_id 等索引列


def test_reference_is_immutable_but_identical_replay_is_ok():
    wid = _work()
    ref = _ref(wid, "ref_fixed")
    with st.transaction() as c:
        first = repo.save_reference(c, ref)
    with st.transaction() as c:
        again = repo.save_reference(c, ref)
    assert first == again
    with pytest.raises(repo.ImmutableRecordConflict):
        with st.transaction() as c:
            repo.save_reference(c, _ref(wid, "ref_fixed", title="被偷偷改了"))
    assert repo.get_reference("ref_fixed")["title"] == "T"


def test_reference_content_digest_is_stamped_by_the_server():
    wid = _work()
    with st.transaction() as c:
        saved = repo.save_reference(c, _ref(wid, content_digest="sha256:客户端乱填"))
    assert saved["content_digest"].startswith("sha256:")
    assert saved["content_digest"] != "sha256:客户端乱填"


def test_script_version_text_can_never_be_overwritten_in_place():
    wid = _work()
    v = _version(wid, "script_fixed", text="原正文")
    with st.transaction() as c:
        saved = repo.save_script_version(c, v)
    assert saved["text_digest"] == text_digest("原正文")
    with pytest.raises(repo.ImmutableRecordConflict):
        with st.transaction() as c:
            repo.save_script_version(c, _version(wid, "script_fixed", text="改了正文但沿用旧 ID"))
    assert repo.get_script_version("script_fixed")["text"] == "原正文"


def test_revision_keeps_parent_and_gets_a_new_id():
    wid = _work()
    with st.transaction() as c:
        parent = repo.save_script_version(c, _version(wid, slot=1, text="第一版"))
        child = repo.save_script_version(
            c, _version(wid, slot=1, text="第二版", parent_id=parent["id"]))
    assert child["id"] != parent["id"] and child["parent_id"] == parent["id"]
    assert repo.get_script_version(parent["id"])["text"] == "第一版"


def test_historical_batches_stay_readable_after_a_newer_batch():
    wid = _work()
    with st.transaction() as c:
        v0 = repo.save_script_version(c, _version(wid, slot=0, text="A"))
        b1 = repo.save_batch(c, {"id": "batch_1", "work_id": wid, "prompt_id": "prm_1",
                                 "content_revision": 1, "job_id": "cj_1",
                                 "members": [{"slot": 0, "candidate_id": v0["id"]}]})
        v0b = repo.save_script_version(c, _version(wid, slot=0, text="B", parent_id=v0["id"]))
        b2 = repo.save_batch(c, {"id": "batch_2", "work_id": wid, "prompt_id": "prm_1",
                                 "content_revision": 1, "job_id": "cj_2",
                                 "members": [{"slot": 0, "candidate_id": v0b["id"]}]})
    ids = [b["id"] for b in repo.list_batches(wid)]
    assert ids == ["batch_1", "batch_2"]
    assert b1["set_digest"] != b2["set_digest"]
    assert repo.get_batch("batch_1")["members"][0]["text_digest"] == text_digest("A")


def test_batch_members_must_be_saved_versions_of_the_same_work():
    wid, other = _work(), _work()
    with st.transaction() as c:
        foreign = repo.save_script_version(c, _version(other))
    with pytest.raises(repo.RepositoryError):
        with st.transaction() as c:
            repo.save_batch(c, {"id": "batch_x", "work_id": wid, "prompt_id": "p",
                                "content_revision": 0, "job_id": "cj",
                                "members": [{"slot": 0, "candidate_id": foreign["id"]}]})
    with pytest.raises(repo.RepositoryError):
        with st.transaction() as c:
            repo.save_batch(c, {"id": "batch_y", "work_id": wid, "prompt_id": "p",
                                "content_revision": 0, "job_id": "cj",
                                "members": [{"slot": 0, "candidate_id": "script_missing"}]})


def test_content_revision_is_monotonic_across_a_b_a_edits():
    wid = _work()
    seen = []
    for prompt in ("prm_A", "prm_B", "prm_A"):
        with st.transaction() as c:
            w = repo.bump_content_revision(c, wid, writing_prompt_id=prompt)
        seen.append(w["content_revision"])
    assert seen == [1, 2, 3]
    assert st.get("works", wid)["writing_prompt_id"] == "prm_A"


def test_bump_rejects_fields_outside_the_shared_input_allowlist():
    wid = _work()
    with pytest.raises(repo.RepositoryError):
        with st.transaction() as c:
            repo.bump_content_revision(c, wid, script_version_id="script_hijack")


def test_bump_with_stale_expected_revision_is_refused():
    wid = _work()
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, writing_prompt_id="prm_1")
    with pytest.raises(repo.StaleContentWrite) as err:
        with st.transaction() as c:
            repo.bump_content_revision(c, wid, expected_revision=0, writing_prompt_id="prm_2")
    assert err.value.code == "CONTENT_REVISION_STALE"
    assert st.get("works", wid)["writing_prompt_id"] == "prm_1"


def _batch_for(wid: str, batch_id: str = "batch_bind") -> dict:
    with st.transaction() as c:
        v = repo.save_script_version(c, _version(wid, text="候选正文"))
        return repo.save_batch(c, {"id": batch_id + uuid4().hex[:6], "work_id": wid,
                                   "prompt_id": "prm_1", "content_revision": 0,
                                   "job_id": "cj_gen", "source_kind": "llm",
                                   "members": [{"slot": 0, "candidate_id": v["id"]}]})


def test_bind_current_batch_projects_into_legacy_script_batches():
    wid = _work(current_content_job_id="cj_gen", content_revision=0)
    batch = _batch_for(wid)
    with st.transaction() as c:
        view = repo.bind_current_batch(c, wid, batch["id"], expected_revision=0,
                                       expected_job_id="cj_gen")
    stored = st.get("script_batches", wid)
    assert stored["id"] == batch["id"] == view["id"]
    assert stored["candidates"][0]["text"] == "候选正文"
    assert stored["candidates"][0]["slot"] == 0
    from app.contracts import ScriptBatch
    ScriptBatch(**stored)  # 投影必须满足严格契约


def test_bind_current_batch_refuses_stale_revision_or_wrong_job():
    wid = _work(current_content_job_id="cj_gen", content_revision=2)
    batch = _batch_for(wid)
    for rev, job in ((1, "cj_gen"), (2, "cj_old")):
        with pytest.raises(repo.StaleContentWrite):
            with st.transaction() as c:
                repo.bind_current_batch(c, wid, batch["id"], expected_revision=rev,
                                        expected_job_id=job)
    assert st.get("script_batches", wid) is None


def test_old_response_cannot_resurrect_after_a_b_a():
    """A→B→A：摘要看起来回到 A，但 revision 已前进，旧任务的回写必须被拒。"""
    wid = _work(current_content_job_id="cj_1", content_revision=0)
    stale_batch = _batch_for(wid)
    for prompt in ("prm_B", "prm_A"):
        with st.transaction() as c:
            repo.bump_content_revision(c, wid, writing_prompt_id=prompt)
    with pytest.raises(repo.StaleContentWrite):
        with st.transaction() as c:
            repo.bind_current_batch(c, wid, stale_batch["id"], expected_revision=0,
                                    expected_job_id="cj_1")


def test_style_profile_revisions_increment_and_snapshots_do_not_follow():
    with st.transaction() as c:
        p1 = repo.save_style_profile(c, {"id": "style_t1", "name": "干练", "persona": "老编辑"})
    assert p1["revision"] == 1
    snapshot = repo.style_snapshot(p1)
    with st.transaction() as c:
        p2 = repo.save_style_profile(c, {"id": "style_t1", "name": "干练", "persona": "改过"},
                                     expected_revision=1)
    assert p2["revision"] == 2
    assert snapshot["persona"] == "老编辑" and snapshot["revision"] == 1
    with pytest.raises(repo.StaleContentWrite):
        with st.transaction() as c:
            repo.save_style_profile(c, {"id": "style_t1", "name": "干练", "persona": "又改"},
                                    expected_revision=1)
