"""M2 两阶段依赖：底片生成条件、时长核对、身份保持与对口型前置（AC-TH-01/05）。

这一组用例守的是"中间产物不能当终点"：

- 底片没审、变体没审、动作没获准 —— 都不能提交对口型；
- 底片比音频短 —— 必然要加速或截字，属明确禁止，不能靠"反正能裁"糊过去；
- 底片身份摘要与冻结基准不符 —— 判定为身份漂移，不是"看起来还行"；
- 审核过的文件被替换 —— 旧审核立即失效，缓存复用必须重核。
"""
from __future__ import annotations

import pytest

from dh_helpers import (HASH_B, allow_billable, binary_response, install_alignment,
                        install_final_audio, install_plate, install_variant, m2_ok,
                        make_mp4, make_transport, new_work, prepare_work)
from app import jobs, storage as st
from app.avatar import registry
from app.digital_human import plates as pl
from app.digital_human import providers as pv
from app.digital_human import submission as dh_sub
from app.digital_human import videos as vd
from app.digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected


def _tasks(work):
    return st.get("tasks", work["current_task_id"])


# ---------- 底片生成的前置 ----------

def test_plate_requires_approved_variant(m2_work):
    variant = st.get("variants", m2_work["variant_id"])
    variant["approval_state"] = "pending"
    st.put("variants", variant["id"], variant)
    with pytest.raises(TalkingHeadBlocked) as e:
        pl.create_plate(m2_work["id"], dry_run=True)
    assert e.value.code == "VARIANT_NOT_APPROVED"


def test_plate_rejects_variant_from_another_identity(m2_work):
    variant = st.get("variants", m2_work["variant_id"])
    variant["identity_id"] = "av_someone_else"
    st.put("variants", variant["id"], variant)
    with pytest.raises(TalkingHeadRejected) as e:
        pl.create_plate(m2_work["id"], dry_run=True)
    assert e.value.code == "VARIANT_IDENTITY_MISMATCH"
    assert "换人负例" in e.value.message


def test_plate_requires_motion_intent(m2_work):
    work = st.get("works", m2_work["id"])
    work["motion_intent"] = {}
    st.put("works", work["id"], work)
    with pytest.raises(TalkingHeadRejected) as e:
        pl.create_plate(m2_work["id"], dry_run=True)
    assert e.value.code == "MOTION_REQUIRED"


def test_plate_requires_approved_motion_reference(m2_work):
    ref_id = m2_work["motion_intent"]["motion_ref_id"]
    ref = st.get("motion_references", ref_id)
    ref["approval_state"] = "pending"
    st.put("motion_references", ref_id, ref, {"identity_id": ref["identity_id"]})
    with pytest.raises(TalkingHeadRejected) as e:
        pl.create_plate(m2_work["id"], dry_run=True)
    assert e.value.code == "MOTION_NOT_APPROVED"


def test_plate_requires_confirmed_audio():
    from dh_helpers import install_identity, install_motion_ref, install_voice_ready
    from app.tts import audio as tts_audio

    install_identity()
    install_voice_ready()
    install_motion_ref()
    work = new_work(route="M2")
    variant = install_variant()
    work = st.get("works", work["id"])
    work["variant_id"] = variant["id"]
    ref = [r for r in st.all_rows("motion_references")
           if r["identity_id"] == work["identity_id"]][-1]
    work["motion_intent"] = {"motion_key": ref["motion_key"], "motion_ref_id": ref["id"],
                             "params": {"amplitude": 0.4}}
    st.put("works", work["id"], work)
    assert tts_audio.final_audio(work["id"]) is None
    with pytest.raises(TalkingHeadBlocked) as e:
        pl.create_plate(work["id"], dry_run=True)
    assert e.value.code == "FINAL_AUDIO_REQUIRED"


def test_mock_plate_is_not_registered(m2_work):
    out = pl.create_plate(m2_work["id"], dry_run=True)
    assert out["registered"] is False and out["is_mock"] is True
    assert "不登记为可用资产" in out["note"]
    assert [v for v in vd.list_videos(m2_work["id"], vd.KIND_PLATE)
            if v.get("is_mock")] == []


def test_real_plate_registers_with_hashes_and_run(m2_work, monkeypatch):
    allow_billable(monkeypatch)
    mp4 = make_mp4(9000)
    transport, calls = make_transport(responses=[
        m2_ok(output_url="https://gpu.example.com/plate.mp4"),
        binary_response(mp4),
    ])
    out = pl.create_plate(m2_work["id"], transport=transport, dry_run=False)
    assert out["registered"] is True
    video = out["video"]
    assert video["kind"] == vd.KIND_PLATE
    assert video["file_hash"].startswith("sha256:")
    assert video["provider_run_id"], "底片必须留下作业/运行记录"
    assert video["duration_ms"] == 9000
    assert len(calls) == 2, "一次提交 + 一次下载"
    lp_args = calls[0]["args"]
    assert lp_args["source_hash"] and lp_args["motion_hash"], \
        "源图与动作参考哈希必须进作业参数，否则版本错配无从判定"


def test_plate_job_failed_is_blocked_not_claimed(m2_work, monkeypatch):
    """远端明确失败：blocked，且不得宣称成功、不得自动回退。"""
    allow_billable(monkeypatch)
    transport, _ = make_transport(responses=[
        {"status_code": 200, "json": {"state": "failed", "error": "显存不足"}}])
    with pytest.raises(TalkingHeadBlocked) as e:
        pl.create_plate(m2_work["id"], transport=transport, dry_run=False)
    assert e.value.code == "PLATE_JOB_NOT_COMPLETED"
    assert "不自动回退" in e.value.message


def test_plate_job_running_is_blocked_but_not_recorded_as_failure(m2_work, monkeypatch):
    """还在生成 ≠ 失败。

    这条钉的是红线条文："排队不是失败"。若把 running 归成作业失败，
    用户会以为要重提，而重提会在同一张 GPU 上多排一份相同的队。
    """
    allow_billable(monkeypatch)
    transport, _ = make_transport(responses=[
        {"status_code": 200, "json": {"state": "running", "job_id": "job_abc123"}}])
    with pytest.raises(TalkingHeadBlocked) as e:
        pl.create_plate(m2_work["id"], transport=transport, dry_run=False)
    assert e.value.code == "PLATE_JOB_STILL_RUNNING"
    assert "PLATE_JOB_NOT_COMPLETED" != e.value.code, "running 不得与 failed 共用错误码"
    assert e.value.detail.get("remote_job_id") == "job_abc123", \
        "必须带回远端作业号，否则下一次查询找不到同一个作业"


# ---------- 时长核对 ----------

def test_plate_shorter_than_audio_is_rejected(m2_work):
    plate = install_plate(m2_work["id"], duration_ms=8000, approved=False,
                          variant_id=m2_work["variant_id"],
                          motion_ref_id=m2_work["motion_intent"]["motion_ref_id"])
    with pytest.raises(TalkingHeadRejected) as e:
        pl.check_plate_duration(plate, 9000, strict=True)
    assert e.value.code == "PLATE_TOO_SHORT"
    assert "加速或截字" in e.value.message


def test_plate_longer_than_audio_records_excess(m2_work):
    plate = install_plate(m2_work["id"], duration_ms=9600, approved=False,
                          variant_id=m2_work["variant_id"],
                          motion_ref_id=m2_work["motion_intent"]["motion_ref_id"])
    info = pl.check_plate_duration(plate, 9000, strict=True)
    assert info["excess_ms"] == 600
    assert "provider_lead_ms" in info["note"]
    assert "不得靠改字幕掩盖" in info["note"]


def test_plate_review_records_duration_evidence(m2_work):
    plate = install_plate(m2_work["id"], duration_ms=9600, approved=False,
                          variant_id=m2_work["variant_id"],
                          motion_ref_id=m2_work["motion_intent"]["motion_ref_id"])
    rec = pl.record_plate_review(plate["id"], reviewer="user_1", outcome="pass",
                                 audio_duration_ms=9000)
    assert rec["duration"]["excess_ms"] == 600
    assert vd.get_video(plate["id"])["excess_ms"] == 600


# ---------- 身份保持 ----------

def test_plate_identity_drift_rejected(m2_work):
    plate = install_plate(m2_work["id"], duration_ms=9000, approved=False,
                          variant_id=m2_work["variant_id"],
                          motion_ref_id=m2_work["motion_intent"]["motion_ref_id"])
    with pytest.raises(TalkingHeadRejected) as e:
        pl.record_plate_review(plate["id"], reviewer="user_1", outcome="pass",
                               audio_duration_ms=9000, identity_digest="sha256:someone_else")
    assert e.value.code == "PLATE_IDENTITY_DRIFT"


def test_plate_review_pass_requires_user(m2_work):
    plate = install_plate(m2_work["id"], duration_ms=9000, approved=False,
                          variant_id=m2_work["variant_id"],
                          motion_ref_id=m2_work["motion_intent"]["motion_ref_id"])
    with pytest.raises(TalkingHeadRejected) as e:
        pl.record_plate_review(plate["id"], reviewer="agent_1", outcome="pass")
    assert e.value.code == "PLATE_REVIEW_ACTOR_INVALID"
    assert "不做自动身份判定" in e.value.message


def test_approved_plate_invalidated_after_file_replaced(m2_work):
    plate = pl.approved_plate_for(m2_work["id"])
    assert plate is not None
    row = vd.get_video(plate["id"])
    row["file_hash"] = "sha256:" + "9" * 64
    st.put(vd.VIDEO_TABLE, row["id"], row, {"work_id": m2_work["id"]})
    assert pl.approved_plate_for(m2_work["id"]) is None, \
        "文件哈希变了，旧审核摘要必须立刻失效"


# ---------- 对口型前置 ----------

def test_musetalk_requires_approved_plate(work):
    """同一个作品没有底片时，M2 提交必须停在 PLATE_NOT_APPROVED。"""
    work = prepare_work(route="M2", with_plate=False)
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_sub.submit_m2(work["id"], task=_tasks(work), dry_run=True)
    assert e.value.code in ("PLATE_NOT_APPROVED",)


def test_musetalk_request_requires_both_hashes():
    with pytest.raises(TalkingHeadRejected) as e:
        pv.build_musetalk_request(plate_url="https://x/p.mp4", audio_url="https://x/a.wav",
                                  plate_hash="", audio_hash="sha256:a")
    assert e.value.code == "MT_INPUT_HASH_MISSING"


def test_musetalk_cannot_swap_audio_silently():
    """请求体绑定的是传入的音频哈希；换一条音频必然改变哈希，从而改变作业身份。"""
    a = pv.build_musetalk_request(plate_url="https://x/p.mp4", audio_url="https://x/a.wav",
                                  plate_hash="sha256:p", audio_hash="sha256:a1")
    b = pv.build_musetalk_request(plate_url="https://x/p.mp4", audio_url="https://x/a.wav",
                                  plate_hash="sha256:p", audio_hash="sha256:a2")
    assert jobs.args_digest(a["args"]) != jobs.args_digest(b["args"])


def test_submit_m2_dry_run_registers_nothing(m2_work, monkeypatch):
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(m2_work["id"], route="M2")
    before = len(vd.list_videos(m2_work["id"], vd.KIND_ORIGINAL))
    out = dh_sub.submit_m2(m2_work["id"], task=_tasks(m2_work), dry_run=True)
    assert out["submitted"] is False and out["is_mock"] is True
    assert len(vd.list_videos(m2_work["id"], vd.KIND_ORIGINAL)) == before


def test_m2_real_submission_blocked_without_budget(m2_work):
    """预算未授权时 GPU 作业停在 blocked：不产生外部副作用，但尝试本身要入账。"""
    from dh_helpers import authorize_cost

    authorize_cost(m2_work["id"], route="M2")
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_sub.submit_m2(m2_work["id"], task=_tasks(m2_work), transport=lambda r: m2_ok(),
                         dry_run=False)
    assert e.value.code in ("BUDGET_NOT_AUTHORIZED", "JOB_BLOCKED")
    runs = dh_sub.runs_for_work(m2_work["id"])
    assert runs and runs[-1]["state"] in ("blocked", "failed", "intent"), \
        "被预算阻断的提交尝试应留下记录，而不是消失"
    assert runs[-1]["last_error"]["code"] == e.value.code, \
        "记录必须写明是门禁的哪一条拦下的，否则审计无从复核"


def test_failure_is_preserved_without_auto_fallback(m2_work, monkeypatch):
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(m2_work["id"], route="M2")
    run = dh_sub.start_run(m2_work["id"], route="M2", provider="m2_worker",
                           model="MuseTalk-1.5", capability="lipsync",
                           request_digest="sha256:x", idempotency_key="k1")
    failed = dh_sub.record_failure(run["id"], code="M2_JOB_FAILED", message="GPU 掉线")
    assert failed["state"] == "failed"
    assert failed["auto_fallback"] is False
    # 没有产生任何"自动改用其他路线"的运行
    assert all(r["route"] == "M2" for r in dh_sub.runs_for_work(m2_work["id"]))


def test_alignment_required_before_segmentation():
    work = prepare_work(audio_ms=75000, with_alignment=False)
    from app.digital_human import segments as sg

    with pytest.raises(TalkingHeadBlocked) as e:
        sg.create_plan(work["id"])
    assert e.value.code == "SEGMENTATION_REQUIRES_ALIGNMENT"


def test_plate_status_reports_mock_and_approved(m2_work):
    status = pl.plate_status(m2_work["id"])
    assert status["approved"] is True
    assert status["approved_plate_id"]
    assert all(not p["is_mock"] for p in status["plates"])


def test_polling_same_remote_output_twice_reuses_one_original(m2_work):
    """多个本地运行记录指向同一远端任务时，不得登记重复原片。"""
    import hashlib
    raw = make_mp4(9000, audio=True)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    probe = {"duration_ms": 9000, "file_hash": digest, "has_video_track": True,
             "has_audio_track": True, "width": 720, "height": 1280, "fps": 25}
    listing = dh_sub.current_call_list(m2_work["id"], route="M2")
    first = dh_sub.start_run(m2_work["id"], route="M2", provider="m2_worker",
                             model="MuseTalk-1.5", capability="lipsync",
                             request_digest=listing["digest"], idempotency_key="dup:1",
                             provider_params={"plate_id": listing["plate_id"]})
    second = dh_sub.start_run(m2_work["id"], route="M2", provider="m2_worker",
                              model="MuseTalk-1.5", capability="lipsync",
                              request_digest=listing["digest"], idempotency_key="dup:2",
                              provider_params={"plate_id": listing["plate_id"]})
    for run in (first, second):
        dh_sub._update_run(run["id"], remote_job_id="remote_same_result", state="waiting_provider")
    downloaded = {"probe": probe, "data": raw}
    first_result = dh_sub._register_original(first["id"], downloaded, note="test")
    second_result = dh_sub._register_original(second["id"], downloaded, note="test")
    originals = vd.list_videos(m2_work["id"], vd.KIND_ORIGINAL)
    assert len(originals) == 1
    assert second_result["video"]["id"] == first_result["video"]["id"]
