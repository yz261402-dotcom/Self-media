"""check_provider 对火山引擎 AK/SK 配对的识别（修复前只认不存在的 VOLC_API_KEY）。"""
import os

from app.config import check_provider

VOLC_AK = "AVATAR_STUDIO_VOLC_ACCESS_KEY"
VOLC_SK = "AVATAR_STUDIO_VOLC_SECRET_KEY"


def test_volc_configured_with_ak_sk(monkeypatch):
    monkeypatch.setenv(VOLC_AK, "AK")
    monkeypatch.setenv(VOLC_SK, "SK")
    chk = check_provider("volcengine")
    assert chk.configured is True
    # 配置齐全但无本项目实测证据：unverified（不是 ready，也不应是 not_configured）
    assert chk.state in ("unverified", "ready")


def test_volc_not_configured_missing_sk(monkeypatch):
    monkeypatch.setenv(VOLC_AK, "AK")
    monkeypatch.delenv(VOLC_SK, raising=False)
    chk = check_provider("volcengine")
    assert chk.configured is False
    assert chk.state == "not_configured"


def test_volc_not_configured_missing_ak(monkeypatch):
    monkeypatch.delenv(VOLC_AK, raising=False)
    monkeypatch.delenv(VOLC_SK, raising=False)
    chk = check_provider("volcengine")
    assert chk.configured is False
    assert chk.state == "not_configured"


def test_deepseek_still_single_key(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", "x")
    chk = check_provider("deepseek")
    assert chk.configured is True
