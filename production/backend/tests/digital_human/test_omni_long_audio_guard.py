"""OmniHuman 长音频策略：60 秒及以上禁止该路线。

OmniHuman 分段独立生成会造成接缝跳变，因此长音频不再自动切片、续发或拼接。
这些用例只走离线夹具，不调用真实供应商，也不产生付费作业。
"""
from __future__ import annotations

import pytest

from app import storage as st
from app.digital_human import omni_driver, routes, segments, submission
from app.digital_human.errors import TalkingHeadRejected
from dh_helpers import prepare_work


LONG_AUDIO_MESSAGE = "不再自动分段"


def _task(work: dict) -> dict:
    return st.get("tasks", work["current_task_id"])


def test_preflight_rejects_omnihuman_audio_at_sixty_seconds():
    work = prepare_work(audio_ms=60000)

    out = routes.preflight(work["id"], task=_task(work),
                           route=routes.ROUTE_OMNIHUMAN)

    blockers = [b for b in out["blockers"] if b["code"] == "OMNIHUMAN_AUDIO_TOO_LONG"]
    assert blockers and blockers[0]["kind"] == "rejected"
    assert LONG_AUDIO_MESSAGE in blockers[0]["message"]
    assert "请缩短音频" in blockers[0]["message"]
    assert out["ok"] is False


def test_current_call_list_rejects_long_omnihuman_audio_without_segment_plan():
    work = prepare_work(audio_ms=75000)

    with pytest.raises(TalkingHeadRejected) as raised:
        submission.current_call_list(work["id"], route=routes.ROUTE_OMNIHUMAN)

    assert raised.value.code == "OMNIHUMAN_AUDIO_TOO_LONG"
    assert LONG_AUDIO_MESSAGE in raised.value.message
    assert "请缩短音频" in raised.value.message


def test_submit_rejects_long_omnihuman_before_transport_or_provider_records(
        monkeypatch):
    work = prepare_work(audio_ms=75000)
    calls = []

    def transport(request):
        calls.append(request)
        raise AssertionError("长音频不应触发供应商调用")

    with pytest.raises(TalkingHeadRejected) as raised:
        submission.submit_omnihuman(work["id"], task=_task(work),
                                     transport=transport, dry_run=False)

    assert raised.value.code == "OMNIHUMAN_AUDIO_TOO_LONG"
    assert LONG_AUDIO_MESSAGE in raised.value.message
    assert calls == []
    assert submission.runs_for_work(work["id"]) == []


def test_old_segment_driver_cannot_resume_long_omnihuman_run():
    work = prepare_work(audio_ms=75000, with_alignment=True,
                        text="第一句足够长用于构造旧分段计划。第二句也足够长用于构造旧分段计划。")
    plan = segments.create_plan(work["id"])
    assert len(plan["segments"]) >= 2
    run = submission.start_run(
        work["id"], route=routes.ROUTE_OMNIHUMAN, provider="volcengine",
        model="omnihuman-1.5", capability=routes.CAP_AVATAR_VIDEO_CLOUD,
        request_digest="sha256:" + "a" * 64,
        idempotency_key="legacy-segment-resume",
        provider_params={"segment_index": 0, "segment_count": len(plan["segments"])},
    )

    outcome = omni_driver.advance_once(work["id"], transport=lambda request: pytest.fail(
        "没有远端任务号的旧分段不应触发查询或供应商调用"))

    assert outcome["state"] == "STOPPED"
    assert outcome["stop_reason"] == "SEGMENT_SUBMISSION_RETIRED"
    assert submission.get_run(run["id"])["state"] == "intent"


def test_m2_remains_available_for_long_audio():
    work = prepare_work(route=routes.ROUTE_M2, audio_ms=75000, with_plate=True)

    out = routes.preflight(work["id"], task=_task(work), route=routes.ROUTE_M2,
                           require_plate=True)

    assert not any(b["code"] == "OMNIHUMAN_AUDIO_TOO_LONG" for b in out["blockers"])
    assert out["ok"] is True, out["blockers"]


def test_omnihuman_audio_just_under_sixty_seconds_stays_allowed():
    work = prepare_work(audio_ms=59999)

    out = routes.preflight(work["id"], task=_task(work),
                           route=routes.ROUTE_OMNIHUMAN)
    assert not any(b["code"] == "OMNIHUMAN_AUDIO_TOO_LONG" for b in out["blockers"])
    listing = submission.current_call_list(work["id"], route=routes.ROUTE_OMNIHUMAN)
    assert listing["segment_count"] == 1
