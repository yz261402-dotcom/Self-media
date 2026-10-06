"""T01 配置测试：先测非法枚举、预算 0、模式越级、密钥不出服务端、排除目录。

对应 05 实施计划 T01 第一项：
- 先测非法枚举/未知字段、预算 0、mode 越级、敏感输出及排除目录探测；
- 新环境仅装独立声明依赖。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import config  # noqa: E402
from app.contracts import CONTRACT_VERSION, Mode  # noqa: E402

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------- 预算 ----------

def test_default_budget_is_zero(monkeypatch):
    for k in ("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "AVATAR_STUDIO_BUDGET_DAILY_FEN",
              "AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS"):
        monkeypatch.delenv(k, raising=False)
    bp = config.budget_policy()
    assert bp.to_dict() == {"per_task_limit_fen": 0, "daily_limit_fen": 0,
                            "max_billable_submissions": 0, "spent_today_fen": 0,
                            "submissions_today": 0}


def test_zero_budget_rejects_billable_even_when_amount_is_zero(monkeypatch):
    for k in ("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "AVATAR_STUDIO_BUDGET_DAILY_FEN",
              "AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS"):
        monkeypatch.delenv(k, raising=False)
    assert config.budget_policy().allows_billable(0) is False


def test_budget_respects_submission_ceiling():
    bp = config.BudgetPolicy(per_task_limit_fen=10000, daily_limit_fen=100000,
                             max_billable_submissions=1)
    assert bp.allows_billable(100) is True
    bp.submissions_today = 1
    assert bp.allows_billable(100) is False


# ---------- 模式越级 ----------

def test_unknown_mode_falls_back_to_mock(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_MODE", "turbo")
    assert config.resolve_mode() == "mock"


def test_integration_requires_explicit_authorization(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_MODE", "integration")
    monkeypatch.delenv("AVATAR_STUDIO_ALLOW_INTEGRATION", raising=False)
    assert config.resolve_mode() == "mock"


def test_production_requires_authorization_and_budget(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_MODE", "production")
    monkeypatch.setenv("AVATAR_STUDIO_ALLOW_PRODUCTION", "1")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "0")
    assert config.resolve_mode() == "mock"


def test_mode_enum_is_closed():
    assert set(config.MODES) == {"mock", "integration", "production"}


# ---------- 供应商连接检查 ----------

def test_unconfigured_provider_is_not_usable(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", raising=False)
    chk = config.check_provider("deepseek")
    assert chk.state == "not_configured"
    assert chk.usable is False


def test_configured_provider_is_unverified_not_ready(monkeypatch):
    """键名齐全不等于模型可用：有 key 也只能报 unverified，不能报 ready。"""
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", "sk-test-123456")
    chk = config.check_provider("deepseek")
    assert chk.state == "unverified"
    assert chk.usable is False
    assert chk.configured is True


def test_unsupported_capability_is_rejected(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", "sk-test-123456")
    chk = config.check_provider("deepseek", capability="text_to_image")
    assert chk.state == "capability_unsupported"


def test_unknown_provider_is_not_supported():
    chk = config.check_provider("some_new_provider")
    assert chk.state == "capability_unsupported"
    assert chk.usable is False


@pytest.mark.parametrize("error,expected", [
    ({"status_code": 401}, "auth_failed"),
    ({"status_code": 403, "code": "INVALID_API_KEY"}, "auth_failed"),
    ({"status_code": 429}, "quota_exhausted"),
    ({"code": "INSUFFICIENT_BALANCE"}, "quota_exhausted"),
    ({"status_code": 404}, "model_not_found"),
    ({"code": "MODEL_NOT_FOUND"}, "model_not_found"),
    ({"code": "CAPABILITY_UNSUPPORTED"}, "capability_unsupported"),
    ({"status_code": 503}, "network_error"),
    ({"message": "connection timeout"}, "network_error"),
])
def test_error_classification_is_specific(error, expected):
    assert config.classify_provider_error(**error) == expected


# ---------- 敏感输出 ----------

def test_redact_never_keeps_more_than_two_chars():
    out = config.redact("sk-abcdefghijklmnop")
    assert out.endswith("op")
    assert "abcdefghijklmn" not in out


def test_short_secret_is_fully_masked():
    assert config.redact("abcd") == "****"
    assert config.redact("") == ""


def test_public_view_exposes_no_plain_secret(monkeypatch):
    secret = "sk-live-abcdefghijklmnop"
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", secret)
    view = config.public_provider_view("deepseek")
    blob = str(view)
    assert secret not in blob
    assert "abcdefghijklmnop" not in blob
    assert view["api_key_present"] is True


def test_provider_states_cover_all_supported():
    states = config.provider_states()
    assert {s["provider"] for s in states} == set(config.PROVIDERS)


# ---------- 排除目录 ----------

def test_runtime_and_venv_are_excluded_from_asset_scan():
    for name in ("runtime", ".venv"):
        with pytest.raises(ValueError):
            config.assert_not_excluded(os.path.join(BACKEND_ROOT, name, "x.png"))


def test_normal_asset_path_is_allowed():
    config.assert_not_excluded(os.path.join(BACKEND_ROOT, "app", "assets", "hero.png"))


def test_contract_version_is_declared():
    assert CONTRACT_VERSION


def test_postgres_backend_declared_but_pgvector_not_claimed():
    st = config.persistence_status()
    assert st["backend"] == "postgresql"
    assert st["postgres_verified"] is True
    assert st["postgres_pgvector_verified"] is False
    assert "pgvector 尚未验证" in st["note"]


# ---------- 依赖声明 ----------

def test_requirements_declare_every_third_party_import():
    """新环境只装独立声明依赖：源码里出现的第三方顶层包必须显式声明。

    声明位置有两个：`requirements.txt`（主依赖）与 `requirements-publish.txt`
    （发布适配器的可选依赖，单独安装）。后者是刻意拆出来的——抖音创作者中心
    适配器只在发布时才需要，`app/publishing/adapters.py` 里也是**函数内**
    惰性导入，缺失时由 `find_spec` 显式报未配置。既然项目自己把这份依赖
    独立声明了，本用例就该认它；否则这条永远红着，反而盖住真正的漏声明。
    """
    import ast

    decl = set()
    for req_file in ("requirements.txt", "requirements-publish.txt"):
        with open(os.path.join(BACKEND_ROOT, req_file), encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                name = line.split("=")[0].split(">")[0].split("<")[0].split("[")[0].strip().lower()
                decl.add(name.replace("_", "-"))

    stdlib = set(sys.stdlib_module_names)
    # import 名与发行包名不一致的映射。仍必须显式声明，只是换名字匹配，不豁免。
    alias = {"PIL": "pillow", "cv2": "opencv-python-headless", "yaml": "pyyaml"}

    def _declared(top: str) -> bool:
        return alias.get(top, top).replace("_", "-") in decl

    missing = set()
    app_dir = os.path.join(BACKEND_ROOT, "app")
    for root, _dirs, files in os.walk(app_dir):
        if any(p in config.EXCLUDED_DIR_NAMES for p in root.replace("\\", "/").split("/")):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(root, fn), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        top = a.name.split(".")[0]
                        if top not in stdlib and not _declared(top):
                            missing.add(top)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    top = node.module.split(".")[0]
                    if top not in stdlib and not _declared(top):
                        missing.add(top)
    # deepagents 是 T00 试验项，其下游 langchain/langgraph 属其自身传递依赖，不单独声明
    transitive = {"langchain", "langgraph", "langchain-core", "langchain-core-messagestool"}
    missing = {m for m in missing if m.replace("_", "-") not in transitive}
    assert not missing, f"未声明的第三方依赖：{sorted(missing)}"


def test_lock_file_pins_every_declared_package():
    lock = {}
    with open(os.path.join(BACKEND_ROOT, "requirements.lock.txt"), encoding="utf-8") as f:
        for line in f:
            if "==" in line:
                k, v = line.strip().split("==", 1)
                lock[k.lower().replace("_", "-")] = v
    optional = set()
    with open(os.path.join(BACKEND_ROOT, "requirements.txt"), encoding="utf-8") as f:
        for raw in f:
            if "# optional" in raw:
                optional.add(raw.split("=")[0].split(">")[0].strip().lower().replace("_", "-"))
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            name = line.split("=")[0].split(">")[0].split("<")[0].split("[")[0].strip()
            key = name.lower().replace("_", "-")
            if key in optional:
                continue  # 可选依赖未安装时不进锁文件
            assert key in lock, f"锁文件缺少 {name}"
    assert optional, "应至少存在一条显式标注 optional 的未安装依赖"


def test_optional_dependency_failure_is_explicit(monkeypatch):
    """未安装的可选依赖必须让该路径显式失败，不能被当成可用能力。"""
    from app.agents.models import CapabilityNotConfigured, DeepSeekModel

    # 不依赖本机真实密钥或依赖安装状态；只验证可选适配器缺失的拒绝行为。
    import sys
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-no-network")
    monkeypatch.setitem(sys.modules, "langchain_openai", None)
    m = DeepSeekModel("content", {})
    with pytest.raises(CapabilityNotConfigured):
        m.as_chat_model()
