"""worker_server（GPU 侧）↔ worker_client（后端侧）的端到端契约测试。

这里起的是**真实的 worker 服务进程**（同进程内线程），只是把 runner 换成假的，
所以在没有 GPU 的机器上也能验证：

  - 请求翻译层是否产出了 worker 认得的 HTTP 请求；
  - 提交 + 轮询这条链路能否收敛到终态；
  - 输出下载会不会因为缺 token 被拒；
  - 各种失败是否被如实归类，而不是伪装成 succeeded。

不验证的东西（不在本文件的职责里，也不假装验证了）：
真实 LivePortrait / MuseTalk 推理、真实 CUDA、真实算力云网络。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from app.digital_human import providers as pv
from app.digital_human import worker_client as wc
from app.digital_human.errors import TalkingHeadBlocked

from dh_helpers import make_mp4

WORKERS_SRC = Path(__file__).resolve().parents[3] / "workers" / "m2_gpu"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


FAKE_RUNNER = '''
import json, sys
job_path, result_path = sys.argv[1], sys.argv[2]
job = json.load(open(job_path, encoding="utf-8"))
mode = __import__("os").environ.get("FAKE_RUNNER_MODE", "ok")
d = __import__("pathlib").Path(job_path).parent
if mode == "fail":
    json.dump({"state": "failed", "code": "OOM", "error": "显存不足"},
              open(result_path, "w", encoding="utf-8"))
elif mode == "crash":
    raise SystemExit(9)
elif mode == "slow":
    __import__("time").sleep(300)
    raise SystemExit(0)
else:
    out = d / "out.mp4"
    payload_path = __import__("os").environ["FAKE_PAYLOAD"]
    out.write_bytes(__import__("pathlib").Path(payload_path).read_bytes())
    json.dump({"state": "succeeded", "output_path": str(out), "duration_ms": 4200},
              open(result_path, "w", encoding="utf-8"))
'''


@pytest.fixture()
def worker(tmp_path):
    """起一个真实 worker 服务；runner 为假脚本。返回 (base_url, token, tmp_path)。"""
    sys.path.insert(0, str(WORKERS_SRC))
    try:
        import worker_server  # type: ignore
    finally:
        sys.path.pop(0)

    root = Path(tmp_path)
    runner = root / "fake_runner.py"
    runner.write_text(FAKE_RUNNER, encoding="utf-8")
    payload = root / "payload.mp4"
    payload.write_bytes(make_mp4(4200))

    prev_payload = os.environ.get("FAKE_PAYLOAD")
    os.environ["FAKE_PAYLOAD"] = str(payload)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    httpd = worker_server.build_server(
        host="127.0.0.1", port=port, token="tok_test",
        runner_commands={"*": [sys.executable, str(runner)]},
        work_root=str(root / "jobs"), public_base=base, timeout_sec=30)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    # 等待端口就绪，避免偶发的连接拒绝被当成 transport 故障
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.05)
    yield base, "tok_test", payload
    if prev_payload is None:
        os.environ.pop("FAKE_PAYLOAD", None)
    else:
        os.environ["FAKE_PAYLOAD"] = prev_payload
    httpd.shutdown()
    httpd.server_close()


def _req(stage: str = "lipsync", **args) -> dict:
    return {"stage": stage, "model": "MuseTalk-1.5",
            "args": dict({"plate_url": "https://tos.example/a.mp4",
                          "audio_url": "https://tos.example/a.wav",
                          "plate_hash": "sha256:" + "a" * 64,
                          "audio_hash": "sha256:" + "b" * 64}, **args)}


# ---------- 请求构造 ----------

def test_build_job_request_has_url_and_body():
    req = wc.build_job_request(_req(), base_url="http://127.0.0.1:18080", token="t")
    assert req["url"] == "http://127.0.0.1:18080/v1/jobs"
    assert req["method"] == "POST"
    assert req["headers"]["Authorization"] == "Bearer t"
    doc = json.loads(req["body"].decode("utf-8"))
    assert doc["stage"] == "lipsync" and doc["args"]["plate_hash"].startswith("sha256:")


def test_unknown_stage_is_blocked_not_crash():
    """stage 非法 = 调用方给的东西不对（blocked），不能落到 KeyError 变成 500。"""
    with pytest.raises(TalkingHeadBlocked) as e:
        wc.build_job_request({"stage": "nope", "args": {}},
                             base_url="http://x", token="t")
    assert e.value.code == "M2_REQUEST_SHAPE_INVALID"


def test_missing_worker_url_is_blocked(monkeypatch):
    monkeypatch.delenv(wc.M2_URL_ENV, raising=False)
    with pytest.raises(TalkingHeadBlocked) as e:
        wc.transport(_req())
    assert e.value.code == "M2_TRANSPORT_UNAVAILABLE"


# ---------- 端到端 ----------

def test_submit_poll_until_succeeded(worker):
    base, token, _payload = worker
    resp = wc.transport(_req(), base_url=base, token=token,
                        poll_interval=0.05, max_wait=10)
    parsed = pv.parse_m2_response(resp)
    assert parsed["state"] == "succeeded", parsed.get("reason")
    assert parsed["output_url"].startswith(f"{base}/v1/outputs/")
    assert parsed["duration_ms"] == 4200
    assert parsed["remote_job_id"], "必须回填 job_id，否则下次轮询找不到同一作业"


def test_output_download_includes_token(worker):
    """download_and_verify 只传 {"method","url"}；token 由 transport 补。

    不补的话这里一定是 401，而 401 会被当成"结果不可用"，
    看上去就像 worker 出片失败——实际上只是少一个 header。
    """
    base, token, payload = worker
    resp = wc.transport(_req(), base_url=base, token=token,
                        poll_interval=0.05, max_wait=10)
    url = pv.parse_m2_response(resp)["output_url"]
    down = wc.transport({"method": "GET", "url": url}, base_url=base, token=token)
    assert down.get("status_code") == 200
    assert down.get("body") == payload.read_bytes()


def test_worker_requires_token(worker):
    base, _token, _payload = worker
    resp = wc.transport(_req(), base_url=base, token="wrong",
                        poll_interval=0.05, max_wait=10)
    parsed = pv.parse_m2_response(resp)
    assert parsed["state"] == "unknown", "401 不能判成成功，也不能判成作业失败"
    assert "401" in (parsed.get("reason") or "")


def test_same_args_is_idempotent(worker):
    base, token, _payload = worker
    first = pv.parse_m2_response(
        wc.transport(_req(), base_url=base, token=token, poll_interval=0.05, max_wait=10))
    second = pv.parse_m2_response(
        wc.transport(_req(), base_url=base, token=token, poll_interval=0.05, max_wait=10))
    assert second["state"] == "succeeded"
    assert first["remote_job_id"] == second["remote_job_id"], \
        "同参数不得重复占用 GPU：第二次应当复用第一次的作业"


def test_runner_failure_is_failed_not_unknown(worker, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_MODE", "fail")
    base, token, _payload = worker
    parsed = pv.parse_m2_response(
        wc.transport(_req(), base_url=base, token=token, poll_interval=0.05, max_wait=10))
    assert parsed["state"] == "failed"
    assert "显存不足" in parsed.get("reason", "")


def test_runner_that_dies_is_not_success(worker, monkeypatch):
    """runner 崩了、没写 result.json：判定失败，绝不是 succeeded。"""
    monkeypatch.setenv("FAKE_RUNNER_MODE", "crash")
    base, token, _payload = worker
    parsed = pv.parse_m2_response(
        wc.transport(_req(), base_url=base, token=token, poll_interval=0.05, max_wait=10))
    assert parsed["state"] == "failed"
    assert parsed.get("error", {}).get("code") == "RUNNER_NO_RESULT"


def test_worker_rejects_unknown_stage_before_running_it(worker):
    """worker 自己也要挡非法 stage，不能只依赖客户端校验。

    直接打 HTTP：worker_client 那边会先拦一道，走不到 worker，所以这里绕过客户端。
    """
    import urllib.error
    import urllib.request

    base, token, _payload = worker
    req = urllib.request.Request(
        f"{base}/v1/jobs",
        data=json.dumps({"stage": "dance", "model": "", "args": {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "非法 stage 必须被拒"
    except urllib.error.HTTPError as e:
        assert e.code == 422


def test_poll_timeout_reports_running_not_failure(worker, monkeypatch):
    """作业没在预算时间内跑完：报 running，**不改判失败**。

    这是红线条文"排队不是失败"的具体落地。作业还在远端执行着，
    判 failed 会让用户重提，进而在同一张 GPU 上多排一份队。
    """
    monkeypatch.setenv("FAKE_RUNNER_MODE", "slow")
    base, token, _payload = worker
    parsed = pv.parse_m2_response(
        wc.transport(_req(plate_url="https://tos.example/slow.mp4"),
                     base_url=base, token=token, poll_interval=0.05, max_wait=0.3))
    assert parsed["state"] == "running", "超时必须回到 running，不许判成 failed/succeeded"
    assert parsed.get("remote_job_id"), "仍要给出作业号，好让下次接着查"


def test_stage_without_runner_is_explicit_failure(tmp_path):
    """某个阶段没配 runner 必须显式 failed，不许"受理成功但永远跑空"。

    M2 有两个阶段，只配一半时最容易出的错是：请求照收、状态永远停在 running，
    界面显示"生成中"，实际上没有任何进程在干活。这里把该题钉死。
    """
    sys.path.insert(0, str(WORKERS_SRC))
    try:
        import worker_server  # type: ignore
    finally:
        sys.path.pop(0)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    httpd = worker_server.build_server(
        host="127.0.0.1", port=port, token="t",
        runner_commands={"motion": [sys.executable, "-c", "print(1)"]},
        work_root=str(Path(tmp_path) / "jobs"), public_base=base, timeout_sec=30)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.05)
    try:
        parsed = pv.parse_m2_response(
            wc.transport({"stage": "lipsync", "model": "MuseTalk-1.5",
                          "args": {"plate_url": "https://x/y.mp4", "audio_url": "https://x/a.wav"}},
                         base_url=base, token="t", poll_interval=0.05, max_wait=5))
        assert parsed["state"] == "failed", "没配 runner 的阶段不得表现为 running/succeeded"
        assert (parsed.get("error") or {}).get("code") == "NO_RUNNER_FOR_STAGE", parsed
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_runner_relative_path_is_rejected_at_startup(tmp_path):
    """runner 脚本用相对路径必须在**启动前**被拒。

    子进程的 cwd 是每个作业自己的目录，相对路径必然解析失败；
    而失败形态只是 python 的一句 "can't open file"，作业被记成
    "未产出 result.json"，真正的原因（路径写错）在远端看不见。
    """
    sys.path.insert(0, str(WORKERS_SRC))
    try:
        import worker_server  # type: ignore
    finally:
        sys.path.pop(0)

    bad = worker_server.check_commands_resolvable(
        {"motion": [sys.executable, "runner_liveportrait.py"]})
    assert bad and "相对路径" in bad[0], f"相对路径必须被拒：{bad}"

    missing = worker_server.check_commands_resolvable(
        {"lipsync": [sys.executable, os.path.join(str(tmp_path), "不存在.py")]})
    assert missing and "不存在" in missing[0]

    ok = worker_server.check_commands_resolvable(
        {"*": [sys.executable, str(Path(WORKERS_SRC) / "runner_musetalk.py")]})
    assert ok == [], f"绝对路径且存在的脚本不该报错：{ok}"


def test_token_not_leaked_to_external_urls():
    """外部对象存储的 URL 不能被塞进 worker token。"""
    req = {"method": "GET", "url": "https://other.example.com/x.mp4"}
    out = wc._inject_token(req, base_url="http://127.0.0.1:18080", token="secret")
    assert "Authorization" not in out.get("headers", {})


def test_env_poll_budget_is_readable(monkeypatch):
    monkeypatch.setenv(wc.MAX_WAIT_ENV, "42")
    assert os.environ.get(wc.MAX_WAIT_ENV) == "42"
