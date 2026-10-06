"""T03 新人物主图生成（person_image）："出图"这条链的真实性与边界。

覆盖：假 transport 下的成功/远端失败/无图/未配置；提示词与尺寸校验；
产物落盘哈希与尺寸；**出图失败不留空壳候选**；媒体路径不得越出工作区；
HTTP 层的成功与两类失败状态码。
"""
from __future__ import annotations

import base64
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app.avatar import person_image as pi  # noqa: E402
from app.avatar import registry  # noqa: E402
from app.avatar.person_image import PersonImageBlocked, PersonImageRejected  # noqa: E402
from app.contracts import HERO_ONLY_LIMITATIONS  # noqa: E402


def _png_bytes(size=(64, 128)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (200, 190, 180)).save(buf, format="PNG")
    return buf.getvalue()


def _ok_transport(image: bytes | None = None):
    image = image if image is not None else _png_bytes()

    def _t(request, timeout=0):
        return {"http_status": 200, "body": {
            "id": "ig-1",
            "data": [{"b64_json": base64.b64encode(image).decode("ascii")}],
            "usage": {"images": 1},
        }}

    return _t


# ---------- 请求构造与校验 ----------

def test_empty_prompt_rejected():
    with pytest.raises(PersonImageRejected) as e:
        pi.compose_prompt(gender="", age="", appearance="", free_text="")
    assert e.value.code == "IMAGE_PROMPT_EMPTY"


def test_unsupported_size_rejected():
    with pytest.raises(PersonImageRejected) as e:
        pi.build_request(prompt="一个年轻女性", size="800x800")
    assert e.value.code == "IMAGE_SIZE_UNSUPPORTED"


def test_count_out_of_range_rejected():
    with pytest.raises(PersonImageRejected) as e:
        pi.build_request(prompt="一个年轻女性", n=9)
    assert e.value.code == "IMAGE_COUNT_OUT_OF_RANGE"


def test_request_digest_stable_and_sensitive():
    a = pi.build_request(prompt="同一句提示词")
    b = pi.build_request(prompt="同一句提示词")
    c = pi.build_request(prompt="换了一句提示词")
    assert pi.request_digest(a) == pi.request_digest(b)
    assert pi.request_digest(a) != pi.request_digest(c)


def test_default_model_uses_image_2_5_sunburst(monkeypatch):
    monkeypatch.delenv(pi.DEFAULT_MODEL_ENV, raising=False)
    request = pi.build_request(prompt="一个年轻女性")
    assert request["model"] == "gpt-image-2.5-sunburst"


def test_image_model_can_be_overridden_for_provider_rollout(monkeypatch):
    monkeypatch.setenv(pi.DEFAULT_MODEL_ENV, "gpt-image-2.5-flare")
    request = pi.build_request(prompt="一个年轻女性")
    assert request["model"] == "gpt-image-2.5-flare"


# ---------- 产出与失败语义 ----------

def test_generate_returns_image_and_size():
    out = pi.generate(prompt="一个年轻女性，长发", transport=_ok_transport())
    assert out["image_bytes"].startswith(b"\x89PNG")
    assert (out["width"], out["height"]) == (64, 128)
    assert out["request_digest"]


def test_response_receipt_is_saved_before_result_download(monkeypatch):
    saved = []
    response = {"http_status": 200, "body": {"id": "original-image", "data": [{"url": "https://image.example/original.png"}]}}

    def unavailable_download(item, timeout=0):
        assert saved == [response]
        raise PersonImageBlocked("IMAGE_PROVIDER_UNREACHABLE", "下载生成结果失败：ReadTimeout")

    monkeypatch.setattr(pi, "_extract_bytes", unavailable_download)
    with pytest.raises(PersonImageBlocked):
        pi.generate(prompt="scene", transport=lambda *a, **kw: response,
                    on_response=lambda request, original: saved.append(original))
    assert saved == [response]


def test_real_transport_reports_network_failure_stage_without_exception_text(monkeypatch):
    import httpx
    monkeypatch.setenv(pi.KEY_ENV, "offline-secret")
    monkeypatch.setenv(pi.BASE_ENV, "https://image.example")
    client_type = httpx.Client

    def fail(request):
        raise httpx.ReadTimeout("Bearer offline-secret at private endpoint")

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(fail), **kwargs))
    with pytest.raises(PersonImageBlocked) as exc:
        pi.generate(prompt="scene")
    assert exc.value.code == "IMAGE_PROVIDER_UNREACHABLE"
    assert exc.value.detail == {"stage": "generation", "exception_type": "ReadTimeout"}
    assert "offline-secret" not in exc.value.message


def test_result_download_reports_stage_without_exception_text(monkeypatch):
    import httpx
    client_type = httpx.Client

    def fail(request):
        raise httpx.ReadError("url contains private credential")

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(fail), **kwargs))
    with pytest.raises(PersonImageBlocked) as exc:
        pi._extract_bytes({"url": "https://image.example/result.png"})
    assert exc.value.detail == {"stage": "download", "exception_type": "ReadError"}
    assert "private credential" not in exc.value.message


def test_remote_http_error_is_blocked_and_retryable():
    def _t(request, timeout=0):
        return {"http_status": 500, "body": {"error": {"message": "boom"}}}

    with pytest.raises(PersonImageBlocked) as e:
        pi.generate(prompt="一个年轻女性", transport=_t)
    assert e.value.code == "IMAGE_PROVIDER_HTTP_ERROR"
    assert e.value.retryable is True


def test_no_image_in_response_is_blocked():
    def _t(request, timeout=0):
        return {"http_status": 200, "body": {"data": []}}

    with pytest.raises(PersonImageBlocked) as e:
        pi.generate(prompt="一个年轻女性", transport=_t)
    assert e.value.code == "IMAGE_PROVIDER_NO_IMAGE"


def test_unconfigured_transport_does_not_touch_network(monkeypatch):
    monkeypatch.delenv(pi.KEY_ENV, raising=False)
    monkeypatch.delenv(pi.BASE_ENV, raising=False)

    def _explode(*a, **k):
        raise AssertionError("未配置时不得发起任何网络请求")

    monkeypatch.setattr(pi, "httpx_transport", pi.httpx_transport)  # 保留真实实现的引用
    out = None
    try:
        out = pi.httpx_transport(pi.build_request(prompt="x"))
    except PersonImageBlocked as e:
        assert e.code == "IMAGE_PROVIDER_NOT_CONFIGURED"
        assert e.retryable is False
    assert out is None


# ---------- 候选 + 资产登记 ----------

@pytest.fixture()
def isolated_workspace(monkeypatch):
    """把产物导向会话内临时目录。

    不用 pytest 的 tmp_path：本机对系统 temp 目录的扫描被拒绝访问
    （PermissionError: pytest-of-22346），会连 fixture 都起不来。
    """
    path = os.path.join(ROOT, "tests", "tmp_media_root")
    os.makedirs(path, exist_ok=True)
    monkeypatch.setenv(pi.WORKSPACE_ENV, path)
    return path


def test_generate_candidate_hero_registers_pending_asset(isolated_workspace):
    result = pi.generate_candidate_hero(gender="女", age="22 岁左右",
                                        appearance="长发、温柔、专业", transport=_ok_transport())
    ident, asset = result["identity"], result["asset"]

    abs_path = os.path.join(str(isolated_workspace), asset["storage_uri"])
    assert os.path.exists(abs_path)
    assert asset["file_hash"].startswith("sha256:")
    assert len(asset["file_hash"]) == 7 + 64
    assert (asset["width"], asset["height"]) == (64, 128)
    # 出图不等于合格：资产与身份都还是 pending
    assert asset["approval_state"] == "pending"
    assert ident["approval_state"] == "pending"
    assert ident["frozen"] is False
    assert result["license"]["scope"] == "unverified"
    # 文件名就是资产 id，便于按 id 回查文件
    assert os.path.basename(asset["storage_uri"]) == asset["id"] + ".png"


def test_failed_generation_leaves_nothing_behind(isolated_workspace):
    before_ids = {i["id"] for i in registry.list_identities()}
    generated_root = os.path.join(str(isolated_workspace), *pi.GENERATED_ASSETS_REL.split("/"))
    before_dirs = set(os.listdir(generated_root)) if os.path.isdir(generated_root) else set()

    def _t(request, timeout=0):
        return {"http_status": 429, "body": {"error": {"message": "rate limited"}}}

    with pytest.raises(PersonImageBlocked):
        pi.generate_candidate_hero(gender="女", age="25 岁", appearance="短发", transport=_t)

    # 既不留候选身份，也不留任何目录/文件
    assert {i["id"] for i in registry.list_identities()} == before_ids
    after_dirs = set(os.listdir(generated_root)) if os.path.isdir(generated_root) else set()
    assert after_dirs == before_dirs


def test_missing_identity_is_rejected_not_blocked():
    with pytest.raises(PersonImageRejected) as e:
        pi.generate_candidate_hero(identity_id="av_not_exist", gender="女",
                                   transport=_ok_transport())
    assert e.value.code == "IDENTITY_NOT_FOUND"


def test_existing_candidate_can_regenerate(isolated_workspace):
    first = pi.generate_candidate_hero(gender="女", age="22 岁", appearance="长发",
                                       transport=_ok_transport())
    second = pi.generate_candidate_hero(identity_id=first["identity"]["id"],
                                        gender="女", age="22 岁", appearance="长发",
                                        transport=_ok_transport(_png_bytes((64, 96))))
    assert second["identity"]["id"] == first["identity"]["id"]
    assert second["asset"]["id"] != first["asset"]["id"]


# ---------- 媒体路径安全 ----------

def test_media_path_rejects_escape_and_absolute(isolated_workspace):
    assert pi.resolve_media_path("../../Windows/System32/config/SAM") is None
    assert pi.resolve_media_path("/etc/passwd") is None
    assert pi.resolve_media_path("C:/Windows/win.ini") is None
    assert pi.resolve_media_path("assets/a.txt") is None


def test_media_path_accepts_generated_png(isolated_workspace):
    result = pi.generate_candidate_hero(gender="女", age="22 岁", appearance="长发",
                                        transport=_ok_transport())
    resolved = pi.resolve_media_path(result["asset"]["storage_uri"])
    assert resolved and os.path.exists(resolved)


# ---------- HTTP 层 ----------

def test_http_endpoint_returns_image_url(monkeypatch, isolated_workspace):
    from fastapi.testclient import TestClient

    import app.main as main_mod

    monkeypatch.setattr(main_mod, "_image_transport", _ok_transport)
    client = TestClient(main_mod.app)
    resp = client.post("/api/v1/avatar-identities/candidates/with-image",
                       json={"gender": "女", "age": "22 岁左右", "appearance": "长发、温柔"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["image_url"].startswith("/api/v1/media?uri=")
    assert body["identity"]["approval_state"] == "pending"
    assert len(body["warnings"]) == 3

    media = client.get(body["image_url"])
    assert media.status_code == 200
    assert media.content.startswith(b"\x89PNG")


def test_http_endpoint_maps_failures(monkeypatch, isolated_workspace):
    from fastapi.testclient import TestClient

    import app.main as main_mod

    monkeypatch.setattr(main_mod, "_image_transport",
                        lambda: (_ for _ in ()).throw(pi.PersonImageRejected("IMAGE_PROMPT_EMPTY", "空")))
    client = TestClient(main_mod.app)
    bad = client.post("/api/v1/avatar-identities/candidates/with-image", json={})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "IMAGE_PROMPT_EMPTY"

    # 参数在调用前求值，所以要先把 _image_transport 恢复成 None，再换替身模块
    monkeypatch.setattr(main_mod, "_image_transport", lambda: None)
    monkeypatch.setattr(main_mod, "person_image", _BlockedModule())
    resp = client.post("/api/v1/avatar-identities/candidates/with-image", json={"gender": "女"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IMAGE_PROVIDER_HTTP_ERROR"
    assert resp.json()["error"]["retryable"] is True


def test_generated_hero_is_linked_back_to_identity(isolated_workspace):
    """主图必须挂回身份。

    曾经只登记了资产没写回 `hero_asset_id`，结果是：ready 检查一直报"缺少定妆原片资产"、
    冻结要靠人补 id——现象离根因很远。这里把它钉成接口的一部分。
    """
    result = pi.generate_candidate_hero(gender="女", age="22 岁左右",
                                        appearance="长发、温柔、专业", transport=_ok_transport())
    ident, asset = result["identity"], result["asset"]

    assert ident["hero_asset_id"] == asset["id"]
    assert ident["license_id"] == result["license"]["id"]

    # 挂回去的必须是能被 get_asset 读到的同一条（不是只改了返回体）
    assert registry.get_identity(ident["id"])["hero_asset_id"] == asset["id"]

    ok, reasons = registry.production_ready(ident["id"])
    assert ok is False  # 仍未定版当然不 ready
    assert "缺少定妆原片资产" not in reasons


def test_frozen_identity_refuses_new_hero(isolated_workspace):
    """已定版的人物不能被重新出图换脸——换人必须显式产生新版本。"""
    result = pi.generate_candidate_hero(gender="女", age="22 岁", appearance="长发",
                                        transport=_ok_transport())
    ident, asset = result["identity"], result["asset"]

    registry.record_review(asset_id=asset["id"], reviewer="user", outcome="pass",
                           identity_digest="sha256:" + "a" * 64,
                           method="人工目检：与期望人设一致")
    registry.set_reference_policy(ident["id"], "hero_only", "user",
                                  reason="AI 凭空生成，不可能存在多角度真值",
                                  acknowledged=list(HERO_ONLY_LIMITATIONS))
    registry.freeze_identity(identity_id=ident["id"], hero_asset_id=asset["id"],
                             identity_digest="sha256:" + "a" * 64, reviewer="user")

    with pytest.raises(PersonImageRejected) as e:
        pi.generate_candidate_hero(identity_id=ident["id"], gender="女", appearance="短发",
                                   transport=_ok_transport())
    assert e.value.code == "IDENTITY_FROZEN"


class _BlockedModule:
    """让端点走 blocked 分支的最小替身，避免为此真的去触网。"""

    DEFAULT_SIZE = pi.DEFAULT_SIZE

    @staticmethod
    def generate_candidate_hero(**kwargs):
        raise PersonImageBlocked("IMAGE_PROVIDER_HTTP_ERROR", "远端 500", retryable=True)
