"""本地恢复已有完整画面字段：不提交供应商、不修改原任务或费用记录。"""
import json

import pytest

from app import storage as st
from app.content import tasks, visual, repository as repo
from app.content.errors import ContentRejected
from app.content.providers import qwen_vision as qv
from tests.content.test_visual_analysis import confirm, env, mp4, setup_work, model_reply, visual_calls  # noqa: F401


def failed_truncation(mp4, monkeypatch, *, gap=False):
    wid, rid, asset = setup_work(mp4)
    with monkeypatch.context() as old:
        old.setattr(qv, "ANALYSIS_VERSION", "visual-v2")
        conf = confirm(wid, rid, asset)
        job = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf["id"], request_id="old-truncated")
        tasks.set_progress(job["id"], state="running")
        decoder = qv.parse_analysis
        old.setattr(qv, "parse_analysis", lambda *a, **kw: (_ for _ in ()).throw(
            ContentRejected("旧版拒绝截断", "OUTPUT_TRUNCATED")))

        def transport(*args):
            raw = json.loads(model_reply(asset.duration_ms))
            obj = json.loads(raw["choices"][0]["message"]["content"])
            if gap:
                obj["scenes"][-1]["end_ms"] -= 100
            obj.pop("warnings")
            raw["choices"][0]["message"]["content"] = json.dumps(obj, ensure_ascii=False)[:-1] + ',"warnings":["完整提醒","未完'
            raw["choices"][0]["finish_reason"] = "length"
            return 200, json.dumps(raw, ensure_ascii=False).encode()

        try:
            visual.handle_visual_analysis(job, transport=transport)
        except ContentRejected as exc:
            tasks.run_job_error(job, exc)
    return wid, rid, asset, tasks.get_job(job["id"]), visual_calls(job["id"])[0]


def test_restore_full_cached_core_creates_one_child_without_paid_call_or_rewriting_history(mp4, monkeypatch):
    wid, rid, asset, job, call = failed_truncation(mp4, monkeypatch)
    before_job, before_call = st.get("content_jobs", job["id"]), st.get("provider_jobs", call["id"])
    before_work = st.get("works", wid)
    monkeypatch.setattr(qv, "ANALYSIS_VERSION", "visual-v3")
    monkeypatch.setattr(qv, "call_frames", lambda **kw: pytest.fail("恢复不得发起供应商调用"))
    result = visual.recover_cached_truncation(wid, job["id"])
    saved = repo.get_reference(result["reference_id"])
    assert saved["parent_id"] == rid
    assert saved["visual"]["state"] == "ready" and saved["visual"]["duration_ms"] == asset.duration_ms
    assert saved["visual"]["analysis_version"] == "visual-v2", "恢复必须保留原分析配置"
    assert saved["visual"]["scenes"][-1]["end_ms"] == asset.duration_ms
    assert any("截断" in w for w in saved["visual"]["warnings"])
    assert any(call["id"] in w for w in saved["visual"]["warnings"])
    assert result["provider_call_id"] == call["id"] and result["new_paid_calls"] == 0
    assert st.get("content_jobs", job["id"]) == before_job
    assert st.get("provider_jobs", call["id"]) == before_call
    assert st.get("works", wid) == before_work
    again = visual.recover_cached_truncation(wid, job["id"])
    assert again["reference_id"] == saved["id"]
    assert len(repo.list_references(wid)) == 2


def test_incomplete_timeline_cannot_be_recovered(mp4, monkeypatch):
    wid, rid, asset, job, call = failed_truncation(mp4, monkeypatch, gap=True)
    with pytest.raises(ContentRejected):
        visual.recover_cached_truncation(wid, job["id"])
    assert len(repo.list_references(wid)) == 1
    assert st.get("provider_jobs", call["id"])["attempts"] == 1


@pytest.mark.parametrize("change", ["foreign", "unknown", "cancelled", "wrong-error", "changed-file", "changed-frames"])
def test_recovery_rejects_unbound_uncertain_or_changed_source(mp4, monkeypatch, change):
    from app.content import media, visual_pipeline as vp
    wid, rid, asset, job, call = failed_truncation(mp4, monkeypatch)
    if change == "foreign":
        wid = setup_work(mp4)[0]
    elif change == "unknown":
        st.put("provider_jobs", call["id"], dict(call, state="unknown"), {"work_id": wid})
    elif change in ("cancelled", "wrong-error"):
        changed = dict(job, state="cancelled") if change == "cancelled" else dict(job, error={"code": "OTHER"})
        st.put("content_jobs", job["id"], changed, {"work_id": wid, "kind": job["kind"],
            "request_id": job["request_id"], "input_digest": job["input_digest"], "state": changed["state"]})
    elif change == "changed-file":
        media.resolve_path(asset).write_bytes(mp4 + b"changed")
    else:
        sample = vp.sample_frames
        monkeypatch.setattr(vp, "sample_frames", lambda *args: [dict(f, sha256="sha256:changed") for f in sample(*args)])
    monkeypatch.setattr(qv, "call_frames", lambda **kw: pytest.fail("恢复不得发起供应商调用"))
    with pytest.raises(ContentRejected):
        visual.recover_cached_truncation(wid, job["id"])
    assert len(repo.list_references(job["work_id"])) == 1
