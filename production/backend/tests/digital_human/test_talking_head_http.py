"""T05 HTTP 层契约测试（AC-UI-05）。

这一层不重测门禁逻辑（那些在 test_route_selection / test_talking_head 里已覆盖），
它要锁住的是**接口语义**：

1. 两类失败必须映射成不同状态码：请求不成立 → 422，当前不可执行 → 409。
   合并成一类，前端就只能提示"失败了"，用户无从判断该改输入还是等条件。
2. 对外视图必须如实：声明（declared）与实测（verified）分列、未实测就是空；
   "尚未审核"不得因为字段缺失而看起来像"通过"。
3. 摘要与清单由服务端推导：客户端自报的摘要不被采信。

全部在 mock 模式下运行：不触网、不产生费用、不提交 GPU 作业。
"""
from __future__ import annotations

import pytest

from dh_helpers import (
    allow_billable, authorize_cost, install_original, prepare_work, revoke_cost_notice,
)
from app import storage as st  # noqa: E402


@pytest.fixture()
def http():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _err(resp) -> str:
    body = resp.json()
    assert "error" in body, f"错误响应必须是 {{error: ...}} 结构：{body}"
    return body["error"]["code"]


def _ready(route: str = "OMNIHUMAN_1_5") -> dict:
    """搭出一个前置全通过的作品，并绑上已确认路线的任务。

    `prepare_work` 自己就会登记任务（含可追溯的路线选择），这里不再追加一次：
    同一作品挂两条任务会让"当前任务"的含义变模糊。
    """
    return prepare_work(route=route, with_plate=(route == "M2"))


# ---------- 1) 能力视图：声明与实测分列 ----------

def test_routes_endpoint_separates_declared_from_verified(http):
    r = http.get("/api/v1/talking-head/routes")
    assert r.status_code == 200
    j = r.json()
    omni = next(x for x in j["routes"] if x["route"] == "OMNIHUMAN_1_5")
    assert omni["declared"]["audio_max_seconds_exclusive"] == 60
    assert omni["declared"]["prompt_max_chars"] == 300
    assert omni["declared"]["resolutions"] == ["720p", "1080p"]
    assert omni["verified"] == {}, "什么都没实测过就不得有 verified 内容"
    assert omni["verified_state"] == "unverified"
    assert omni["open_questions"], "未决问题必须列出来，而不是留白"


def test_routes_endpoint_marks_m2_as_partially_verified(http):
    j = http.get("/api/v1/talking-head/routes").json()
    m2 = next(x for x in j["routes"] if x["route"] == "M2")
    assert m2["verified_state"] == "partially_verified"
    assert "非约 60 秒能力证据" in m2["verified"]["exp_001"]["scope"], \
        "历史 15 秒样片不得被当作约 60 秒能力证据"


def test_prices_and_scenarios_are_estimates_not_invoices(http):
    j = http.get("/api/v1/talking-head/routes").json()
    omni = next(p for p in j["price_snapshots"] if p["route"] == "OMNIHUMAN_1_5")
    assert omni["unit_fen"] == 100
    assert omni["unit"] == "fen_per_generated_second"
    assert omni["billing_verified"] is False, "未经账单核验就不得声称计费口径已确认"
    assert omni["price_source"] == "user_confirmed_rate", \
        "单价来自用户报价，不是服务方账单：来源必须写明"
    scen = {s["scenario"]: s for s in j["scenarios"]}
    assert scen["单条 60 秒"]["estimated_fen"] == 6000
    assert scen["每天 5 条 60 秒"]["per_day_fen"] == 30000
    assert scen["30 天每天 5 条"]["monthly_fen"] == 900000
    # 三项未核验的计费细节必须逐条写明：取整、最低消费、失败是否扣费。
    # 只写"待核验"而不写待核验的是什么，等于没法核。
    assert "取整" in omni["rounding_note"]
    assert "最低" in omni["minimum_note"]
    assert "失败" in omni["failure_billing_note"]
    assert "TTS" in omni["evidence"], "单价范围的边界（只含视频生成费）必须声明"


def test_routes_endpoint_carries_cost_notice_per_route(http):
    """路线视图必须带上"这条贵不贵、贵在哪"：没有它，界面只能靠文案猜。"""
    j = http.get("/api/v1/talking-head/routes").json()
    by = {x["route"]: x for x in j["routes"]}
    assert by["M2"]["cost_tier"] == "low"
    assert by["OMNIHUMAN_1_5"]["cost_tier"] == "high"
    assert by["M2"]["cost_notice_required"] is False
    assert by["OMNIHUMAN_1_5"]["cost_notice_required"] is True
    assert by["OMNIHUMAN_1_5"]["body_motion"]["supported"] is True
    assert by["M2"]["body_motion"]["supported"] is False
    # 不带作品时没有计费时长：金额必须是"未计算"，不能编一个数。
    assert by["OMNIHUMAN_1_5"]["cost_notice"]["estimated_fen"] is None


def test_cost_notice_has_amount_when_audio_ready(http):
    w = _ready()
    j = http.get(f"/api/v1/talking-head/routes?work_id={w['id']}").json()
    notice = next(x for x in j["routes"] if x["route"] == "OMNIHUMAN_1_5")["cost_notice"]
    assert notice["amount_known"] is True
    assert notice["estimated_fen"] > 0
    assert notice["estimated_fen"] == notice["billing_seconds"] * notice["unit_fen"]


def test_selecting_high_cost_route_requires_cost_notice_ack(http):
    """高成本路线不确认费用提示就选：409（等人确认），不是 422（改输入）。"""
    w = http.post("/api/v1/works", json={"topic": "RAG 科普"}).json()
    r = http.post(f"/api/v1/works/{w['id']}/route", json={"route": "OMNIHUMAN_1_5"})
    assert r.status_code == 409, r.text
    assert _err(r) == "COST_NOTICE_REQUIRED"
    assert r.json()["error"]["action"] == "acknowledge_cost_notice"
    assert r.json()["error"]["retryable"] is True
    # 没确认就不该留下"已选 OmniHuman"的事实
    assert st.get("works", w["id"])["route"] != "OMNIHUMAN_1_5"


def test_selecting_low_cost_route_needs_no_notice(http):
    w = http.post("/api/v1/works", json={"topic": "RAG 科普"}).json()
    r = http.post(f"/api/v1/works/{w['id']}/route", json={"route": "M2"})
    assert r.status_code == 200, r.text
    assert r.json()["cost_notice_acknowledged"] is False


def test_m2_has_no_unit_price(http):
    j = http.get("/api/v1/talking-head/routes").json()
    m2 = next(p for p in j["price_snapshots"] if p["route"] == "M2")
    assert m2["unit_fen"] == 0 and m2["unit"] == "no_unit_price", \
        "本地路线没有单价：实验观测到的卡时费用不得被当成单价"
    assert m2["price_source"] == "unverified"
    from app.digital_human import cost as dh_cost

    est = dh_cost.estimate("M2", duration_ms=60000)
    assert est["billable"] is False and est["estimated_fen"] == 0
    assert "GPU" in est["note"], "M2 不是零成本：卡时仍计入成本，只是不按生成秒计价"


# ---------- 2) 前置视图：返回阻断项而不是抛错 ----------

def test_preflight_on_fresh_work_reports_blockers_without_route_error(http):
    """未选路线的作品查预检，必须得到阻断项清单，而不是"这条路线不支持"。"""
    w = http.post("/api/v1/works", json={"topic": "RAG 科普"}).json()
    r = http.get(f"/api/v1/works/{w['id']}/talking-head/preflight")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is False
    codes = [b["code"] for b in j["blockers"]]
    assert "ROUTE_UNSUPPORTED" not in codes
    assert "FINAL_AUDIO_REQUIRED" in codes
    for b in j["blockers"]:
        assert b["kind"] in ("rejected", "blocked"), \
            "阻断项必须区分「改输入」（rejected）与「等条件」（blocked）"


def test_preflight_rejects_explicitly_unsupported_route(http):
    w = http.post("/api/v1/works", json={"topic": "RAG 科普"}).json()
    r = http.get(f"/api/v1/works/{w['id']}/talking-head/preflight?route=GROK_V1")
    assert r.status_code == 422
    assert _err(r) == "ROUTE_UNSUPPORTED"


def test_preflight_ok_when_everything_bound(http):
    w = _ready()
    j = http.get(f"/api/v1/works/{w['id']}/talking-head/preflight").json()
    assert j["ok"] is True and j["blockers"] == []
    assert j["audio_duration_ms"] == 9000
    assert j["motion_intent"]["state"] == "unspecified"


# ---------- 3) 提交：路线与预算的先后次序 ----------

def test_submit_without_task_is_blocked_not_rejected(http):
    """没有任务就没有路线授权：这是"当前不可执行"（409），不是"请求非法"（422）。"""
    w = prepare_work(with_task=False)
    r = http.post(f"/api/v1/works/{w['id']}/talking-head/submissions", json={})
    assert r.status_code == 409
    assert _err(r) == "TASK_REQUIRED"


def test_submit_rejects_request_carrying_neither_image_nor_audio():
    """纯文生视频不能冒充「固定人物图＋已确认音频」的口播主流程。"""
    from app.digital_human import routes as routes_mod

    with pytest.raises(routes_mod.TalkingHeadRejected) as e:
        routes_mod.reject_text_to_video({"prompt": "一只猫在跳舞", "text": "跳舞"})
    assert e.value.code == "TEXT_TO_VIDEO_NOT_SUPPORTED"


def test_submit_blocked_without_cost_confirmation(http):
    """报价与预算都还没确认时提交：这是「等条件」（409），不是「请求非法」（422）。"""
    w = _ready()
    r = http.post(f"/api/v1/works/{w['id']}/talking-head/submissions",
                  json={"prompt": "自然讲述", "dry_run": True})
    assert r.status_code == 409
    assert _err(r) == "COST_CONFIRMATION_REQUIRED"


def test_submit_blocked_when_cost_notice_not_acknowledged(http, monkeypatch):
    """绕过界面直接提交高成本路线：费用提示没确认，一样拦在 409。

    这条要证明的是门禁在服务端——前端弹窗只是呈现，不是约束。
    """
    w = _ready()
    allow_billable(monkeypatch)
    authorize_cost(w["id"], route="OMNIHUMAN_1_5", prompt="自然讲述")
    revoke_cost_notice(w["id"])
    r = http.post(f"/api/v1/works/{w['id']}/talking-head/submissions",
                  json={"prompt": "自然讲述", "dry_run": True})
    assert r.status_code == 409
    assert _err(r) == "COST_NOTICE_NOT_ACKNOWLEDGED"
    assert http.get(f"/api/v1/works/{w['id']}/talking-head/videos").json() == [], \
        "被拦下的提交不得留下任何产物"


def test_submit_blocked_when_budget_not_authorized(http):
    """已确认清单与金额，但预算仍是 0：报价确认不等于付费授权。"""
    w = _ready()
    # 摘要含 prompt 与分辨率：确认时的输入必须与提交时逐字一致，
    # 否则拦下来的会是"清单变了"，而不是我们要验证的"预算未授权"。
    authorize_cost(w["id"], route="OMNIHUMAN_1_5", prompt="自然讲述")
    r = http.post(f"/api/v1/works/{w['id']}/talking-head/submissions",
                  json={"prompt": "自然讲述", "dry_run": True})
    assert r.status_code == 409
    assert _err(r) == "BUDGET_NOT_AUTHORIZED"
    assert http.get(f"/api/v1/works/{w['id']}/talking-head/videos").json() == [], \
        "被预算拦下的提交不得留下任何产物"


def test_submit_dry_run_registers_no_video(http, monkeypatch):
    w = _ready()
    allow_billable(monkeypatch)
    authorize_cost(w["id"], route="OMNIHUMAN_1_5", prompt="自然讲述")
    r = http.post(f"/api/v1/works/{w['id']}/talking-head/submissions",
                  json={"prompt": "自然讲述", "dry_run": True})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["is_mock"] is True and j["submitted"] is False
    assert http.get(f"/api/v1/works/{w['id']}/talking-head/videos").json() == [], \
        "dry_run 不得登记任何视频资产"


def test_submit_rejects_changed_call_list(http, monkeypatch):
    """确认后改了表演描述：清单摘要变化必须被拦，金额相同也不例外。"""
    w = _ready()
    allow_billable(monkeypatch)
    authorize_cost(w["id"], route="OMNIHUMAN_1_5", prompt="自然讲述")
    r = http.post(f"/api/v1/works/{w['id']}/talking-head/submissions",
                  json={"prompt": "换一个完全不同的表演", "dry_run": True})
    assert r.status_code == 422
    assert _err(r) == "COST_LIST_CHANGED"


# ---------- 4) 视频与门禁视图 ----------

def test_videos_endpoint_flags_mock_as_hard_failure(http):
    w = _ready()
    from app.tts import audio as tts_audio

    audio = tts_audio.final_audio(w["id"])
    video = install_original(w["id"], audio_asset_id=audio["id"], is_mock=True)
    rows = http.get(f"/api/v1/works/{w['id']}/talking-head/videos").json()
    row = next(v for v in rows if v["id"] == video["id"])
    assert "VIDEO_IS_MOCK" in [f["code"] for f in row["hard_failures"]]


def test_gate_endpoint_does_not_render_unreviewed_as_pass(http):
    w = _ready()
    j = http.get(f"/api/v1/works/{w['id']}/talking-head/gate").json()
    assert j["original_confirmed"] is False
    assert j["gate"] is None, "没有已确认原片时 gate 必须是 null，而不是空对象或通过"
    assert "不把" in j["note"]


def test_video_gate_endpoint_keeps_gate_and_audio_track_separate(http):
    w = _ready()
    from app.tts import audio as tts_audio

    audio = tts_audio.final_audio(w["id"])
    video = install_original(w["id"], audio_asset_id=audio["id"])
    j = http.get(f"/api/v1/talking-head/videos/{video['id']}/gate").json()
    assert j["gate"]["outcome"] == "not_run", \
        "没人审过就是 not_run：报成 fail 会让人以为已经有人给出过否决结论"
    assert j["gate"]["review_state"] == "not_run"
    assert j["audio_track"]["outcome"] == "not_run"
    assert j["gate"]["ok"] is False, "not_run 同样不是通过"
    assert "分别" in j["note"]


def test_video_gate_distinguishes_reviewed_fail_from_not_run(http):
    """审了没过 → fail；这一条与 not_run 必须可区分。"""
    w = _ready()
    from app.digital_human import gates as dh_gates
    from app.tts import audio as tts_audio

    audio = tts_audio.final_audio(w["id"])
    video = install_original(w["id"], audio_asset_id=audio["id"])
    dims = {d: "pass" for d in dh_gates.DIMENSIONS}
    dims["lip_sync"] = "fail"
    dh_gates.record_video_review(
        video["id"], reviewer="user_1", outcome="fail", dimensions=dims,
        references={d: {"asset_id": f"ref_{d}", "start_ms": 0, "end_ms": 800}
                    for d in dh_gates.DIMENSIONS}, note="口型不同步")
    j = http.get(f"/api/v1/talking-head/videos/{video['id']}/gate").json()
    assert j["gate"]["outcome"] == "fail"
    assert j["gate"]["review_state"] == "fail"


def test_video_gate_endpoint_404s_for_unknown_video(http):
    r = http.get("/api/v1/talking-head/videos/video_nope/gate")
    assert r.status_code == 404


# ---------- 5) 人工审核端点：谁有权判通过 ----------

def _review_payload(**over) -> dict:
    dims = {d: "pass" for d in ("lip_sync", "identity", "body_motion", "expression")}
    refs = {d: {"asset_id": f"ref_{d}", "start_ms": 0, "end_ms": 1000} for d in dims}
    p = {"reviewer": "user_1", "outcome": "pass", "dimensions": dims, "references": refs}
    p.update(over)
    return p


def test_review_pass_by_non_user_is_rejected(http):
    w = _ready()
    from app.tts import audio as tts_audio

    video = install_original(w["id"], audio_asset_id=tts_audio.final_audio(w["id"])["id"])
    r = http.post(f"/api/v1/talking-head/videos/{video['id']}/review",
                  json=_review_payload(reviewer="agent"))
    assert r.status_code == 422
    assert _err(r) == "VIDEO_REVIEW_ACTOR_INVALID"


def test_review_pass_without_references_is_rejected(http):
    w = _ready()
    from app.tts import audio as tts_audio

    video = install_original(w["id"], audio_asset_id=tts_audio.final_audio(w["id"])["id"])
    r = http.post(f"/api/v1/talking-head/videos/{video['id']}/review",
                  json=_review_payload(references={}))
    assert r.status_code == 422
    assert _err(r) == "REVIEW_WITHOUT_EVIDENCE"


def test_review_rejects_waveform_correlation_as_lipsync_proof(http):
    w = _ready()
    from app.tts import audio as tts_audio

    video = install_original(w["id"], audio_asset_id=tts_audio.final_audio(w["id"])["id"])
    refs = _review_payload()["references"]
    refs["lip_sync"]["method"] = "waveform_correlation"
    r = http.post(f"/api/v1/talking-head/videos/{video['id']}/review",
                  json=_review_payload(references=refs))
    assert r.status_code == 422
    assert _err(r) == "LIPSYNC_PROOF_INVALID"


def test_audio_track_pass_requires_measured_offset(http):
    w = _ready()
    from app.tts import audio as tts_audio

    audio = tts_audio.final_audio(w["id"])
    video = install_original(w["id"], audio_asset_id=audio["id"])
    r = http.post(f"/api/v1/talking-head/videos/{video['id']}/audio-track-checks",
                  json={"audio_asset_id": audio["id"], "method": "decode_align",
                        "outcome": "pass", "evidence": "夹具"})
    assert r.status_code == 422
    assert _err(r) == "AUDIO_TRACK_OFFSET_MISSING", "不得默认 0ms"


def test_audio_track_offset_over_tolerance_is_rejected(http):
    w = _ready()
    from app.tts import audio as tts_audio

    audio = tts_audio.final_audio(w["id"])
    video = install_original(w["id"], audio_asset_id=audio["id"])
    r = http.post(f"/api/v1/talking-head/videos/{video['id']}/audio-track-checks",
                  json={"audio_asset_id": audio["id"], "method": "decode_align",
                        "outcome": "pass", "measured_offset_ms": 500, "evidence": "夹具"})
    assert r.status_code == 422
    assert _err(r) == "AUDIO_TRACK_OFFSET_EXCEEDED"


def test_plate_cannot_be_confirmed_as_final(http):
    w = _ready(route="M2")
    from app.digital_human import plates as dh_plates

    plate = dh_plates.approved_plate_for(w["id"])
    r = http.post(f"/api/v1/works/{w['id']}/talking-head/final-video",
                  json={"video_id": plate["id"], "actor_id": "user_1", "review_ack": True})
    assert r.status_code == 422
    assert _err(r) == "PLATE_NOT_FINAL"


def test_runs_http_view_has_polling_identity_without_internal_params(http):
    from app.digital_human import submission

    work = _ready(route="M2")
    run = submission.start_run(
        work["id"], task_id=work["current_task_id"], route="M2", provider="m2_worker",
        model="MuseTalk-1.5", capability="lipsync", request_digest="sha256:http-contract",
        idempotency_key="http-contract", provider_params={"private_test_token": "not-for-client"})
    submission._update_run(run["id"], state="waiting_provider", remote_job_id="remote_http_shape")

    response = http.get(f"/api/v1/works/{work['id']}/talking-head/submissions")
    assert response.status_code == 200, response.text
    row = next(item for item in response.json()["runs"] if item["id"] == run["id"])
    assert row["work_id"] == work["id"]
    assert row["task_id"] == work["current_task_id"]
    assert row["provider"] == "m2_worker"
    assert row["model"] == "MuseTalk-1.5"
    assert row["created_at"] == run["created_at"]
    assert row["state"] == "waiting_provider"
    assert row["remote_job_id"] == "remote_http_shape"
    assert "provider_params" not in row
    assert "private_test_token" not in response.text


def test_confirm_final_video_requires_review_ack(http):
    w = _ready()
    from app.tts import audio as tts_audio

    audio = tts_audio.final_audio(w["id"])
    video = install_original(w["id"], audio_asset_id=audio["id"])
    r = http.post(f"/api/v1/works/{w['id']}/talking-head/final-video",
                  json={"video_id": video["id"], "actor_id": "user_1"})
    assert r.status_code == 422
    assert _err(r) == "VIDEO_REVIEW_ACK_REQUIRED"


def test_confirm_final_video_freezes_and_binds_work(http):
    w = _ready()
    from app.tts import audio as tts_audio

    audio = tts_audio.final_audio(w["id"])
    video = install_original(w["id"], audio_asset_id=audio["id"])
    r = http.post(f"/api/v1/talking-head/videos/{video['id']}/audio-track-checks",
                  json={"audio_asset_id": audio["id"], "method": "decode_align",
                        "outcome": "pass", "measured_offset_ms": 10, "evidence": "夹具实测"})
    assert r.status_code == 200, r.text
    from dh_helpers import approve_original

    approve_original(video["id"], audio_asset_id=audio["id"], offset_ms=10)
    r = http.post(f"/api/v1/works/{w['id']}/talking-head/final-video",
                  json={"video_id": video["id"], "actor_id": "user_1", "review_ack": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["duration_ms"] == 9000
    g = http.get(f"/api/v1/works/{w['id']}/talking-head/gate").json()
    assert g["original_confirmed"] is True
    assert g["gate"]["outcome"] == "pass"


# ---------- 6) 队列与适配器状态 ----------

def test_queue_endpoint_reports_serialization(http):
    j = http.get("/api/v1/talking-head/queue").json()
    assert j["serialized"] is True
    assert set(j["counts"]) >= {"queued", "leased", "blocked"}
    assert "不占本地 GPU" in j["note"]


def test_adapters_endpoint_reports_credentials_absent_honestly(http, monkeypatch):
    """没凭据就必须报 not_configured，不能笼统写成「待验证」。"""
    from app.digital_human import providers as dh_pv

    monkeypatch.delenv(dh_pv.SECRET_ENV_AK, raising=False)
    monkeypatch.delenv(dh_pv.SECRET_ENV_SK, raising=False)
    monkeypatch.delenv(dh_pv.M2_URL_ENV, raising=False)
    j = http.get("/api/v1/talking-head/adapters").json()
    omni = j["omnihuman"]
    assert omni["configured"] is False
    assert omni["state"] == "not_configured", \
        "缺凭据要配置；与「配置了但没实测」是两件事，处理方式不同"
    assert omni["verified"] == {}, "没有实测证据就不得有 verified 内容"
    assert j["m2_worker"]["configured"] is False
    assert j["m2_worker"]["state"] == "not_configured"
    # 状态词汇必须与 T04 的 TTS 适配器一致，否则前端得为"同一件事"写两套渲染分支。
    from app.tts import provider as tts_provider

    tts_state = tts_provider.adapter_status()["state"]
    assert tts_state in ("not_configured", "unverified", "ready")
    for key in ("omnihuman", "m2_worker"):
        assert j[key]["state"] in ("not_configured", "unverified", "ready"), \
            f"{key} 用了 T04 之外的第三种状态词，前端会漏渲染"


# ---------- 7) 路线选择与任务创建的衔接 ----------

def _task_of(work_id: str) -> dict:
    """从持久化状态读任务：HTTP 建任务要求人物/稿件/音频齐全，
    这里要验的是"任务与选择记录的衔接"，不是整条前置链，所以直接读登记结果。"""
    from app import storage as st

    return st.get("tasks", st.get("works", work_id)["current_task_id"])


def test_route_confirmed_before_task_is_backfilled_whole(http):
    """先确认路线、再建任务：已确认的事实必须整体回填，不能只回填一半。

    只回填选择记录而漏掉确认标记，提交会被「路线尚未确认」拦下——
    用户明明做过这个动作，系统却只记住了一半。
    """
    w = http.post("/api/v1/works", json={"topic": "RAG 科普"}).json()
    # 高成本路线：带上费用提示确认才是一次合规的路线选择。
    r = http.post(f"/api/v1/works/{w['id']}/route",
                  json={"route": "OMNIHUMAN_1_5", "cost_notice_ack": True})
    assert r.status_code == 200, r.text
    msg = r.json()["message"]
    assert "费用已确认" not in msg, \
        "路线确认不等于本次调用清单已确认；回报里不得声称费用已确认"
    assert "调用清单" in msg, "必须说清下一步还差什么"
    assert r.json()["cost_notice_acknowledged"] is True

    http.post("/api/v1/tasks", json={"work_id": w["id"]})
    t = _task_of(w["id"])
    assert t["route_selection_id"], "人工决定必须可追溯"
    assert t["route_confirmed"] is True, "用户已确认过路线，建任务不得把它忘掉"
    assert t["route"] == "OMNIHUMAN_1_5"


def test_route_confirmed_endpoint_records_traceable_selection(http):
    """先建任务、后确认路线：确认动作必须落到那条任务上，并冻结选择记录。"""
    w = http.post("/api/v1/works", json={"topic": "RAG 科普"}).json()
    http.post("/api/v1/tasks", json={"work_id": w["id"]})
    before = _task_of(w["id"])
    assert not before.get("route_selection_id"), "还没确认时不得凭空有选择记录"

    http.post(f"/api/v1/works/{w['id']}/route",
              json={"route": "OMNIHUMAN_1_5", "cost_notice_ack": True})
    after = _task_of(w["id"])
    assert after["route_selection_id"], "确认路线必须冻结一条选择记录，否则提交时应被拒"
    assert after["route_confirmed"] is True
    assert after["route"] == "OMNIHUMAN_1_5"


def test_task_creation_response_shape_is_order_independent(http):
    """响应形状必须与「先确认路线还是先建任务」无关。

    同一类前置（未确认路线 / 未绑定人物）必须给出同一种结构：
    否则客户端要为两个只差顺序的流程写两套解析，而顺序与用户无关。
    """
    w1 = http.post("/api/v1/works", json={"topic": "顺序 A"}).json()
    r1 = http.post("/api/v1/tasks", json={"work_id": w1["id"]})
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["state"] == "blocked" and b1["block_reason"] == "COST_CONFIRMATION_REQUIRED"
    assert b1["last_error"]["action"] == "confirm_cost"

    w2 = http.post("/api/v1/works", json={"topic": "顺序 B"}).json()
    http.post(f"/api/v1/works/{w2['id']}/route",
              json={"route": "OMNIHUMAN_1_5", "cost_notice_ack": True})
    r2 = http.post("/api/v1/tasks", json={"work_id": w2["id"]})
    assert r2.status_code == 200, "缺少人物同样是前置不满足，不得抛 422"
    b2 = r2.json()
    assert b2["state"] == "blocked" and b2["block_reason"] == "SELECTION_REQUIRED"
    # 建作品时已默认绑定预设人物，因此这里第一个未满足的是稿件。
    # 关键不在于报的是哪一条，而在于它给出的是**可直接渲染的阻断结构**。
    assert b2["last_error"]["action"] == "select_script"
    assert b2["last_error"]["message"]
    assert b2["route_selection_id"], "已确认的路线决定仍要冻结在任务上"
    assert b2["route_confirmed"] is True


# ---------- M2 远端 worker 的通道接线 ----------

def test_m2_transport_is_none_until_worker_is_configured(monkeypatch):
    """没配 worker 地址时必须返回 None，让既有的 blocked 路径生效。

    若返回一个"看起来能用"的假通道，界面会显示提交成功，
    而实际上什么都没执行——这比直接报 409 糟得多。
    """
    from app import main as backend_main

    monkeypatch.delenv("AVATAR_STUDIO_M2_WORKER_URL", raising=False)
    assert backend_main._m2_transport() is None


def test_run_transport_routes_by_provider(http, monkeypatch):
    """M2 的 run 走 worker 通道，其余走火山网关。

    统一给 urllib_transport 会让 M2 请求因缺 url 字段报 KeyError，
    并被当成服务端 500——真正的原因（通道没接）就此消失。
    """
    from app import main as backend_main
    from app.digital_human import submission as dh_sub

    w = http.post("/api/v1/works", json={"topic": "通道分流"}).json()
    m2_run = dh_sub.start_run(w["id"], route="M2", provider="m2_worker",
                              model="MuseTalk-1.5", capability="lipsync",
                              request_digest="d1", idempotency_key="k1")
    om_run = dh_sub.start_run(w["id"], route="OMNIHUMAN_1_5", provider="volcengine",
                              model="omnihuman-1.5", capability="lipsync",
                              request_digest="d2", idempotency_key="k2")

    monkeypatch.delenv("AVATAR_STUDIO_M2_WORKER_URL", raising=False)
    assert backend_main._run_transport(m2_run["id"]) is None, "未接线时不得给出可用通道"
    assert backend_main._run_transport(om_run["id"]) is not None

    monkeypatch.setenv("AVATAR_STUDIO_M2_WORKER_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("AVATAR_STUDIO_M2_WORKER_TOKEN", "t")
    assert backend_main._run_transport(m2_run["id"]) is not None, "配好地址后 M2 才有通道"


# ---------- 路线视图必须区分"已选"与"默认指向" ----------

def test_routes_view_says_unconfirmed_when_no_selection_was_made(http):
    """作品 route 字段带默认值 M2，那个默认值**不是**用户的选择。

    界面要据此显示"尚未选择"；如果这里回 True，界面就会把没选过的作品
    显示成"已选 M2"，用户以为可以直接提交（后端仍会以 ROUTE_NOT_CONFIRMED 拦下）。
    """
    w = prepare_work(route="M2", with_task=False)
    j = http.get(f"/api/v1/talking-head/routes?work_id={w['id']}").json()
    assert j["selected_route"] == "M2", "作品字段如实返回，不因为它带默认值就隐藏"
    assert j["route_confirmed"] is False, "没有可追溯的人工选择就不得报已确认"


def test_routes_view_says_confirmed_after_user_selected_route(http):
    w = prepare_work(route="M2", with_task=False)
    r = http.post(f"/api/v1/works/{w['id']}/route", json={"route": "M2"})
    assert r.status_code == 200, r.text
    j = http.get(f"/api/v1/talking-head/routes?work_id={w['id']}").json()
    assert j["route_confirmed"] is True
    assert j["selected_route"] == "M2"


# ---------- 分段进度 / 续发入口（界面的"继续生成剩余分段"靠这两个）----------

def test_driver_progress_endpoint_is_read_only(http):
    """进度接口只读：不能因为"看一眼"就把推进器启起来。"""
    work = _ready()
    r = http.get(f"/api/v1/works/{work['id']}/talking-head/driver")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "IDLE" and body["resumable"] is False
    assert body["total"] == 0 and body["pending"] == 0


def test_driver_resume_is_a_noop_when_there_is_nothing_to_continue(http):
    """没有运行记录时续发是空操作：不启动线程，也不假装成功。"""
    work = _ready()
    r = http.post(f"/api/v1/works/{work['id']}/talking-head/driver/resume", json={})
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "OMNIHUMAN_SEGMENT_SUBMISSION_RETIRED"


def test_driver_endpoints_reject_unknown_work(http):
    assert http.get("/api/v1/works/work_missing/talking-head/driver").status_code == 404
    assert http.post("/api/v1/works/work_missing/talking-head/driver/resume",
                     json={}).status_code == 404
