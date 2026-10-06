import importlib.util
import json
import sys
import urllib.error
import uuid
from pathlib import Path

import pytest


PRODUCTION = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PRODUCTION / "backend"))


def load():
    path = PRODUCTION / "scripts" / "probe_content_writer.py"
    assert path.exists(), "single-call writer probe is not implemented"
    spec = importlib.util.spec_from_file_location("probe_content_writer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    from app import config, storage as st
    monkeypatch.setenv("AVATAR_STUDIO_DATABASE_URL", "postgresql://avatar:avatar_studio_local@127.0.0.1:15432/avatar_studio")
    monkeypatch.setenv("AVATAR_STUDIO_ANTHROPIC_API_KEY", "sk-test-private-writer-key")
    monkeypatch.setenv("AVATAR_STUDIO_ANTHROPIC_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_PROVIDER", "anthropic")
    monkeypatch.setenv("AVATAR_STUDIO_FX_USD_CNY", "7.2")
    monkeypatch.setattr(config, "_load_local_env_file", lambda: None)
    monkeypatch.setattr(config, "bootstrap_secrets", lambda: [])
    monkeypatch.setattr(st, "DB_PATH", str(tmp_path / uuid.uuid4().hex / "studio.db"))
    prior = getattr(st._local, "conn", None)
    st._local.conn = None
    st.init_db()
    yield st
    st._local.conn.close()
    st._local.conn = prior


def last_call(st):
    row = st.conn().execute("SELECT data FROM provider_jobs ORDER BY ins_seq DESC LIMIT 1").fetchone()
    return json.loads(row["data"]) if row else None


def test_default_creates_only_reviewable_plan_and_never_calls_provider(isolated, tmp_path):
    p = load()
    calls = []
    plan = p.run_probe(tmp_path / "probe", transport=lambda *args: calls.append(args))
    assert calls == [] and last_call(isolated) is None
    assert plan["model"] == "claude-sonnet-5-5"
    assert plan["max_tokens"] == 64 and plan["timeout_seconds"] == 30
    assert plan["endpoint"] == "https://relay.example/v1/messages"
    assert plan["request"]["messages"] == [{"role": "user", "content": '只返回 {"ok":true}。'}]
    assert "sk-test-private" not in json.dumps(plan)
    assert p.run_probe(tmp_path / "probe")["plan_id"] == plan["plan_id"]


def test_502_evidence_is_redacted_and_ledger_holds_unknown_fees(isolated, tmp_path):
    p = load()
    calls = []
    def transport(url, headers, body, timeout):
        calls.append((url, headers, json.loads(body), timeout))
        raw = b'{"error":{"type":"upstream_error","message":"no healthy channel for claude-sonnet-5-5 sk-test-private-writer-key sk-another-long-secret"}}'
        return 502, raw, {"X-Request-Id": "req_123", "Set-Cookie": "private-cookie", "X-Api-Key": headers["x-api-key"]}
    out = tmp_path / "probe"
    p.run_probe(out)
    report = p.run_probe(out, execute=True, transport=transport)
    job = last_call(isolated)
    assert len(calls) == 1 and calls[0][2]["model"] == "claude-sonnet-5-5"
    assert calls[0][2]["max_tokens"] == 64 and calls[0][3] == 30
    assert calls[0][1]["user-agent"] == "AvatarStudio/0.1"
    assert job["state"] == "failed" and job["settlement_state"] == "held" and job["actual_cost_fen"] is None
    assert report["job_id"] == job["id"]
    evidence = json.loads((out / "response.json").read_text(encoding="utf-8"))
    assert "no healthy channel" in evidence["error_body_redacted"]
    assert evidence["headers"] == {"x-request-id": "req_123"}
    saved = "".join(f.read_text(encoding="utf-8") for f in out.glob("*.json"))
    assert "sk-test-private" not in saved and "sk-another-long" not in saved and "private-cookie" not in saved
    with pytest.raises(p.ProbeRejected, match="already|已"):
        p.run_probe(out, execute=True, transport=transport)
    assert len(calls) == 1


def test_timeout_is_unknown_and_cannot_be_sent_again(isolated, tmp_path):
    p = load()
    calls = []
    def transport(*args):
        calls.append(args)
        raise urllib.error.URLError(TimeoutError("secret network detail"))
    out = tmp_path / "probe"
    p.run_probe(out)
    report = p.run_probe(out, execute=True, transport=transport)
    job = last_call(isolated)
    assert report["state"] == "unknown"
    assert job["state"] == "unknown" and job["settlement_state"] == "held" and job["actual_cost_fen"] is None
    with pytest.raises(p.ProbeRejected):
        p.run_probe(out, execute=True, transport=transport)
    assert len(calls) == 1


def test_plan_change_or_endpoint_change_prevents_sending(isolated, monkeypatch, tmp_path):
    p = load()
    out = tmp_path / "probe"
    p.run_probe(out)
    monkeypatch.setenv("AVATAR_STUDIO_ANTHROPIC_BASE_URL", "https://other.example")
    with pytest.raises(p.ProbeRejected, match="plan|计划"):
        p.run_probe(out, execute=True, transport=lambda *args: pytest.fail("must not send"))
    assert last_call(isolated) is None


def test_redirects_never_forward_credentials():
    p = load()
    req = __import__("urllib.request", fromlist=["Request"]).Request("https://relay.example/v1/messages", data=b"{}", headers={"x-api-key": "private"})
    assert p.NoRedirects().redirect_request(req, None, 307, "redirect", {}, "https://other.example/v1/messages") is None


def test_error_type_cannot_echo_the_auth_key_to_ledger(isolated, tmp_path):
    p = load()
    out = tmp_path / "probe"
    p.run_probe(out)
    raw = b'{"error":{"type":"sk-test-private-writer-key","message":"bad upstream"}}'
    p.run_probe(out, execute=True, transport=lambda *args: (502, raw, {}))
    assert "sk-test-private-writer-key" not in json.dumps(last_call(isolated))


def test_standalone_loads_local_env_before_bootstrapping_keys(isolated, monkeypatch, tmp_path):
    from app import config
    p = load()
    calls = []
    monkeypatch.setattr(config, "_load_local_env_file", lambda: calls.append("env"))
    monkeypatch.setattr(config, "bootstrap_secrets", lambda: calls.append("keys"))
    p.run_probe(tmp_path / "probe")
    assert calls == ["env", "keys"]


def test_json_escaped_auth_key_is_redacted_before_error_capture(isolated, tmp_path):
    p = load()
    out = tmp_path / "probe"
    p.run_probe(out)
    encoded = "".join("\\u%04x" % ord(c) for c in "sk-test-private-writer-key")
    other_encoded = "".join("\\u%04x" % ord(c) for c in "sk-another-long-secret")
    raw = ('{"error":{"type":"upstream_error","message":"encoded ' + encoded + ' ' + other_encoded + '"}}').encode()
    p.run_probe(out, execute=True, transport=lambda *args: (502, raw, {}))
    evidence = json.loads((out / "response.json").read_text(encoding="utf-8"))
    inner = json.loads(evidence["error_body_redacted"])
    assert "sk-test-private-writer-key" not in inner["error"]["message"]
    assert "sk-another-long-secret" not in inner["error"]["message"]
    assert "[REDACTED]" in inner["error"]["message"]


def test_success_uses_real_usage_without_changing_work_pointers(isolated, tmp_path):
    p = load()
    before = {"id": p.WORK_ID, "writing_prompt_id": "prompt_before", "current_content_job_id": "job_before", "version": 3}
    isolated.put("works", p.WORK_ID, before)
    raw = json.dumps({"id": "msg_success", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": '{"ok": true}'}], "stop_reason": "end_turn", "usage": {"input_tokens": 38, "output_tokens": 6}}).encode()
    out = tmp_path / "probe"
    p.run_probe(out)
    report = p.run_probe(out, execute=True, transport=lambda *args: (200, raw, {}))
    assert report["state"] == "succeeded" and last_call(isolated)["settlement_state"] == "settled"
    assert report["probe_ok"] is True
    assert last_call(isolated)["usage"] == {"input_tokens": 38, "output_tokens": 6}
    assert isolated.get("works", p.WORK_ID) == before
