"""路线声明、前置门禁与费用门禁（AC-UI-05、AC-TH-01）。

覆盖 05 实施计划 T05 第一项要求的负例：
未选路线、未确认音频、未审人物/变体、版本错配、纯文生视频冒充主流程，
以及"报价不等于付费授权"这条最容易在实现里被悄悄放宽的边界。
"""
from __future__ import annotations

import pytest

from dh_helpers import (PRESET_IDENTITY, allow_billable, authorize_cost, install_plate,
                        install_task, install_variant, new_work, prepare_work,
                        revoke_cost_notice)
from app import config, storage as st
from app.digital_human import cost as dh_cost
from app.digital_human import routes as rt
from app.digital_human import submission as dh_sub
from app.digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected
from app.tts import audio as tts_audio


def _task(work: dict) -> dict:
    return st.get("tasks", work["current_task_id"])


# ---------- 能力视图 ----------

def test_route_view_separates_declared_and_verified():
    view = rt.route_view("OMNIHUMAN_1_5")
    assert view["provider"] == "volcengine"
    assert view["declared"]["audio_max_seconds_exclusive"] == 60
    assert view["declared"]["prompt_max_chars"] == 300
    assert view["verified"] == {}, "本项目尚未实测 OmniHuman，verified 必须为空而不是照抄声明"
    assert view["verified_state"] == "unverified"
    assert view["open_questions"], "未决问题必须列出，不能把未知当已知"


def test_m2_route_carries_historical_evidence_with_scope_warning():
    view = rt.route_view("M2")
    assert view["requires_motion_plate"] is True
    assert view["stage_count"] == 2
    exp = view["verified"]["exp_001"]
    assert exp["output_seconds"] == 15.20
    assert "非账单" in exp["scope"] and "非约 60 秒能力证据" in exp["scope"], \
        "实验数字不能升级成生产单价或长片证据"


def test_two_routes_declare_cost_tier_and_body_motion():
    """给用户挑的两条路线：低成本 M2（头肩口型）与高成本 OmniHuman（含肢体动作）。

    档位必须是路线声明的一部分，而不是前端文案：换文案不该改变"哪条要费用确认"。
    """
    m2 = rt.route_view("M2")
    oh = rt.route_view("OMNIHUMAN_1_5")
    assert m2["cost_tier"] == "low" and oh["cost_tier"] == "high"
    assert m2["cost_notice_required"] is False, "低成本路线不需要费用提示确认"
    assert oh["cost_notice_required"] is True, "高成本路线必须先确认费用提示"
    assert oh["body_motion"]["supported"] is True
    assert m2["body_motion"]["supported"] is False
    assert oh["body_motion"]["verified"] is False, "含肢体动作是声明口径，动作质量尚未逐项验收"
    assert "¥" in oh["cost_summary"] and "生成秒" in oh["cost_summary"]
    assert "GPU" in m2["cost_summary"] or "卡时" in m2["cost_summary"]


def test_cost_notice_gives_no_amount_without_audio():
    """没有计费时长就不给金额：此时的任何数字都是编的。"""
    notice = dh_cost.cost_notice("OMNIHUMAN_1_5")
    assert notice["requires_acknowledgement"] is True
    assert notice["estimated_fen"] is None and notice["amount_known"] is False
    assert "生成秒" in notice["amount_pending_reason"]
    assert notice["notice_version"] == rt.COST_NOTICE_VERSION
    assert any("未核验" in r for r in notice["risks"]), "未核验的风险必须写进提示"


def test_cost_notice_amount_matches_rate():
    notice = dh_cost.cost_notice("OMNIHUMAN_1_5", duration_ms=60000)
    assert notice["estimated_fen"] == 6000, "60 秒 × 100 分/秒 = 60 元"
    assert notice["ceiling_fen"] >= notice["estimated_fen"]
    assert notice["amount_known"] is True


def test_m2_cost_notice_says_no_unit_price():
    notice = dh_cost.cost_notice("M2", duration_ms=60000)
    assert notice["requires_acknowledgement"] is False
    assert notice["amount_known"] is False
    assert "没有云单价" in notice["amount_pending_reason"] or "不按生成秒" in notice["amount_pending_reason"]


def test_unsupported_route_rejected():
    with pytest.raises(TalkingHeadRejected) as e:
        rt.route_spec("GROK_IMAGINE_VIDEO_1_5")
    assert e.value.code == "ROUTE_UNSUPPORTED"
    assert "只读" in e.value.message


# ---------- 路线选择 ----------

def test_route_not_selected_rejected(work):
    task = dict(_task(work), route="", route_confirmed=False)
    with pytest.raises(TalkingHeadRejected) as e:
        rt.assert_route_selected(task)
    assert e.value.code == "ROUTE_NOT_SELECTED"


def test_route_not_confirmed_rejected(work):
    task = dict(_task(work), route_confirmed=False)
    with pytest.raises(TalkingHeadRejected) as e:
        rt.assert_route_selected(task)
    assert e.value.code == "ROUTE_NOT_CONFIRMED"
    assert "默认路线不是付费授权" in e.value.message


def test_route_without_traceable_selection_rejected(work):
    task = dict(_task(work), route_selection_id=None)
    with pytest.raises(TalkingHeadRejected) as e:
        rt.assert_route_selected(task)
    assert e.value.code == "ROUTE_SELECTION_UNTRACEABLE"


def test_route_request_mismatch_rejected(work):
    with pytest.raises(TalkingHeadRejected) as e:
        rt.assert_route_selected(_task(work), "M2")
    assert e.value.code == "ROUTE_MISMATCH"


def test_missing_task_cannot_submit():
    with pytest.raises(TalkingHeadBlocked) as e:
        rt.assert_route_selected(None, "M2")
    assert e.value.code == "TASK_REQUIRED"


def test_high_cost_route_requires_cost_notice_acknowledged(work):
    """高成本路线：没确认费用提示就不能选。这是服务端门禁，不是前端弹窗。"""
    revoke_cost_notice(work["id"])
    with pytest.raises(TalkingHeadBlocked) as e:
        rt.assert_route_selected(_task(work))
    assert e.value.code == "COST_NOTICE_NOT_ACKNOWLEDGED"
    assert "高成本" in e.value.message
    assert "调用清单" in e.value.message, "必须说清这与提交前的清单确认是两件事"

    out = rt.preflight(work["id"], task=_task(work))
    assert "COST_NOTICE_NOT_ACKNOWLEDGED" in [b["code"] for b in out["blockers"]]
    assert out["ok"] is False


def test_cost_notice_ack_is_version_scoped(work):
    """提示内容/费率变了，旧确认失效——不能拿旧价确认去走新价。"""
    w = st.get("works", work["id"])
    w["cost_notice_ack"] = dict(w["cost_notice_ack"], version="old-version")
    st.put("works", work["id"], w)
    with pytest.raises(TalkingHeadBlocked) as e:
        rt.assert_route_selected(_task(work))
    assert e.value.code == "COST_NOTICE_NOT_ACKNOWLEDGED"


def test_switching_to_low_cost_route_needs_no_notice(m2_work):
    """低成本路线不要求费用提示确认：不给用户平白加一道门槛。"""
    revoke_cost_notice(m2_work["id"])
    assert rt.assert_route_selected(_task(m2_work)) == "M2"


# ---------- 前置门禁 ----------

def test_preflight_passes_when_everything_ready(work):
    out = rt.preflight(work["id"], task=_task(work))
    assert out["ok"] is True, out["blockers"]
    assert out["audio_duration_ms"] == 9000
    assert out["route"] == "OMNIHUMAN_1_5"


def test_preflight_requires_confirmed_audio(work):
    audio = tts_audio.final_audio(work["id"])
    audio["confirmed"] = False
    st.put("audios", audio["id"], audio, {"work_id": work["id"]})
    out = rt.preflight(work["id"], task=_task(work))
    assert out["ok"] is False
    assert "FINAL_AUDIO_REQUIRED" in [b["code"] for b in out["blockers"]]


def test_preflight_rejects_mock_audio(work):
    audio = tts_audio.final_audio(work["id"])
    audio["is_mock"] = True
    st.put("audios", audio["id"], audio, {"work_id": work["id"]})
    out = rt.preflight(work["id"], task=_task(work))
    assert "FINAL_AUDIO_IS_MOCK" in [b["code"] for b in out["blockers"]]


def test_preflight_rejects_unapproved_identity():
    work = prepare_work()
    ident = st.get("identities", PRESET_IDENTITY)
    ident["approval_state"] = "pending"
    st.put("identities", PRESET_IDENTITY, ident)
    out = rt.preflight(work["id"], task=_task(work))
    assert "IDENTITY_NOT_APPROVED" in [b["code"] for b in out["blockers"]]


def test_preflight_rejects_audio_from_an_old_script_version(work):
    audio = tts_audio.final_audio(work["id"])
    audio["script_version_id"] = "sv_other_version"
    st.put("audios", audio["id"], audio, {"work_id": work["id"]})
    out = rt.preflight(work["id"], task=_task(work))
    codes = [b["code"] for b in out["blockers"]]
    # final_audio 已先过滤过期稿件，预检应拒绝缺少当前可用音频，不能放行旧音轨。
    assert tts_audio.final_audio(work["id"]) is None
    assert "FINAL_AUDIO_REQUIRED" in codes
    assert out["ok"] is False
    assert any(b["kind"] == "rejected" for b in out["blockers"])


def test_preflight_requires_plate_for_m2(work):
    out = rt.preflight(work["id"], task=_task(work), route="M2", require_plate=True)
    assert "PLATE_NOT_APPROVED" in [b["code"] for b in out["blockers"]]
    assert [b["kind"] for b in out["blockers"] if b["code"] == "PLATE_NOT_APPROVED"] == ["blocked"]


def test_late_plate_for_other_audio_does_not_displace_current_plate(m2_work):
    from app.digital_human import plates

    original = plates.approved_plate_for(m2_work["id"])
    plate = install_plate(m2_work["id"], duration_ms=9000,
                          variant_id=m2_work["variant_id"],
                          motion_ref_id=m2_work["motion_intent"]["motion_ref_id"],
                          audio_asset_id="audio_other_version")
    out = rt.preflight(m2_work["id"], task=_task(m2_work), route="M2", require_plate=True)
    assert out["ok"] is True, out["blockers"]
    assert out["plate_id"] == original["id"]
    assert out["plate_id"] != plate["id"]


def test_require_preflight_raises_blocked_vs_rejected(work):
    """两类失败必须能被区分：blocked 等条件变化，rejected 改输入才有意义。"""
    rt.require_preflight(work["id"], task=_task(work))  # 不抛即通过

    audio = tts_audio.final_audio(work["id"])
    audio["script_version_id"] = "sv_other"
    st.put("audios", audio["id"], audio, {"work_id": work["id"]})
    with pytest.raises(TalkingHeadRejected):
        rt.require_preflight(work["id"], task=_task(work))


# ---------- 纯文生视频冒充 ----------

def test_text_to_video_payload_rejected():
    with pytest.raises(TalkingHeadRejected) as e:
        rt.reject_text_to_video({"prompt": "一个女生在讲 RAG"})
    assert e.value.code == "TEXT_TO_VIDEO_NOT_SUPPORTED"
    assert "不是文生视频" in e.value.message


def test_text_to_video_passes_when_image_and_audio_bound():
    rt.reject_text_to_video({"prompt": "x", "variant_id": "var_1", "audio_asset_id": "audio_1"})


def test_submission_without_route_selection_is_rejected(work, monkeypatch):
    allow_billable(monkeypatch)
    task = dict(_task(work), route_confirmed=False)
    st.put("tasks", task["id"], task, {"work_id": work["id"]})
    with pytest.raises((TalkingHeadRejected, TalkingHeadBlocked)) as e:
        dh_sub.submit_omnihuman(work["id"], task=task, dry_run=True)
    assert e.value.code in ("ROUTE_NOT_CONFIRMED", "ROUTE_SELECTION_UNTRACEABLE")


# ---------- 估价与价格快照 ----------

def test_estimate_matches_user_confirmed_rate():
    est = dh_cost.estimate("OMNIHUMAN_1_5", duration_ms=60000)
    assert est["estimated_fen"] == 6000, "60 秒 × 100 分/秒 = 60 元"
    day = dh_cost.estimate("OMNIHUMAN_1_5", duration_ms=60000, per_day=5)
    assert day["per_day_fen"] == 30000
    month = dh_cost.estimate("OMNIHUMAN_1_5", duration_ms=60000, per_day=5, days=30)
    assert month["monthly_fen"] == 900000, "30 天约 9000 元"


def test_price_snapshot_is_quote_not_invoice():
    snap = dh_cost.current_price("OMNIHUMAN_1_5")
    assert snap["unit_fen"] == 100
    assert snap["price_source"] == "user_confirmed_rate"
    assert snap["billing_verified"] is False, "报价不能写成已核验账单"
    assert "未核验" in snap["rounding_note"]
    assert "未核验" in snap["failure_billing_note"]


def test_m2_has_no_unit_price():
    est = dh_cost.estimate("M2", duration_ms=9000)
    assert est["estimated_fen"] == 0
    assert est["billing_seconds"] is None
    assert est["billable"] is False
    assert "不是生产单价" in est["note"]


def test_estimate_marks_rounding_risk_and_excluded_costs():
    est = dh_cost.estimate("OMNIHUMAN_1_5", duration_ms=60000, segment_count=6)
    assert est["ceiling_fen"] >= est["estimated_fen"]
    assert "TTS" in est["excluded"] and "返工重做" in est["excluded"]


# ---------- 费用确认 ----------

def test_cost_confirmation_requires_user_actor(work):
    with pytest.raises(TalkingHeadRejected) as e:
        dh_cost.confirm_cost(work["id"], route="OMNIHUMAN_1_5", request_digest="sha256:x",
                             estimated_fen=6000, ceiling_fen=7200, actor_id="agent_1")
    assert e.value.code == "COST_ACTOR_INVALID"


def test_cost_confirmation_requires_request_digest(work):
    with pytest.raises(TalkingHeadRejected) as e:
        dh_cost.confirm_cost(work["id"], route="OMNIHUMAN_1_5", request_digest="",
                             estimated_fen=6000, ceiling_fen=7200, actor_id="user_1")
    assert e.value.code == "COST_REQUEST_DIGEST_REQUIRED"


def test_cost_confirmation_does_not_authorize_payment(work):
    rec = dh_cost.confirm_cost(work["id"], route="OMNIHUMAN_1_5", request_digest="sha256:x",
                               estimated_fen=6000, ceiling_fen=7200, actor_id="user_1")
    assert rec["authorizes_payment"] is False
    assert rec["max_billable_submissions"] == 0
    assert "不是付费授权" in rec["note"]


def test_cost_confirmation_does_not_fake_a_time_based_expiry(work):
    """确认记录不得写一个算出来的"有效期"却无人校验——那是对用户的假承诺。"""
    rec = dh_cost.confirm_cost(work["id"], route="OMNIHUMAN_1_5",
                               request_digest="sha256:x", estimated_fen=6000,
                               ceiling_fen=7200, actor_id="user_1")
    assert rec["valid_until"] is None, "有到期时间就必须有人校验它，否则不得写"
    assert rec["expiry_policy"] == "invalidate_on_change"
    assert "不按时间自动过期" in rec["expiry_note"]


def test_m2_blocker_explains_gpu_not_unit_price(work):
    """M2 没有云单价，拦它的理由必须是卡时与预算，而不是"计费路线"。"""
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_cost.assert_submission_allowed(work["id"], route="M2",
                                          request_digest="sha256:x", estimated_fen=0)
    assert e.value.code == "COST_CONFIRMATION_REQUIRED"
    assert "计费路线" not in e.value.message, \
        "与自家价格快照（unit=no_unit_price）矛盾的措辞会让人去找不存在的单价"
    assert "GPU" in e.value.message and "卡时" in e.value.message

    dh_cost.confirm_cost(work["id"], route="M2", request_digest="sha256:x",
                         estimated_fen=0, ceiling_fen=0, actor_id="user_1")
    with pytest.raises(TalkingHeadBlocked) as e2:
        dh_cost.assert_submission_allowed(work["id"], route="M2",
                                          request_digest="sha256:x", estimated_fen=0)
    assert e2.value.code == "BUDGET_NOT_AUTHORIZED"
    assert "卡时" in e2.value.message


def test_submission_blocked_without_budget_even_after_confirmation(work):
    """报价确认齐全，但预算未授权 → 仍然拦。这是本任务最容易写错的一条。"""
    conf = authorize_cost(work["id"])
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_cost.assert_submission_allowed(work["id"], route="OMNIHUMAN_1_5",
                                          request_digest=conf["request_digest"],
                                          estimated_fen=conf["estimated_fen"])
    assert e.value.code == "BUDGET_NOT_AUTHORIZED"
    assert "报价确认不等于付费授权" in e.value.message


def test_cost_ceiling_exceeded_stops_submission(work, monkeypatch):
    allow_billable(monkeypatch)
    digest = dh_sub.call_list_digest("OMNIHUMAN_1_5", audio_id="a", audio_hash="h",
                                     variant_id=None, motion_ref_id=None, prompt="",
                                     resolution="1080p", segment_count=1)
    dh_cost.confirm_cost(work["id"], route="OMNIHUMAN_1_5", request_digest=digest,
                         estimated_fen=6000, ceiling_fen=6500, actor_id="user_1")
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_cost.assert_submission_allowed(work["id"], route="OMNIHUMAN_1_5",
                                          request_digest=digest, estimated_fen=9000)
    assert e.value.code == "COST_CEILING_EXCEEDED"
    assert "超额须停止提交" in e.value.message


def test_changed_call_list_requires_reconfirmation(work, monkeypatch):
    allow_billable(monkeypatch)
    old = dh_sub.call_list_digest("OMNIHUMAN_1_5", audio_id="a1", audio_hash="h",
                                  variant_id=None, motion_ref_id=None, prompt="",
                                  resolution="1080p", segment_count=1)
    dh_cost.confirm_cost(work["id"], route="OMNIHUMAN_1_5", request_digest=old,
                         estimated_fen=6000, ceiling_fen=7200, actor_id="user_1")
    new = dh_sub.call_list_digest("OMNIHUMAN_1_5", audio_id="a1", audio_hash="h",
                                  variant_id=None, motion_ref_id=None, prompt="加一个手势",
                                  resolution="1080p", segment_count=1)
    with pytest.raises(TalkingHeadRejected) as e:
        dh_cost.assert_submission_allowed(work["id"], route="OMNIHUMAN_1_5",
                                          request_digest=new, estimated_fen=6000)
    assert e.value.code == "COST_LIST_CHANGED"


def test_call_list_digest_changes_with_every_cost_relevant_field(work):
    base = dh_sub.call_list_digest("OMNIHUMAN_1_5", audio_id="a", audio_hash="h",
                                   variant_id="v", motion_ref_id=None, prompt="p",
                                   resolution="1080p", segment_count=1)
    variants = [
        dict(audio_id="a2"), dict(audio_hash="h2"), dict(variant_id="v2"),
        dict(prompt="p2"), dict(resolution="720p"), dict(segment_count=2),
    ]
    for change in variants:
        kwargs = dict(audio_id="a", audio_hash="h", variant_id="v", motion_ref_id=None,
                      prompt="p", resolution="1080p", segment_count=1)
        kwargs.update(change)
        assert dh_sub.call_list_digest("OMNIHUMAN_1_5", **kwargs) != base, change


# ---------- Mock 不伪造媒体 ----------

def test_mock_submission_registers_nothing(work, monkeypatch):
    allow_billable(monkeypatch)
    authorize_cost(work["id"])
    out = dh_sub.submit_omnihuman(work["id"], task=_task(work), dry_run=True)
    assert out["submitted"] is False and out["is_mock"] is True
    assert out["runs"][0]["is_mock"] is True
    from app.digital_human import videos as vd

    assert vd.list_videos(work["id"]) == [], "占位提交不得登记任何视频产物"
    assert "不得进入审核与后期" in out["note"]


def test_api_capabilities_mode_stays_mock_in_test_env():
    assert config.resolve_mode() == "mock"
