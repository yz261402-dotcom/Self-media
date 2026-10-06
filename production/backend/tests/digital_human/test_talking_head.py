"""数字人口播主流程：OmniHuman 提交编排、假成功拦截、原片门禁、失败保留。

这一组用例围绕一个判断：**"看起来成了"和"真的成了"之间的每一道缝都要堵上。**

- 提交成功只说明受理。HTTP 200 没有 task_id 既不是成功也不是失败，是未知；
- 远端报 `done` 但没有 video_url、下载回来是 HTML 错误页/截断文件/无视频轨，
  都不能登记成原片；
- 原片"全部通过"但没有任何引用、或拿波形相关当口型证据，都不算通过；
- 失败与未知一律保留原记录，不自动换路线、不盲目重提；
- 约 60 秒只能按同一音轨的句子边界分段，禁止加速与截字。
"""
from __future__ import annotations

import hashlib

import pytest

from dh_helpers import (HASH_A, allow_billable, approve_original, binary_response,
                        html_error_page, install_original, install_plate, install_variant,
                        make_mp4, make_transport, prepare_work, volc_query, volc_submit_no_task_id,
                        volc_submit_ok)
from app import storage as st
from app.digital_human import gates as dh_gates
from app.digital_human import providers as pv
from app.digital_human import routes as dh_routes
from app.digital_human import segments as sg
from app.digital_human import submission as dh_sub
from app.digital_human import videos as vd
from app.digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected
from app.tts import audio as tts_audio


def _tasks(work):
    return st.get("tasks", work["current_task_id"])


def _creds(monkeypatch):
    """放置成对的假凭据。签名是纯函数，所以拿确定性的 ak/sk 就能验证请求头。"""
    monkeypatch.setenv(pv.SECRET_ENV_AK, "AKTEST0001")
    monkeypatch.setenv(pv.SECRET_ENV_SK, "SKTESTSECRET")
    return "AKTEST0001"


def _final_audio(work):
    return tts_audio.final_audio(work["id"])


def _segment_plan(work):
    """让分段计划先落地：调用清单与段数必须来自同一份推导。"""
    return sg.latest_plan(work["id"]) or sg.create_plan(work["id"])


def _stub_segment_media(monkeypatch, *, audio_url="https://cdn.example.com/final.wav"):
    """分段场景的媒体替身：跳过真实 TOS 上传与 ffmpeg 切片。

    但**保留"每段必须携带它自己那一段的音频"这条约束**：切片按时间窗产出
    确定性字节，于是"不同段拿到不同 audio_url"可以被断言；一旦有人图省事
    把整轨 URL 复用给每一段，用例立刻失败——那正是 2026-09-28 那次
    "提交成功、查询永远 50215"的成因。
    """
    from pathlib import Path as _Path

    from app.digital_human import omni_media

    objects: dict = {}

    class Publisher:
        def exists(self, key):
            return key in objects

        def ensure_bucket(self):
            pass

        def publish_file(self, path, key=None, content_type=None):
            objects[key] = _Path(path).read_bytes()
            return self.url_for(key)

        def url_for(self, key):
            return "https://bucket.example.com/" + key

        def verify_public_file(self, url, expected_hash):
            data = objects[url.split(".com/", 1)[1]]
            if "sha256:" + hashlib.sha256(data).hexdigest() != expected_hash:
                raise ValueError("wrong bytes")

    monkeypatch.setattr(omni_media, "prepare_media",
                        lambda source, audio, *, ak, sk: {
                            "image_url": "https://cdn.example.com/hero.png",
                            "audio_url": audio_url,
                            "audio_path": _Path("stub-final.wav")})
    monkeypatch.setattr(omni_media, "_slice_audio",
                        lambda src, start_ms, end_ms, dest: dest.write_bytes(
                            f"seg:{start_ms}-{end_ms}".encode()))
    monkeypatch.setattr(omni_media, "_publisher", lambda ak, sk: Publisher())
    return objects, audio_url


# ---------- 提交编排 ----------

def test_dry_run_makes_no_request_and_registers_nothing(work, monkeypatch):
    """dry_run 必须真的不发请求：有占位产出但零外部调用才算安全。"""
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    before = list(vd.list_videos(work["id"]))
    transport, calls = make_transport(responses=[volc_submit_ok()], default=volc_submit_ok())

    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=True)
    assert out["submitted"] is False and out["is_mock"] is True
    assert calls == [], "dry_run 不得产生任何外部请求"
    assert out["runs"][0]["is_mock"] is True
    assert list(vd.list_videos(work["id"])) == before


def _segmented_routing_transport(remote: dict):
    """分段场景的通道替身：查询走 remote["status"]，提交逐个发 task_id。"""
    submits = []

    def transport(req):
        if req.get("method") == "GET":
            return binary_response(make_mp4(15000))
        if req.get("query", {}).get("Action") == pv.VOLC_ACTION_QUERY:
            return volc_query(remote["status"])
        submits.append(req)
        return volc_submit_ok(f"volc-seg-{len(submits)}")

    return transport, submits


def test_driver_groups_runs_by_attempt_not_just_digest(segmented_work, monkeypatch):
    """同一摘要下的新旧尝试必须分开：上一轮遗留的 unknown 不得拦停新一次尝试。

    重做会复用同一个摘要（输入没变），但换一组幂等键（`:retry:<新确认书>`）。
    若按摘要分组，旧轮次的 unknown 段会立刻把新轮次判成"需对账"而停住——
    而新轮次是用户重新确认费用后显式授权的，不该被它拦住。
    """
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from app.digital_human import omni_driver

    work_id = segmented_work["id"]
    digest = "sha256:" + "a" * 64

    def _run(suffix: str, created_at: str) -> dict:
        run = dh_sub.start_run(
            work_id, route=dh_routes.ROUTE_OMNIHUMAN, provider="volcengine",
            model="omnihuman-1.5", capability=dh_routes.CAP_AVATAR_VIDEO_CLOUD,
            request_digest=digest,
            idempotency_key=f"oh:{work_id}:{digest}:oh_seg_1{suffix}",
            provider_params={"segment_index": 0, "retry_suffix": suffix})
        run["created_at"] = created_at
        st.put(dh_sub.RUN_TABLE, run["id"], run, {"work_id": work_id})
        return run

    old = _run("", "2026-09-28T00:00:00+00:00")
    dh_sub.mark_run_unknown(old["id"], "上一轮结果未知，先对账")
    new = _run(":retry:cost_new", "2026-09-28T01:00:00+00:00")
    dh_sub._update_run(new["id"], state="waiting_provider", remote_job_id="volc-new")

    group = omni_driver._active_group(work_id)
    assert group["attempt"].endswith(":retry:cost_new")
    assert [r["id"] for r in group["runs"]] == [new["id"]], "旧轮次的运行不得并入本轮"


# ---------- 分段进度与续发（界面据此决定要不要显示"继续生成剩余分段"）----------

def test_driver_progress_is_done_only_when_every_segment_succeeded(segmented_work, monkeypatch):
    """少一段都不算 DONE —— 进度不能把"大部分完成"报成完成。"""
    _creds(monkeypatch)
    from app.digital_human import omni_driver

    work_id = segmented_work["id"]
    total = len(_segment_plan(segmented_work)["segments"])
    digest = "sha256:" + "b" * 64
    for i in range(total):
        run = dh_sub.start_run(
            work_id, route=dh_routes.ROUTE_OMNIHUMAN, provider="volcengine",
            model="omnihuman-1.5", capability=dh_routes.CAP_AVATAR_VIDEO_CLOUD,
            request_digest=digest, idempotency_key=f"oh:{work_id}:{digest}:oh_seg_{i + 1}",
            provider_params={"segment_index": i, "retry_suffix": ""})
        dh_sub._update_run(run["id"], state="succeeded", remote_job_id=f"volc-{i}")
        snap = omni_driver.progress(work_id)
        assert snap["succeeded"] == i + 1
        expected = "DONE" if i + 1 == total else "STOPPED"
        assert snap["state"] == expected, f"第 {i + 1}/{total} 段不该报成 {snap['state']}"
        if i + 1 < total:
            assert snap["resumable"] is False
            assert snap["stop_reason"] == "SEGMENT_SUBMISSION_RETIRED"
    assert snap["resumable"] is False


def test_real_submit_signs_request_with_declared_action(work, monkeypatch):
    """请求体与签名是离线可验证的：不能等到花了钱才发现 Action 写错。"""
    ak = _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, calls = make_transport(default=volc_submit_ok("volc-1"))
    dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                            dry_run=False, resolution="1080p")

    req = calls[0]
    assert req["method"] == "POST"
    assert req["query"] == {"Action": pv.VOLC_ACTION_SUBMIT, "Version": pv.VOLC_API_VERSION}
    assert req["json"]["req_key"] == pv.OMNIHUMAN_REQ_KEY
    assert req["json"]["image_url"].startswith("http"), "供应商只能读 URL"
    assert req["json"]["audio_url"].startswith("http")
    # 官方契约：output_resolution 是 int（720/1080），不是 "1080p" 字符串。
    # 传字符串会让云端预处理报 invalid output_resolution（真实事故，已有回归钉子）。
    assert req["json"]["output_resolution"] == 1080
    assert isinstance(req["json"]["output_resolution"], int)
    auth = req["headers"]["Authorization"]
    assert auth.startswith("HMAC-SHA256 Credential=") and ak in auth
    assert "SignedHeaders=content-type;host;x-content-sha256;x-date" in auth


def test_submit_without_credentials_is_blocked_before_any_request(work, monkeypatch):
    monkeypatch.delenv(pv.SECRET_ENV_AK, raising=False)
    monkeypatch.delenv(pv.SECRET_ENV_SK, raising=False)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, calls = make_transport(default=volc_submit_ok())
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                dry_run=False)
    assert e.value.code == "OMNIHUMAN_CREDENTIALS_MISSING"
    assert calls == []


def test_missing_local_media_blocks_before_supplier_submission(work, monkeypatch):
    """本地媒体可以自动上传，但未落盘的路径不能伪造成供应商 URL。"""
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    audio = _final_audio(work)
    audio["storage_uri"] = f"audio/{work['id']}/final.wav"
    st.put("audios", audio["id"], audio, {"work_id": work["id"]})

    transport, calls = make_transport(default=volc_submit_ok())
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                dry_run=False)
    assert e.value.code == "OMNIHUMAN_MEDIA_PATH_INVALID"
    assert calls == []


def test_audio_at_limit_cannot_be_submitted_as_single_call(segmented_work, monkeypatch):
    """60 秒整已经"不小于"上限：单次调用不成立，必须走分段。"""
    with pytest.raises(TalkingHeadRejected) as e:
        pv.assert_omnihuman_input(audio_duration_ms=60000)
    assert e.value.code == "OMNIHUMAN_AUDIO_TOO_LONG"
    assert "禁止加速或截字" in e.value.message

    ok = pv.assert_omnihuman_input(audio_duration_ms=59999)
    assert ok["recommended_seconds_exceeded"] is True, "超过 15 秒建议值要如实标注"


def test_prompt_and_resolution_outside_declared_limits_are_rejected():
    with pytest.raises(TalkingHeadRejected) as e:
        pv.assert_omnihuman_input(audio_duration_ms=9000, prompt="表" * 301)
    assert e.value.code == "OMNIHUMAN_PROMPT_TOO_LONG"

    with pytest.raises(TalkingHeadRejected) as e:
        pv.assert_omnihuman_input(audio_duration_ms=9000, resolution="4k")
    assert e.value.code == "OMNIHUMAN_RESOLUTION_UNSUPPORTED"


# ---------- 假成功拦截 ----------

def test_submit_200_without_task_id_is_unknown_not_success(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, _ = make_transport(default=volc_submit_no_task_id())
    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=False)
    assert out["submitted"] is False
    run = out["runs"][0]
    assert run["state"] == "unknown"
    assert run["last_error"]["code"] == "RESULT_UNKNOWN"


def test_poll_transport_failure_keeps_run_state(work, monkeypatch):
    """查询时通道抖动不得把在飞的运行改成 unknown。

    2026-09-28 真实踩到：一次 TLS 抖动让查询"无响应"，运行被记成 unknown，
    而远端其实一直在正常 generating。一次网络抖动不该变成一次人工对账，
    更不该因此挡住后续重提——"没问到"和"问到了『不知道』"必须分开。
    """
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, _ = make_transport(default=volc_submit_ok("volc-a"))
    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=False)
    run_id = out["runs"][0]["id"]
    assert dh_sub.get_run(run_id)["state"] == "waiting_provider"

    def flaky(_req):
        return {"status_code": None, "error": "network_error",
                "message": "URLError: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]>"}

    polled = dh_sub.poll_run(run_id, transport=flaky)
    assert polled["transport_unavailable"] is True
    assert polled["state"] == "waiting_provider"
    assert dh_sub.get_run(run_id)["state"] == "waiting_provider", \
        "网络层失败不得降级运行状态"


def test_auth_failure_is_blocked_and_not_retried(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, calls = make_transport(default={"status_code": 401, "json": {}})
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                dry_run=False)
    assert e.value.code == "OMNIHUMAN_AUTH_FAILED"
    assert len(calls) == 1, "鉴权失败不得重试计费动作"


def test_quota_exhausted_is_blocked_without_switching_model(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, calls = make_transport(default={"status_code": 429, "json": {}})
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                dry_run=False)
    assert e.value.code == "OMNIHUMAN_QUOTA_EXHAUSTED"
    assert len(calls) == 1


def test_gateway_error_on_submit_is_unknown_not_failure(work, monkeypatch):
    """502/503/504 说明请求可能已经到达：既不能当成功，也不能当"没提交过"。"""
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, _ = make_transport(default={"status_code": 503, "json": {}})
    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=False)
    assert out["runs"][0]["state"] == "unknown"


def test_query_done_without_video_url_is_failure(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, calls = make_transport(responses=[
        volc_submit_ok("volc-1"), volc_query("done", video_url=None)])
    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=False)
    polled = dh_sub.poll_run(out["runs"][0]["id"], transport=transport)
    assert polled["state"] == "failed"
    assert "没有 video_url" in polled["run"]["last_error"]["message"]
    assert vd.list_videos(work["id"]) == [], "文件缺失不得登记任何原片"
    assert len(calls) == 2, "失败后不再尝试下载"


def test_query_status_not_found_stays_unknown(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, _ = make_transport(responses=[volc_submit_ok("volc-1"),
                                             volc_query("not_found", video_url=None)])
    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=False)
    polled = dh_sub.poll_run(out["runs"][0]["id"], transport=transport)
    assert polled["state"] == "unknown"
    assert "不构成确定结论" in polled["run"]["last_error"]["message"]


def test_query_running_keeps_waiting_without_download(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, calls = make_transport(responses=[
        volc_submit_ok("volc-1"),
        {"status_code": 200, "json": {"ResponseMetadata": {}, "data": {"status": "generating"}}}])
    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=False)
    polled = dh_sub.poll_run(out["runs"][0]["id"], transport=transport)
    assert polled["state"] == "waiting_provider"
    assert len(calls) == 2, "还在生成时不得下载"
    assert vd.list_videos(work["id"]) == []


def test_poll_without_remote_id_becomes_unknown(work, monkeypatch):
    _creds(monkeypatch)
    run = dh_sub.start_run(work["id"], route="OMNIHUMAN_1_5", provider="volcengine",
                           model="omnihuman-1.5", capability="avatar_video_cloud",
                           request_digest="sha256:x", idempotency_key="poll:1")
    polled = dh_sub.poll_run(run["id"], transport=lambda r: volc_submit_ok())
    assert polled["state"] == "unknown"
    assert "无远端 ID 不得改判" in polled["note"]


def test_mock_run_is_not_pollable(work):
    run = dh_sub.start_run(work["id"], route="OMNIHUMAN_1_5", provider="volcengine",
                           model="omnihuman-1.5", capability="avatar_video_cloud",
                           request_digest="sha256:m", idempotency_key="poll:mock",
                           is_mock=True)
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_sub.poll_run(run["id"], transport=lambda r: {})
    assert e.value.code == "MOCK_RUN_NOT_POLLABLE"


@pytest.mark.parametrize("payload,code", [
    (html_error_page(), "VIDEO_IS_HTML"),
    (b"", "VIDEO_EMPTY"),
    (b"\x89PNG\r\n\x1a\n" + b"\0" * 64, "VIDEO_CONTAINER_MISMATCH"),
])
def test_downloaded_non_video_payloads_are_rejected(payload, code):
    with pytest.raises(TalkingHeadRejected) as e:
        pv.probe_media(payload)
    assert e.value.code == code


@pytest.mark.parametrize("kwargs,keyword", [
    ({"truncate_bytes": 40}, "截断"),
    ({"video": False}, "没有视频轨"),
    ({"with_moov": False}, "moov"),
])
def test_structurally_broken_mp4_is_unusable(kwargs, keyword):
    probe = pv.probe_media(make_mp4(9000, **kwargs))
    with pytest.raises(TalkingHeadRejected) as e:
        pv.assert_usable_video(probe)
    assert e.value.code == "VIDEO_UNUSABLE"
    assert keyword in e.value.message


def test_expired_result_url_is_blocked_not_refetched_as_new_task(work, monkeypatch):
    """403/404 只说明地址失效：按原 task_id 重新取地址，不是重新提交任务。"""
    with pytest.raises(TalkingHeadBlocked) as e:
        pv.download_and_verify("https://cdn.example.com/v.mp4",
                              lambda r: {"status_code": 403, "body": b""})
    assert e.value.code == "VIDEO_URL_EXPIRED"
    assert "重新获取而不是重提任务" in e.value.message


def test_poll_success_registers_original_bound_to_audio_and_identity(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, calls = make_transport(responses=[
        volc_submit_ok("volc-1"), volc_query("done"), binary_response(make_mp4(9000))])
    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=False)
    polled = dh_sub.poll_run(out["runs"][0]["id"], transport=transport)
    assert polled["state"] == "succeeded"
    video = polled["video"]
    audio = _final_audio(work)
    assert video["kind"] == vd.KIND_ORIGINAL
    assert video["audio_asset_id"] == audio["id"], "原片必须绑定驱动它的那条音频"
    assert video["identity_id"] == work["identity_id"], "原片必须绑定作品选定人物"
    assert video["file_hash"].startswith("sha256:")
    assert video["has_video_track"] is True
    assert video["duration_ms"] == 9000
    assert video["global_offset_ms"] == 0
    assert polled["run"]["result_asset_ids"] == [video["id"]]
    assert len(calls) == 3


def _approved_original(work, **kw):
    audio = _final_audio(work)
    video = install_original(work["id"], audio_asset_id=audio["id"], **kw)
    approve_original(video["id"], audio_asset_id=audio["id"])
    return video


def test_original_gate_passes_only_with_four_dimensions_and_audio_track(work):
    video = _approved_original(work)
    gate = dh_gates.video_gate(video["id"])
    assert gate["ok"] is True and gate["outcome"] == "pass"
    assert set(gate["dimensions"]) == set(dh_gates.DIMENSIONS)
    assert gate["audio_track"]["outcome"] == "pass"


def test_original_gate_lists_missing_review_instead_of_passing(work):
    video = install_original(work["id"], audio_asset_id=_final_audio(work)["id"])
    gate = dh_gates.video_gate(video["id"])
    assert gate["ok"] is False
    assert any("缺少人工原片审核" in r for r in gate["reasons"])
    assert any("未核对音轨保持" in r for r in gate["reasons"])


def test_audio_track_pass_does_not_substitute_for_review(work):
    """音轨通过只解决"是不是同一条声音"，不能顶替口型与身份。"""
    audio = _final_audio(work)
    video = install_original(work["id"], audio_asset_id=audio["id"])
    dh_gates.record_audio_track_check(video["id"], audio_asset_id=audio["id"],
                                     method="decode_align", outcome="pass",
                                     measured_offset_ms=0, evidence="夹具")
    gate = dh_gates.video_gate(video["id"])
    assert gate["ok"] is False
    assert any("缺少人工原片审核" in r for r in gate["reasons"])


def test_plate_is_never_accepted_as_final_original(work, monkeypatch):
    allow_billable(monkeypatch)
    work_m2 = prepare_work(route="M2", with_plate=True)
    plate = dh_sub and [v for v in vd.list_videos(work_m2["id"], vd.KIND_PLATE)][0]
    audio = _final_audio(work_m2)
    review = dh_gates.record_video_review(
        plate["id"], reviewer="user_1", outcome="pass",
        dimensions={d: "pass" for d in dh_gates.DIMENSIONS},
        references={d: {"asset_id": f"r_{d}", "start_ms": 0, "end_ms": 500}
                    for d in dh_gates.DIMENSIONS})
    assert review["outcome"] == "pass"
    gate = dh_gates.video_gate(plate["id"])
    assert gate["ok"] is False
    assert any("动态底片不是交付原片" in r for r in gate["reasons"])
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.confirm_final_video(work_m2["id"], plate["id"], actor_id="user_1",
                                     review_ack=True)
    assert e.value.code == "PLATE_NOT_FINAL"


def test_mock_video_never_passes_gate(work):
    audio = _final_audio(work)
    video = install_original(work["id"], audio_asset_id=audio["id"], is_mock=True)
    ok, reasons = vd.is_auditable(video)
    assert ok is False and any("占位产物" in r for r in reasons)
    gate = dh_gates.video_gate(video["id"])
    assert gate["ok"] is False and any("VIDEO_IS_MOCK" in r or "占位产物" in r
                                      for r in gate["reasons"])


def test_review_pass_requires_user_actor(work):
    video = install_original(work["id"], audio_asset_id=_final_audio(work)["id"])
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.record_video_review(
            video["id"], reviewer="agent_1", outcome="pass",
            dimensions={d: "pass" for d in dh_gates.DIMENSIONS},
            references={d: {"asset_id": "x"} for d in dh_gates.DIMENSIONS})
    assert e.value.code == "VIDEO_REVIEW_ACTOR_INVALID"
    assert "未接入视频理解能力" in e.value.message


def test_review_requires_every_dimension_to_pass(work):
    video = install_original(work["id"], audio_asset_id=_final_audio(work)["id"])
    dims = {d: "pass" for d in dh_gates.DIMENSIONS}
    dims["body_motion"] = "review"
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.record_video_review(
            video["id"], reviewer="user_1", outcome="pass", dimensions=dims,
            references={d: {"asset_id": "x"} for d in dh_gates.DIMENSIONS})
    assert e.value.code == "VIDEO_REVIEW_INCOMPLETE"
    assert "不能互相顶替" in e.value.message


def test_review_pass_requires_reference_for_each_dimension(work):
    video = install_original(work["id"], audio_asset_id=_final_audio(work)["id"])
    refs = {d: {"asset_id": "x"} for d in dh_gates.DIMENSIONS}
    refs["expression"] = {}
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.record_video_review(
            video["id"], reviewer="user_1", outcome="pass",
            dimensions={d: "pass" for d in dh_gates.DIMENSIONS}, references=refs)
    assert e.value.code == "REVIEW_WITHOUT_EVIDENCE"
    assert "无引用的通过等于没有通过" in e.value.message


def test_waveform_correlation_cannot_prove_lip_sync(work):
    video = install_original(work["id"], audio_asset_id=_final_audio(work)["id"])
    refs = {d: {"asset_id": "x"} for d in dh_gates.DIMENSIONS}
    refs["lip_sync"] = {"asset_id": "x", "method": "waveform_correlation",
                        "pearson": 0.99996}
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.record_video_review(
            video["id"], reviewer="user_1", outcome="pass",
            dimensions={d: "pass" for d in dh_gates.DIMENSIONS}, references=refs)
    assert e.value.code == "LIPSYNC_PROOF_INVALID"


def test_review_dimension_name_must_be_known(work):
    video = install_original(work["id"], audio_asset_id=_final_audio(work)["id"])
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.record_video_review(video["id"], reviewer="user_1", outcome="pass",
                                     dimensions={"lip_sync": "pass", "vibe": "pass"},
                                     references={"lip_sync": {"asset_id": "x"}})
    assert e.value.code == "VIDEO_REVIEW_DIMENSION_UNKNOWN"


def test_review_invalidated_when_file_replaced(work):
    video = _approved_original(work)
    assert dh_gates.video_gate(video["id"])["ok"] is True
    row = vd.get_video(video["id"])
    row["file_hash"] = "sha256:" + "7" * 64
    st.put(vd.VIDEO_TABLE, row["id"], row, {"work_id": work["id"]})
    gate = dh_gates.video_gate(video["id"])
    assert gate["ok"] is False
    assert any("审核已失效" in r for r in gate["reasons"])


def test_audio_track_pass_requires_measured_offset(work):
    audio = _final_audio(work)
    video = install_original(work["id"], audio_asset_id=audio["id"])
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.record_audio_track_check(video["id"], audio_asset_id=audio["id"],
                                         method="decode_align", outcome="pass",
                                         evidence="夹具")
    assert e.value.code == "AUDIO_TRACK_OFFSET_MISSING"
    assert "不得默认 0ms" in e.value.message


def test_audio_track_offset_beyond_tolerance_is_rejected(work):
    audio = _final_audio(work)
    video = install_original(work["id"], audio_asset_id=audio["id"])
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.record_audio_track_check(video["id"], audio_asset_id=audio["id"],
                                         method="decode_align", outcome="pass",
                                         measured_offset_ms=51, evidence="夹具")
    assert e.value.code == "AUDIO_TRACK_OFFSET_EXCEEDED"
    assert "不得靠改字幕掩盖" in e.value.message


def test_audio_track_must_bind_specific_audio(work):
    video = install_original(work["id"], audio_asset_id=_final_audio(work)["id"])
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.record_audio_track_check(video["id"], audio_asset_id="",
                                         method="decode_align", outcome="pass",
                                         measured_offset_ms=0, evidence="夹具")
    assert e.value.code == "AUDIO_TRACK_UNBOUND"


def test_audio_track_check_against_other_audio_does_not_pass(work):
    """核对的是别条音频 → 这条结论对当前文件不再成立，必须作废而不是继续算 pass。"""
    audio = _final_audio(work)
    other = tts_audio.register_audio(
        work_id=work["id"], kind="full", voice_id=work["voice_asset_id"],
        duration_ms=9000, file_hash=HASH_A, storage_uri="https://cdn.example.com/other.wav",
        script_version_id=work["script_version_id"],
        performance_plan_id=work["performance_plan_id"], sample_rate=32000, channels=1,
        timing_source=None, note="夹具里的另一条音频")
    video = _approved_original(work)
    assert dh_gates.audio_track_status(video["id"])["outcome"] == "pass"

    dh_gates.record_audio_track_check(video["id"], audio_asset_id=other["id"],
                                     method="decode_align", outcome="pass",
                                     measured_offset_ms=0, evidence="夹具")
    status = dh_gates.audio_track_status(video["id"])
    assert status["outcome"] == "invalidated", \
        "核对绑定的是别条音频：结论已失效，不能继续返回 pass"
    assert status["recorded_outcome"] == "pass", "原始记录仍要保留，不作篡改"
    assert "不一致" in status["reason"]
    assert dh_gates.video_gate(video["id"])["ok"] is False


def test_confirm_final_video_requires_user_ack(work):
    video = _approved_original(work)
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.confirm_final_video(work["id"], video["id"], actor_id="user_1")
    assert e.value.code == "VIDEO_REVIEW_ACK_REQUIRED"

    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.confirm_final_video(work["id"], video["id"], actor_id="agent_1",
                                     review_ack=True)
    assert e.value.code == "VIDEO_ACTOR_INVALID"


def test_confirm_final_video_freezes_and_supersedes_previous(work):
    audio = _final_audio(work)
    first = _approved_original(work)
    out1 = dh_gates.confirm_final_video(work["id"], first["id"], actor_id="user_1",
                                        review_ack=True)
    assert out1["ok"] is True
    assert st.get("works", work["id"])["video_asset_id"] == first["id"]

    second = install_original(work["id"], audio_asset_id=audio["id"],
                             file_hash="sha256:" + "3" * 64)
    approve_original(second["id"], audio_asset_id=audio["id"])
    out2 = dh_gates.confirm_final_video(work["id"], second["id"], actor_id="user_1",
                                        review_ack=True)
    assert out2["revoked_previous"] == [first["id"]]
    assert vd.get_video(first["id"])["approval_state"] == "revoked"
    assert vd.final_video(work["id"])["id"] == second["id"]


def test_confirm_final_video_requires_gate_to_pass(work):
    video = install_original(work["id"], audio_asset_id=_final_audio(work)["id"])
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.confirm_final_video(work["id"], video["id"], actor_id="user_1",
                                     review_ack=True)
    assert e.value.code == "VIDEO_HARD_GATE_FAILED"


def test_confirm_final_video_rejects_other_work(work):
    other = prepare_work()
    video = _approved_original(other)
    with pytest.raises(TalkingHeadRejected) as e:
        dh_gates.confirm_final_video(work["id"], video["id"], actor_id="user_1",
                                     review_ack=True)
    assert e.value.code == "VIDEO_WORK_MISMATCH"


def test_gate_summary_does_not_report_unreviewed_as_passed(work):
    summary = dh_gates.gate_summary(work["id"])
    assert summary["final_video_id"] is None
    assert summary["original_confirmed"] is False
    assert summary["gate"] is None, "没审核不能写成通过"


# ---------- 失败保留 / 不自动回退 ----------

def test_failure_is_preserved_without_switching_route(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, _ = make_transport(responses=[volc_submit_ok("volc-1"),
                                             volc_query("done", video_url=None)])
    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=False)
    polled = dh_sub.poll_run(out["runs"][0]["id"], transport=transport)
    assert polled["state"] == "failed"
    assert polled["run"]["auto_fallback"] is False
    routes_used = {r["route"] for r in dh_sub.runs_for_work(work["id"])}
    assert routes_used == {"OMNIHUMAN_1_5"}, "失败不得自动改用 M2 或其他云口播"


def test_unknown_requires_reconciliation_before_resubmit(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, calls = make_transport(default=volc_submit_no_task_id())
    dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                            dry_run=False)
    assert len(calls) == 1
    with pytest.raises(TalkingHeadBlocked) as e:
        dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                dry_run=False)
    assert e.value.code == "RESUBMIT_REQUIRES_RECONCILIATION"
    assert "重复费用" in e.value.message
    assert len(calls) == 1, "被拦下的重提不得产生新的外部请求"


def test_failed_run_requires_explicit_replacement_authorization(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, _ = make_transport(responses=[volc_submit_ok("volc-1"),
                                             volc_query("done", video_url=None)])
    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=False)
    dh_sub.poll_run(out["runs"][0]["id"], transport=transport)

    with pytest.raises(TalkingHeadBlocked) as e:
        dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                dry_run=False)
    assert e.value.code == "RESUBMIT_REQUIRES_AUTHORIZATION"

    ok = dh_sub.resubmit_guard(work["id"], out["runs"][0]["request_digest"],
                               replacement_authorized=True)
    assert ok["ok"] is True and ok["replacement_authorized"] is True


def test_settle_unknown_records_evidence(work, monkeypatch):
    _creds(monkeypatch)
    allow_billable(monkeypatch)
    from dh_helpers import authorize_cost

    authorize_cost(work["id"])
    transport, _ = make_transport(default=volc_submit_no_task_id())
    out = dh_sub.submit_omnihuman(work["id"], task=_tasks(work), transport=transport,
                                  dry_run=False)
    run_id = out["runs"][0]["id"]
    settled = dh_sub.settle_unknown(run_id, "failed",
                                    {"source": "provider_query", "at": "2026-09-23",
                                     "note": "按原 task_id 查询：远端未受理"})
    assert settled["state"] == "failed"
    assert settled["reconciliation"]["evidence"]["source"] == "provider_query"
    assert dh_sub.submission_status(work["id"])["auto_fallback"] is False


def test_route_change_invalidates_video_but_keeps_confirmed_audio(work, monkeypatch):
    allow_billable(monkeypatch)
    video = _approved_original(work)
    dh_gates.confirm_final_video(work["id"], video["id"], actor_id="user_1", review_ack=True)
    audio_before = _final_audio(work)

    out = dh_gates.on_route_change(work["id"], "M2")
    assert out["previous_route"] == "OMNIHUMAN_1_5"
    assert video["id"] in out["revoked_confirmed"]
    assert vd.get_video(video["id"])["confirmed"] is False
    assert vd.final_video(work["id"]) is None
    assert st.get("works", work["id"])["video_asset_id"] is None
    audio_after = _final_audio(work)
    assert audio_after["id"] == audio_before["id"], "已确认音频不重新合成"
    assert "不重新合成声音" in out["note"]


# ---------- 分段约束 ----------

def test_segments_come_from_sentence_boundaries_of_one_audio_track(segmented_work):
    plan = _segment_plan(segmented_work)
    body = "".join(s["text"] for s in plan["segments"])
    assert body.strip(), "分段必须覆盖正文，不得丢字"
    prev_end = 0
    for seg in plan["segments"]:
        assert seg["global_offset_ms"] == prev_end, "偏移必须连续，否则拼接会漏字或重字"
        assert seg["duration_ms"] < plan["limit_ms"]
        prev_end = seg["end_ms"]
    assert plan["summary"]["speedup_applied"] is False
    assert plan["summary"]["truncation_applied"] is False


def test_over_limit_segment_is_rejected_not_squeezed():
    fake = [{"index": 0, "segment_id": "oh_seg_1", "duration_ms": 60000,
             "global_offset_ms": 0, "end_ms": 60000}]
    with pytest.raises(TalkingHeadRejected) as e:
        sg.assert_segments_submittable(fake)
    assert e.value.code == "SEGMENT_EXCEEDS_LIMIT"
    assert "禁止加速或截字" in e.value.message


def test_discontinuous_offsets_are_rejected():
    fake = [{"index": 0, "segment_id": "oh_seg_1", "duration_ms": 1000,
             "global_offset_ms": 0, "end_ms": 1000},
            {"index": 1, "segment_id": "oh_seg_2", "duration_ms": 1000,
             "global_offset_ms": 1200, "end_ms": 2200}]
    with pytest.raises(TalkingHeadRejected) as e:
        sg.assert_segments_submittable(fake)
    assert e.value.code == "SEGMENT_OFFSET_GAP"


def test_segmentation_requires_alignment_not_average_split():
    work = prepare_work(audio_ms=75000, with_alignment=False)
    with pytest.raises(TalkingHeadBlocked) as e:
        sg.create_plan(work["id"])
    assert e.value.code == "SEGMENTATION_REQUIRES_ALIGNMENT"


# ---------- 能力声明诚实性 ----------

def test_declared_and_verified_stay_separated():
    view = dh_routes.route_view(dh_routes.ROUTE_OMNIHUMAN)
    assert view["declared"]["audio_max_seconds_exclusive"] == 60
    assert view["declared"]["audio_recommended_seconds"] == 15
    assert view["verified"] == {}, "未实测就不能用声明填满 verified"
    assert view["verified_state"] == "unverified"
    assert any("不证明" in e for e in view["evidence"])


def test_historical_short_clip_is_not_long_form_evidence():
    view = dh_routes.route_view(dh_routes.ROUTE_OMNIHUMAN)
    assert any("不是当前人物" in e or "不证明长片" in e for e in view["evidence"])
    m2 = dh_routes.route_view(dh_routes.ROUTE_M2)
    assert "非约 60 秒能力证据" in m2["verified"]["exp_001"]["scope"]


def test_adapter_status_reports_config_state_before_verification(monkeypatch):
    """配置状态与实测状态是两件事：没凭据是 not_configured，有凭据没跑通才是 unverified。

    两者都写成 unverified 会让"缺凭据"看起来像"只差一次实测"，
    但前者要运维配置、后者要真跑一次，处理路径完全不同。
    """
    monkeypatch.delenv(pv.SECRET_ENV_AK, raising=False)
    monkeypatch.delenv(pv.SECRET_ENV_SK, raising=False)
    status = pv.adapter_status()
    assert status["omnihuman"]["configured"] is False
    assert status["omnihuman"]["state"] == "not_configured"
    assert pv.SECRET_ENV_AK in status["omnihuman"]["detail"], \
        "缺哪个配置项要说清楚，否则运维只能猜"
    assert status["omnihuman"]["verified"] == {}, "没有实测就不得有 verified 内容"

    monkeypatch.setenv(pv.SECRET_ENV_AK, "ak-fixture")
    monkeypatch.setenv(pv.SECRET_ENV_SK, "sk-fixture")
    status = pv.adapter_status()
    assert status["omnihuman"]["configured"] is True
    assert status["omnihuman"]["state"] == "unverified", \
        "凭据存在只说明能发请求，不说明模型与人物 B 已验证"
    assert status["omnihuman"]["verified"] == {}, "本用例没有真实调用，verifier 必须仍为空"
    assert status["m2_worker"]["state"] in ("unverified", "not_configured")
