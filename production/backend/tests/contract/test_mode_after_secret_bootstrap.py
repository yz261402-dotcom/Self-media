"""运行模式必须在密钥注入**之后**重算——回归：曾把真实调用记成 mock。

`main.MODE` 是模块级常量，import 时就算好了；而供应商凭据是 lifespan 启动时才从
密钥文件读进来的。于是"凭据放在密钥文件里"这种（也就是实际部署的）情形下，
算模式时每个供应商都还是 `not_configured` → `_production_ready()` 为假 →
`resolve_mode()` 一律回落 `mock`。

产出本身不受影响（`plates.py` / `submission.py` / `_tts_dry_run()` 都是**调用时**
才算），所以这个 bug 不会自己暴露；受影响的是记录：`_create_task` 会把
`task.mode` 写成 mock，于是一次真花了钱的调用在台账里记着 mock。

这两条测试的分工：
    · 第一条钉住"密钥/证据到位后必须变成 production"（本 bug 的正例）；
    · 第二条钉住"只给环境变量、没有实测证据，仍然进不了 production"，
      防止为了修第一条而把越级拦截一起放开。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app import config, main, storage as st

DEEPSEEK_KEY_ENV = "AVATAR_STUDIO_DEEPSEEK_API_KEY"


@pytest.fixture()
def production_env(monkeypatch):
    """把环境摆成"允许生产"，但**不注入任何真实密钥文件**（保持用例无副作用）。"""
    monkeypatch.setattr(main, "MODE", main.MODE)  # 用完恢复模块级 MODE
    monkeypatch.setenv("AVATAR_STUDIO_MODE", "production")
    monkeypatch.setenv("AVATAR_STUDIO_ALLOW_PRODUCTION", "1")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "1")
    monkeypatch.setenv(DEEPSEEK_KEY_ENV, "sk-test-fixture-not-a-real-key")
    # 不指向真实密钥文件：本用例只验"模式重算"，不去碰用户目录
    monkeypatch.setenv("AVATAR_STUDIO_SECRETS_FILE", os.path.join(st.DB_PATH + ".absent"))


def _record_evidence(outcome: str = "succeeded") -> None:
    """写入一条供应商实测证据——`usable` 只认它，配置项存在本身只算 unverified。"""
    st.put("provider_evidence", "pev_fixture_mode", {
        "id": "pev_fixture_mode", "provider": "deepseek", "capability": "chat",
        "outcome": outcome, "checked_at": "2026-09-25T00:00:00",
        "evidence": "测试夹具构造，不代表真实连通", "detail": "",
    })


def _clear_evidence() -> None:
    """同一会话共用一个测试库，证据行会跨用例残留 → 每个用例自带前置，不靠执行顺序。"""
    c = st.conn()
    c.execute("DELETE FROM provider_evidence WHERE id=?", ["pev_fixture_mode"])
    c.commit()


def test_mode_is_recomputed_after_startup_when_evidence_exists(production_env):
    _record_evidence("succeeded")
    # 进 lifespan 才算真正启动：密钥/证据在这一步之后才齐
    with TestClient(main.app) as client:
        assert client.get("/api/v1/health").json()["mode"] == "production"


def test_env_alone_cannot_reach_production(production_env):
    """没有实测证据时，只写 AVAATAR_STUDIO_MODE=production 也不许进生产。"""
    _clear_evidence()
    with TestClient(main.app) as client:
        assert client.get("/api/v1/health").json()["mode"] == "mock"
    # 直接调解析函数也应是 mock：越级拦截不该被这次改动放宽
    assert config.resolve_mode() == "mock"
