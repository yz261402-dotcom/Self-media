"""真实网络传输层契约测试：urllib_transport 对本地 mock 服务器发起真实 HTTP。

不触碰任何真实密钥、不访问外网；只验证传输层确实把请求发出去并把字节拿回来，
契约与 digital_human/providers.py 的 _resp_json 对齐（{status_code, body}）。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.digital_human import providers as pv


def _server_return(payload: dict):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(n)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_submit_request_url_carries_signed_query():
    """回归钉子（2026-09-23 实测踩坑）：签名覆盖的 Action/Version 必须真实
    出现在请求 URL 上，否则网关报 "missing Action parameter"。
    本测试不联网——只钉请求构造的 URL 形态。"""
    req = pv.build_submit_request(
        image_url="https://example.com/i.png", audio_url="https://example.com/a.wav",
        output_resolution="1080p",
        ak="AKLT-test", sk="c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0", x_date="20260923T000000Z")
    assert "Action=CVSubmitTask" in req["url"], req["url"]
    assert "Version=2022-08-31" in req["url"], req["url"]
    # output_resolution 必须转成 int（"1080p" 字符串会被云端预处理拒绝）
    assert req["json"]["output_resolution"] == 1080, req["json"]
    assert pv.normalize_resolution("720p") == 720
    assert pv.normalize_resolution("999") is None
    q_req = pv.build_query_request(task_id="t-1", ak="AKLT-test",
                                   sk="c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0",
                                   x_date="20260923T000000Z")
    assert "Action=CVGetResult" in q_req["url"], q_req["url"]
    assert "Version=2022-08-31" in q_req["url"], q_req["url"]


def test_urllib_transport_post_returns_status_and_body():
    server, port = _server_return({"data": {"task_id": "t1"}})
    try:
        resp = pv.urllib_transport(
            {"url": f"http://127.0.0.1:{port}/x", "method": "POST",
             "headers": {"X-Test": "1"}, "body": b"{}"})
        assert resp["status_code"] == 200
        assert json.loads(resp["body"]) == {"data": {"task_id": "t1"}}
    finally:
        server.shutdown()


def test_urllib_transport_get_works():
    server, port = _server_return({"ok": True})
    try:
        resp = pv.urllib_transport({"url": f"http://127.0.0.1:{port}/y", "method": "GET"})
        assert resp["status_code"] == 200
        assert json.loads(resp["body"]) == {"ok": True}
    finally:
        server.shutdown()


def test_urllib_transport_http_error_status():
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(n)
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = pv.urllib_transport(
            {"url": f"http://127.0.0.1:{server.server_address[1]}/z", "method": "POST",
             "body": b"{}"})
        assert resp["status_code"] == 403
    finally:
        server.shutdown()


def test_no_http_response_is_not_mistaken_for_an_ambiguous_answer(monkeypatch):
    """连 HTTP 响应都没拿到时，状态必须是"没问到"，不能记成 unknown。

    2026-09-28 真实踩到：一次 TLS 抖动（UNEXPECTED_EOF_WHILE_READING）让查询
    返回"无响应"，而解析层把它当成"远端给了含糊答案"，于是**正在正常生成**的
    作业被记成 unknown——一次网络抖动变成一次人工对账，还挡住了重提。
    "没问到"和"问到了『不知道』"是两件事，不能共用一个状态。
    """
    import ssl
    import urllib.request

    def boom(*_args, **_kwargs):
        raise ssl.SSLError("UNEXPECTED_EOF_WHILE_READING")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    dead = pv.urllib_transport({"url": "https://visual.volcengineapi.com/",
                                "method": "POST", "body": b"{}"})
    assert dead["status_code"] is None and dead["error"] == "network_error"

    assert pv.parse_query_response(dead)["state"] == "unavailable"
    assert pv.parse_m2_response(dead)["state"] == "unavailable"


def test_gateway_errors_are_also_unavailable_not_unknown():
    """网关 502/503/504：请求没到业务层，同样什么都没问到。

    本机就有这种情形——本地代理对不可达上游回 502。若把它记成 unknown，
    一次代理抽风就会让在飞的作业需要人工对账。
    """
    for status in (502, 503, 504):
        assert pv.parse_query_response({"status_code": status, "body": b"{}"})["state"] \
            == "unavailable"
    # 业务层有答复（4xx/5xx 非网关）才是真正的"结果未知"。
    for status in (400, 500):
        assert pv.parse_query_response({"status_code": status, "body": b"{}"})["state"] \
            == "unknown"
