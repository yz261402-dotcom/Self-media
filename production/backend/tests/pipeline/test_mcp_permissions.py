"""T02：受限 MCP 通道——能力白名单、角色权限、本机工具不得绕过台账。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import jobs, mcp, storage as st  # noqa: E402

WORK = "work_mcp_0001"
CTX = {"work_id": WORK, "task_id": "task_mcp_1", "work_item_id": "wi_mcp_1", "namespace": f"work:{WORK}:supervisor"}


@pytest.fixture
def ledger_probe(monkeypatch):
    monkeypatch.setitem(mcp.REGISTRY, "test_ledger_probe", {
        "kind": "external", "billable": False, "heavy": False, "verified": False,
        "provider": "offline_test"})
    monkeypatch.setitem(mcp.PERMISSIONS, "supervisor", [*mcp.PERMISSIONS["supervisor"], "test_ledger_probe"])
    return "test_ledger_probe"


def test_unregistered_capability_is_unavailable():
    with pytest.raises(mcp.McpDenied) as e:
        mcp.submit(role="audio", capability="face_swap", args={}, ctx=CTX, idempotency_key="k1")
    assert e.value.code == "CAPABILITY_NOT_REGISTERED"


def test_role_without_permission_is_denied():
    with pytest.raises(mcp.McpDenied) as e:
        mcp.submit(role="editor", capability="tts", args={"text": "x"}, ctx=CTX, idempotency_key="k2")
    assert e.value.code == "PERMISSION_DENIED"


def test_local_capability_cannot_go_through_submit():
    """本机纯函数走 submit 等于绕过台账，必须拒绝。"""
    with pytest.raises(mcp.McpDenied) as e:
        mcp.submit(role="content", capability="script_candidates", args={}, ctx=CTX, idempotency_key="k3")
    assert e.value.code == "NOT_EXTERNAL"


def test_external_capability_cannot_be_called_as_local():
    with pytest.raises(mcp.McpDenied) as e:
        mcp.call_local(role="supervisor", capability="tts", args={}, ctx=CTX, fn=lambda a, c: {})
    assert e.value.code == "NOT_LOCAL"


def test_submit_requires_work_binding():
    with pytest.raises(mcp.McpDenied) as e:
        mcp.submit(role="supervisor", capability="tts", args={"text": "x"}, ctx={}, idempotency_key="k4")
    assert e.value.code == "VALIDATION_FAILED"


def test_submit_creates_persistent_job(ledger_probe):
    job = mcp.submit(role="supervisor", capability=ledger_probe, args={"fixture": "test"},
                     ctx=CTX, idempotency_key=f"k_{st.new_id('i')}")
    assert job["state"] in ("queued", "blocked")
    assert jobs.get_job(job["id"])["capability"] == ledger_probe


def test_submit_is_idempotent_under_mcp(ledger_probe):
    key = f"k_{st.new_id('i')}"
    args = {"fixture": "test"}
    a = mcp.submit(role="supervisor", capability=ledger_probe, args=args, ctx=CTX, idempotency_key=key)
    b = mcp.submit(role="supervisor", capability=ledger_probe, args=args, ctx=CTX, idempotency_key=key)
    assert a["id"] == b["id"]
    assert b["replayed"] is True


def test_query_returns_reconciliation_requirement_for_unknown(monkeypatch):
    # 独立非计费测试能力保留 UNKNOWN 对账覆盖，不恢复已撤下的后期 MCP。
    monkeypatch.setitem(mcp.REGISTRY, "test_probe", {
        "kind": "external", "billable": False, "heavy": False, "verified": False,
        "provider": "offline_test",
    })
    monkeypatch.setitem(mcp.PERMISSIONS, "quality", [*mcp.PERMISSIONS["quality"], "test_probe"])
    job = mcp.submit(role="quality", capability="test_probe", args={"fixture": "t1"},
                     ctx=CTX, idempotency_key=f"k_{st.new_id('i')}")
    assert job["state"] == "queued"
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    jobs.mark_unknown(job["id"], "w1")
    out = mcp.query(job["id"], "quality")
    assert out["state"] == "unknown"
    assert out["reconciliation"]["may_create_replacement"] is False


def test_query_by_unauthorized_role_denied(ledger_probe):
    job = mcp.submit(role="supervisor", capability=ledger_probe, args={"fixture": "test"},
                     ctx=CTX, idempotency_key=f"k_{st.new_id('i')}")
    with pytest.raises(mcp.McpDenied) as e:
        mcp.query(job["id"], "editor")
    assert e.value.code == "PERMISSION_DENIED"


def test_local_call_is_audited():
    def fn(args, ctx):
        return {"ok": True}

    out = mcp.call_local(role="content", capability="script_candidates", args={"q": 1},
                         ctx=dict(CTX, idempotency_key="k_local"), fn=fn)
    assert out["result"]["ok"] is True
    audit = mcp.audit_for_work_item("wi_mcp_1")
    assert any(a["tool"] == "mcp.local:script_candidates" for a in audit)


def test_registered_capability_is_not_auto_verified():
    mcp.register("face_swap", {"kind": "external", "billable": True, "heavy": True, "verified": False})
    spec = mcp.REGISTRY["face_swap"]
    assert spec["verified"] is False
    with pytest.raises(mcp.McpDenied):
        mcp.submit(role="avatar", capability="face_swap", args={}, ctx=CTX, idempotency_key="k5")


def test_capabilities_for_role_is_a_subset():
    assert "tts" in mcp.capabilities_for("supervisor")
    assert mcp.capabilities_for("audio") == []
    assert "tts" not in mcp.capabilities_for("editor")
