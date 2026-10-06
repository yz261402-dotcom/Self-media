"""Image 2.5 model selection contracts.

These tests are intentionally offline: they only inspect request construction and
the public provider configuration.  The image provider id remains stable for
backwards compatibility, while its default *model* is fixed to the Image 2.5
Sunburst variant because this route prioritizes character consistency.  Operators
may explicitly switch the model (for example, to the faster Flare variant) via
the existing environment override.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app import config  # noqa: E402
from app import storage as st  # noqa: E402
from app.avatar import person_image as pi  # noqa: E402


def test_person_image_default_request_uses_image_25_sunburst(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设置覆盖变量时，业务请求固定使用 Sunburst 人物一致性模型。"""
    monkeypatch.delenv(pi.DEFAULT_MODEL_ENV, raising=False)

    request = pi.build_request(prompt="一个年轻女性")

    assert request["model"] == "gpt-image-2.5-sunburst"
    assert request["model"] != "gpt-image-2"


def test_provider_default_model_is_image_25(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型配置页与实际请求的默认值必须同步，避免页面仍宣称旧模型。"""
    monkeypatch.delenv("AVATAR_STUDIO_GPTIMAGE_MODEL", raising=False)

    spec = config.PROVIDERS["gpt-image-2"]
    assert spec.default_model == "gpt-image-2.5-sunburst"
    view = config.public_provider_view("gpt-image-2")
    assert view["model"] == "gpt-image-2.5-sunburst"


def test_image_25_default_can_be_explicitly_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    """保留运维覆盖能力：环境变量可切换到 Flare。"""
    monkeypatch.setenv(pi.DEFAULT_MODEL_ENV, "gpt-image-2.5-flare")

    request = pi.build_request(prompt="一个年轻女性")

    assert request["model"] == "gpt-image-2.5-flare"


def test_explicit_model_argument_wins_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """探针或回归任务传入 model 时，不能被环境默认值覆盖。"""
    monkeypatch.setenv(pi.DEFAULT_MODEL_ENV, "gpt-image-2.5-flare")

    request = pi.build_request(prompt="一个年轻女性", model="gpt-image-2.5-flare")

    assert request["model"] == "gpt-image-2.5-flare"


@pytest.fixture(autouse=True)
def _clean_provider_evidence_after_test():
    """本模块写入的证据不得污染同一进程中的其它配置测试。"""
    before = {row.get("id") for row in st.all_rows("provider_evidence")}
    yield
    after = {row.get("id") for row in st.all_rows("provider_evidence")}
    added = [row_id for row_id in after - before if row_id]
    if added:
        conn = st.conn()
        conn.executemany("DELETE FROM provider_evidence WHERE id = ?", [(row_id,) for row_id in added])
        conn.commit()


def _put_image_evidence(row_id: str, **changes: object) -> None:
    row = {
        "id": row_id,
        "provider": "gpt-image-2",
        "capability": "text_to_image",
        "outcome": "succeeded",
        "checked_at": "2026-09-29T00:00:00",
        "evidence": "测试夹具，不代表真实连通",
        "detail": "",
    }
    row.update(changes)
    st.put("provider_evidence", row_id, row)


def test_legacy_image_evidence_without_model_does_not_make_sunburst_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVATAR_STUDIO_GPTIMAGE_API_KEY", "image-fixture-key")
    _put_image_evidence("pev_image_legacy_missing_model")

    check = config.check_provider("gpt-image-2", capability="text_to_image")

    assert check.model == "gpt-image-2.5-sunburst"
    assert check.state == "unverified"


def test_legacy_image_evidence_for_image_2_does_not_make_sunburst_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVATAR_STUDIO_GPTIMAGE_API_KEY", "image-fixture-key")
    _put_image_evidence("pev_image_legacy_model", model="gpt-image-2")

    check = config.check_provider("gpt-image-2", capability="text_to_image")

    assert check.state == "unverified"


def test_matching_image_model_evidence_makes_current_model_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVATAR_STUDIO_GPTIMAGE_API_KEY", "image-fixture-key")
    _put_image_evidence("pev_image_sunburst_model", model="gpt-image-2.5-sunburst")

    check = config.check_provider("gpt-image-2", capability="text_to_image")

    assert check.state == "ready"


def test_record_provider_evidence_records_current_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVATAR_STUDIO_GPTIMAGE_API_KEY", "image-fixture-key")

    record = config.record_provider_evidence(
        provider="gpt-image-2", capability="text_to_image", outcome="succeeded",
        evidence="测试夹具",
    )

    assert record["model"] == "gpt-image-2.5-sunburst"


def test_non_image_evidence_without_model_keeps_legacy_ready_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", "text-fixture-key")
    st.put("provider_evidence", "pev_text_legacy", {
        "id": "pev_text_legacy",
        "provider": "deepseek",
        "capability": "chat",
        "outcome": "succeeded",
        "checked_at": "2026-09-29T00:00:00",
        "evidence": "历史夹具",
        "detail": "",
    })

    check = config.check_provider("deepseek", capability="chat")

    assert check.state == "ready"
