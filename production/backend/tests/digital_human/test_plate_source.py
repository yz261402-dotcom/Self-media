"""底片直接使用已定版原图，来源与音频必须贯穿登记及复用。"""
import pytest

from app import jobs, storage as st
from app.avatar import licensing, registry
from app.contracts import HERO_ONLY_LIMITATIONS, VideoAssetInfo
from app.digital_human import plates, routes, videos
from app.digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected
from app.tts import audio as tts_audio
from dh_helpers import (HASH_A, HASH_B, allow_billable, binary_response,
                        install_final_audio, install_motion_ref, install_variant, make_mp4,
                        make_transport, m2_ok, prepare_work)


@pytest.fixture
def hero_work():
    work = prepare_work(route="M2", with_variant=False)
    ident = registry.create_identity_candidate("底片原图测试", "写实主播", origin="generated")
    lic = licensing.register(subject=ident["id"], kind="image_source", scope="commercial_ok",
                             evidence="测试夹具权属声明，不是生产授权", verified_by="user_test")
    hero = registry.register_asset(ident["id"], "hero", storage_uri="https://cdn.example.com/hero.png",
                                   file_hash=HASH_A, source_evidence="测试构造的原图", license_id=lic["id"])
    registry.record_review(hero["id"], reviewer="user_test", outcome="pass", identity_digest=HASH_B,
                           method="manual")
    registry.set_reference_policy(ident["id"], "hero_only", "user_test", reason="测试构造",
                                  acknowledged=HERO_ONLY_LIMITATIONS)
    registry.freeze_identity(ident["id"], hero["id"], HASH_B, reviewer="user_test", license_id=lic["id"])
    motion = install_motion_ref(ident["id"])
    work.update(identity_id=ident["id"], variant_id=None,
                motion_intent={"motion_key": motion["motion_key"], "motion_ref_id": motion["id"]})
    st.put("works", work["id"], work)
    return work


def test_approved_hero_can_prepare_plate_without_creating_variant(hero_work):
    before = st.all_rows("variants")
    output = plates.create_plate(hero_work["id"], dry_run=True)
    hero = registry.get_asset(registry.get_identity(hero_work["identity_id"])["hero_asset_id"])
    assert output["source"]["kind"] == "hero"
    assert output["source"]["asset_id"] == hero["id"]
    assert output["request"]["args"]["source_hash"] == hero["file_hash"]
    assert output["variant_id"] is None
    assert st.all_rows("variants") == before
    status = plates.plate_status(hero_work["id"])
    assert status["preparation"]["ok"] is True
    assert status["preparation"]["source"] == output["source"]
    preflight = routes.preflight(hero_work["id"], route="M2",
                                task=st.get("tasks", hero_work["current_task_id"]), require_plate=False)
    assert preflight["ok"] is True, preflight["blockers"]
    assert preflight["source"]["asset_id"] == hero["id"]


def test_explicit_bad_variant_never_falls_back_to_hero(hero_work):
    with pytest.raises(TalkingHeadRejected) as error:
        plates.create_plate(hero_work["id"], variant_id="var_missing", dry_run=True)
    assert error.value.code == "VARIANT_NOT_FOUND"
    status = plates.plate_status(hero_work["id"], variant_id="var_missing")
    assert status["preparation"]["ok"] is False
    assert status["preparation"]["blockers"][0]["code"] == "VARIANT_NOT_FOUND"


@pytest.mark.parametrize("change", ["pending", "hash", "license", "wrong_identity", "mock", "style"])
def test_hero_approval_source_and_license_are_not_bypassed(hero_work, change):
    ident = registry.get_identity(hero_work["identity_id"])
    hero = registry.get_asset(ident["hero_asset_id"])
    if change == "pending":
        hero["approval_state"] = "pending"
    elif change == "hash":
        hero["file_hash"] = "sha256:" + "f" * 64
    elif change == "license":
        licensing.revoke_commercial(hero["license_id"], "user_test", "测试撤销")
    elif change == "wrong_identity":
        hero["identity_id"] = "av_other"
    elif change == "mock":
        hero["is_mock"] = True
    else:
        ident["keywords"] = {"style": "cartoon"}
        st.put("identities", ident["id"], ident)
    st.put("avatar_assets", hero["id"], hero, {"identity_id": hero["identity_id"]})
    with pytest.raises((TalkingHeadBlocked, TalkingHeadRejected)):
        plates.create_plate(hero_work["id"], dry_run=True)
    assert plates.plate_status(hero_work["id"])["preparation"]["ok"] is False


def test_explicit_unapproved_variant_does_not_use_valid_hero(hero_work):
    variant = install_variant(hero_work["identity_id"])
    variant["approval_state"] = "pending"
    st.put("variants", variant["id"], variant)
    with pytest.raises(TalkingHeadBlocked) as error:
        plates.create_plate(hero_work["id"], variant_id=variant["id"], dry_run=True)
    assert error.value.code == "VARIANT_NOT_APPROVED"


def test_generated_plate_keeps_source_and_audio_and_can_enter_lipsync(hero_work, monkeypatch):
    allow_billable(monkeypatch)
    transport, calls = make_transport(responses=[m2_ok(), binary_response(make_mp4(9000, audio=False))])
    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/plate.mp4")
    output = plates.create_plate(hero_work["id"], dry_run=False, transport=transport)
    video = output["video"]
    hero = registry.get_asset(registry.get_identity(hero_work["identity_id"])["hero_asset_id"])
    assert video["audio_asset_id"] == tts_audio.final_audio(hero_work["id"])["id"]
    assert video["source_asset_id"] == hero["id"]
    assert video["source_asset_hash"] == hero["file_hash"]
    assert video["variant_id"] is None
    VideoAssetInfo(**video)
    plates.record_plate_review(video["id"], reviewer="user_test", outcome="pass",
                               audio_duration_ms=9000, identity_digest=HASH_B)
    assert plates.approved_plate_for(hero_work["id"])["id"] == video["id"]
    check = routes.preflight(hero_work["id"], route="M2", task=st.get("tasks", hero_work["current_task_id"]))
    assert check["ok"] is True, check["blockers"]
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [("identity_id", "av_other"), ("variant_id", "var_other"),
                                       ("audio_asset_id", "audio_other"), ("source_asset_id", "img_other"),
                                       ("source_asset_hash", HASH_B), ("source_asset_hash", None)])
def test_approved_plate_cannot_cross_current_input_bindings(hero_work, monkeypatch, field, value):
    allow_billable(monkeypatch)
    transport, _ = make_transport(responses=[m2_ok(), binary_response(make_mp4(9000, audio=False))])
    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/plate.mp4")
    video = plates.create_plate(hero_work["id"], dry_run=False, transport=transport)["video"]
    plates.record_plate_review(video["id"], reviewer="user_test", outcome="pass", audio_duration_ms=9000)
    video = videos.get_video(video["id"])
    video[field] = value
    st.put("videos", video["id"], video, {"work_id": hero_work["id"]})
    assert plates.approved_plate_for(hero_work["id"]) is None


def test_preparation_does_not_depend_on_downstream_plan_or_plate(hero_work):
    work = dict(hero_work, performance_plan_id=None)
    st.put("works", work["id"], work)
    check = plates.plate_status(work["id"])["preparation"]
    assert check["ok"] is True, check["blockers"]
    assert check["source"]["kind"] == "hero"


def test_repeating_completed_plate_reuses_output_without_second_submission(hero_work, monkeypatch):
    allow_billable(monkeypatch)
    transport, calls = make_transport(responses=[m2_ok(), binary_response(make_mp4(9000, audio=False))])
    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/plate.mp4")
    first = plates.create_plate(hero_work["id"], dry_run=False, transport=transport)
    assert jobs.get_job(first["job_id"])["state"] == "succeeded"
    second = plates.create_plate(hero_work["id"], dry_run=False, transport=transport)
    assert second["video"]["id"] == first["video"]["id"]
    assert second["replayed"] is True
    assert len(calls) == 2


@pytest.mark.parametrize("state", ["running", "failed"])
def test_repeating_uncompleted_plate_never_submits_again(hero_work, monkeypatch, state):
    allow_billable(monkeypatch)
    transport, calls = make_transport(default={"status_code": 200,
                                               "json": {"state": state, "job_id": "remote_1"}})
    with pytest.raises(TalkingHeadBlocked):
        plates.create_plate(hero_work["id"], dry_run=False, transport=transport)
    with pytest.raises(TalkingHeadBlocked):
        plates.create_plate(hero_work["id"], dry_run=False, transport=transport)
    assert len(calls) == 1


def test_equal_length_new_audio_cannot_replay_old_audio_binding(hero_work, monkeypatch):
    allow_billable(monkeypatch)
    transport, calls = make_transport(responses=[m2_ok(), binary_response(make_mp4(9000, audio=False)),
                                                m2_ok(), binary_response(make_mp4(9000, audio=False))])
    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/plate.mp4")
    first = plates.create_plate(hero_work["id"], dry_run=False, transport=transport)
    new_audio = install_final_audio(hero_work["id"], duration_ms=9000)
    second = plates.create_plate(hero_work["id"], dry_run=False, transport=transport)
    assert second["video"]["audio_asset_id"] == new_audio["id"]
    assert second["job_id"] != first["job_id"]
    assert len(calls) == 4


def _motion_jobs(work_id: str):
    return [j for j in jobs.jobs_for_work(work_id) if j.get("capability") == "motion"]


def test_reconciled_cancelled_plate_releases_key_for_one_fresh_attempt(hero_work, monkeypatch):
    """对账结论为 cancelled 的死尝试不得永久占死幂等键。

    否则一次传输抖动就会把这组（作品/图/动作/音频）彻底锁死，
    且没有任何输入可以改动来解锁。
    """
    allow_billable(monkeypatch)

    def broken(req):
        raise RuntimeError("隧道中断：提交未取得可核对结果")

    dead_transport, _ = make_transport(default=broken)
    with pytest.raises(RuntimeError):
        plates.create_plate(hero_work["id"], dry_run=False, transport=dead_transport)
    stuck = _motion_jobs(hero_work["id"])[-1]
    assert stuck["state"] == "unknown"

    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/plate.mp4")
    good_transport, good_calls = make_transport(
        responses=[m2_ok(), binary_response(make_mp4(9000, audio=False))])

    # 未对账前：重复点击仍被拦下，且不得产生新的 GPU 提交
    with pytest.raises(TalkingHeadBlocked) as blocked:
        plates.create_plate(hero_work["id"], dry_run=False, transport=good_transport)
    assert blocked.value.code == "PLATE_JOB_ALREADY_SUBMITTED"
    assert len(good_calls) == 0, "未对账的未知作业不得触发新的 GPU 提交"

    # 对账落定：远端从未收到该作业，无产物、无计费
    jobs.resolve_unknown(stuck["id"], "cancelled",
                         {"source": "remote-jobs-listing", "remote_seen": False})

    # 对账后重新点击：允许一次全新尝试
    output = plates.create_plate(hero_work["id"], dry_run=False, transport=good_transport)
    assert output["registered"] is True
    assert output["job_id"] != stuck["id"]
    assert jobs.get_job(output["job_id"])["state"] == "succeeded"
    assert len(good_calls) == 2

    # 新世代已成功：再次点击复用产物，不得重复提交
    again = plates.create_plate(hero_work["id"], dry_run=False, transport=good_transport)
    assert again["video"]["id"] == output["video"]["id"]
    assert len(good_calls) == 2, "已成功的世代不得重复提交"


def test_fresh_attempt_after_reconcile_still_blocks_second_queue(hero_work, monkeypatch):
    """对账放行的只是"一次"新尝试：同一死世代内的重复点击仍不得多排 GPU 队列。"""
    allow_billable(monkeypatch)

    def broken(req):
        raise RuntimeError("隧道中断")

    dead_transport, _ = make_transport(default=broken)
    with pytest.raises(RuntimeError):
        plates.create_plate(hero_work["id"], dry_run=False, transport=dead_transport)
    stuck = _motion_jobs(hero_work["id"])[-1]
    jobs.resolve_unknown(stuck["id"], "cancelled", {"source": "remote-jobs-listing"})

    running, calls = make_transport(default={"status_code": 200,
                                             "json": {"state": "running", "job_id": "remote_1"}})
    with pytest.raises(TalkingHeadBlocked):
        plates.create_plate(hero_work["id"], dry_run=False, transport=running)
    with pytest.raises(TalkingHeadBlocked):
        plates.create_plate(hero_work["id"], dry_run=False, transport=running)
    assert len(calls) == 1, "同一死世代只放行一次新尝试"
