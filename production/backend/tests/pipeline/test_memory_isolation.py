"""T02：分层记忆与命名空间隔离——分支隔离、共享经验须审核、期望版本合并。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import memory  # noqa: E402

NS_A = "work:work_a:branch_1"
NS_B = "work:work_b:branch_1"


def test_write_requires_namespace():
    with pytest.raises(memory.MemoryDenied) as e:
        memory.write(layer="experience", namespace="", key="k", content={}, actor_role="content")
    assert e.value.code == "NAMESPACE_REQUIRED"


def test_unknown_layer_rejected():
    with pytest.raises(memory.MemoryDenied) as e:
        memory.write(layer="secret", namespace=NS_A, key="k", content={}, actor_role="content")
    assert e.value.code == "VALIDATION_FAILED"


def test_shared_experience_cannot_be_written_as_approved():
    with pytest.raises(memory.MemoryDenied) as e:
        memory.write(layer="experience", namespace=NS_A, key="k", content={"x": 1},
                     actor_role="content", scope="project", state="approved")
    assert e.value.code == "REVIEW_REQUIRED"


def test_branch_entries_stay_in_their_namespace():
    memory.write(layer="experience", namespace=NS_A, key="hook", content={"t": "A 的开头"},
                 actor_role="content", scope="role")
    found = memory.search(layer="experience", namespace=NS_B)
    assert all(e["namespace"] != NS_A for e in found)


def test_proposed_experience_is_not_cross_task_visible():
    e = memory.write(layer="experience", namespace=NS_A, key="tip", content={"t": "候选经验"},
                     actor_role="content", scope="project")
    assert e["state"] == "proposed"
    visible_ids = {x["id"] for x in memory.search(scope="project", approved_only=True)}
    assert e["id"] not in visible_ids


def test_approved_experience_becomes_visible():
    e = memory.write(layer="experience", namespace=NS_A, key="tip2", content={"t": "已审经验"},
                     actor_role="content", scope="project")
    approved = memory.approve(e["id"], e["content_digest"], reviewer="user")
    assert approved["state"] == "approved"
    assert e["id"] in {x["id"] for x in memory.search(scope="project", approved_only=True)}


def test_approve_with_wrong_expected_digest_conflicts():
    e = memory.write(layer="experience", namespace=NS_A, key="tip3", content={"t": "v1"},
                     actor_role="content", scope="project")
    with pytest.raises(memory.MemoryDenied) as exc:
        memory.approve(e["id"], "sha256:stale-digest", reviewer="user")
    assert exc.value.code == "VERSION_CONFLICT"


def test_only_proposed_can_be_approved():
    e = memory.write(layer="experience", namespace=NS_A, key="tip4", content={"t": "x"},
                     actor_role="content", scope="project")
    memory.approve(e["id"], e["content_digest"], reviewer="user")
    with pytest.raises(memory.MemoryDenied) as exc:
        memory.approve(e["id"], e["content_digest"], reviewer="user")
    assert exc.value.code == "STATE_CONFLICT"


def test_supersede_preserves_old_entry():
    old = memory.write(layer="experience", namespace=NS_A, key="rule", content={"v": 1},
                       actor_role="content", scope="project")
    memory.approve(old["id"], old["content_digest"], reviewer="user")
    new = memory.supersede(old["id"], layer="experience", namespace=NS_A, key="rule",
                           content={"v": 2}, actor_role="content", reviewer="user")
    assert new["version"] == 2
    assert memory.get(old["id"])["state"] == "superseded"
    assert memory.get(old["id"])["superseded_by"] == new["id"]
    assert memory.get(old["id"])["content"] == {"v": 1}, "原始结果必须保留"


def test_rejected_entry_is_not_visible():
    e = memory.write(layer="experience", namespace=NS_A, key="tip5", content={"t": "bad"},
                     actor_role="content", scope="project")
    memory.reject(e["id"], reviewer="user", reason="证据不足")
    assert e["id"] not in {x["id"] for x in memory.search(scope="project")}


def test_layers_are_separated():
    c = memory.write(layer="checkpoint", namespace="task:t1", key="state", content={"s": 1},
                     actor_role="supervisor", task_id="t1")
    p = memory.write(layer="persona", namespace="identity:av_base_b_v1", key="desc",
                     content={"d": "人物 B"}, actor_role="avatar")
    assert memory.read(layer="checkpoint", namespace="task:t1", key="state")["id"] == c["id"]
    assert memory.read(layer="persona", namespace="identity:av_base_b_v1", key="desc")["id"] == p["id"]
    assert memory.search(layer="checkpoint") != memory.search(layer="persona")


def test_pending_reviews_lists_only_project_candidates():
    memory.write(layer="experience", namespace=NS_B, key="pending1", content={"t": "待审"},
                 actor_role="content", scope="project")
    pend = memory.pending_reviews("project")
    assert any(e["key"] == "pending1" for e in pend)
    assert all(e["state"] == "proposed" for e in pend)


def test_isolation_report_has_no_leak():
    rep = memory.isolation_report()
    assert rep["isolated"] is True
    assert rep["leaked_unapproved"] == []
