"""密钥加载器测试：验证 VOLC AK/SK 与通用单 Key 供应商均能正确注入。

不写入任何真实密钥；仅用占位值验证注入逻辑。
用 tempfile 自建临时目录，避免依赖 pytest 的 tmp_path fixture（在满跑场景下
偶尔因权限问题 setup 失败）。
"""
import os
import shutil
import tempfile
import textwrap

from app.config import load_secrets_from_file

VOLC_AK = "AVATAR_STUDIO_VOLC_ACCESS_KEY"
VOLC_SK = "AVATAR_STUDIO_VOLC_SECRET_KEY"
DEEPSEEK = "AVATAR_STUDIO_DEEPSEEK_API_KEY"


def _write_keys(tmpdir, body: str) -> str:
    path = os.path.join(tmpdir, "keys.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body).strip() + "\n")
    return path


def _clean(monkeypatch):
    for v in (VOLC_AK, VOLC_SK, DEEPSEEK):
        monkeypatch.delenv(v, raising=False)


def test_load_secrets_injects_volc_ak_sk_and_generic(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="secrets_")
    try:
        _clean(monkeypatch)
        path = _write_keys(tmp, """
            **deepseek**: sk-test-deepseek
            **volc-ak**: AKIDTEST123
            **volc-sk**: SKSECRETTEST456
        """)
        injected = load_secrets_from_file(path)
        assert VOLC_AK in injected
        assert VOLC_SK in injected
        assert DEEPSEEK in injected
        assert os.environ.get(VOLC_AK) == "AKIDTEST123"
        assert os.environ.get(VOLC_SK) == "SKSECRETTEST456"
        assert os.environ.get(DEEPSEEK) == "sk-test-deepseek"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_secrets_volc_keys_with_spaces(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="secrets_")
    try:
        _clean(monkeypatch)
        path = _write_keys(tmp, "**volc-ak** : AKIDSPACED\n**volc-sk** : SKSPACED\n")
        load_secrets_from_file(path)
        assert os.environ.get(VOLC_AK) == "AKIDSPACED"
        assert os.environ.get(VOLC_SK) == "SKSPACED"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_secrets_ignores_unknown_provider(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="secrets_")
    try:
        _clean(monkeypatch)
        path = _write_keys(tmp, "**some-unknown-provider**: abc\n**volc-ak**: X\n")
        injected = load_secrets_from_file(path)
        assert VOLC_AK in injected
        assert os.environ.get(VOLC_AK) == "X"
        assert not any(i.startswith("AVATAR_STUDIO_SOME") for i in injected)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_secrets_handles_underscore_form(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="secrets_")
    try:
        _clean(monkeypatch)
        path = _write_keys(tmp, "**volc_ak**: AKUNDER\n**volc_sk**: SKUNDER\n")
        load_secrets_from_file(path)
        assert os.environ.get(VOLC_AK) == "AKUNDER"
        assert os.environ.get(VOLC_SK) == "SKUNDER"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_secrets_omnihuman_block_fullwidth_colon_and_zerowidth(monkeypatch):
    """复刻真实密钥文件写法：中文全角冒号、AK 内联于区块头、SK 续行以零宽空格+tab 起头。

    这正是此前漏加载的根因——`str.strip()` 不会移除 U+200B 零宽空格，
    导致续行 `SK：...` 无法被 `^(AK|SK)` 正则命中。
    """
    tmp = tempfile.mkdtemp(prefix="secrets_")
    try:
        _clean(monkeypatch)
        zw = "\u200b"
        body = ("**OmniHuman1.5**：AK：AKLTtestAK\n"
                + zw + "\t\t\t      SK：SKtestSECRET==\n")
        path = os.path.join(tmp, "keys.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        injected = load_secrets_from_file(path)
        assert VOLC_AK in injected
        assert VOLC_SK in injected
        assert os.environ.get(VOLC_AK) == "AKLTtestAK"
        assert os.environ.get(VOLC_SK) == "SKtestSECRET=="
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_load_secrets_minmax_maps_to_minimax(monkeypatch):
    """真实文件用 `**minmax**`（少了 i）命名，应映射到 MINIMAX 供应商 key。"""
    tmp = tempfile.mkdtemp(prefix="secrets_")
    try:
        _clean(monkeypatch)
        path = _write_keys(tmp, "**minmax**：sk-api-test-minimax-key\n")
        injected = load_secrets_from_file(path)
        assert "AVATAR_STUDIO_MINIMAX_API_KEY" in injected
        assert os.environ.get("AVATAR_STUDIO_MINIMAX_API_KEY") == "sk-api-test-minimax-key"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
