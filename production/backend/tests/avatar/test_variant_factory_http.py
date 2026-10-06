"""F02 换装/换景变体的生产链（HTTP 级）。

为什么需要这组用例：
  `compose.py` 是"身份零漂移"唯一的产出路径，但它此前**没有任何 HTTP 入口**——
  界面上的「生成变体」从来不产出资产（端点要 `storage_uri`+`file_hash`，而界面不发文件），
  于是底片那一步必然卡 `VARIANT_NOT_APPROVED`，且从界面完全看不出缺的是"生产者"。
  这组用例把这条链钉死：mask 草稿 → 人目视确认 → 确定性合成 → 待审变体 → 准入通过。

同时钉住两类语义**不合并**（与 T04/T05 同约定）：
  · 请求本身不成立（edge_mode 未知、画框越界、mask 尺寸不符）→ 422，改输入才有意义；
  · 当前条件不具备（mask 未目视确认、背景权属未核验）→ 409 + retryable，等人补条件。
  把后者报成 422，用户会一直去调参数，调多少遍都不会通。

夹具说明：图像是程序生成的合成图（渐变背景 + 深色椭圆模拟人物），
权属记录用真实的人工确认端点（`/avatar-licenses/{id}/attest`）置为可商用，
而不是直接把 scope 写成 commercial_ok —— 这样测的是**真实的人确认路径**。
"""
from __future__ import annotations

import hashlib
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402
from app.avatar import licensing, registry  # noqa: E402

W, H = 400, 600
HARD = [[120, 180, 280, 420]]
ATTEST_TOKEN = "COMMERCIAL_ATTEST_V1"
FIXTURE_USER = "user:fixture-reviewer"


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def tmp_path():
    """项目内临时目录：本机系统临时目录不可写（WinError 5），沿用既有用例的做法。"""
    import shutil

    d = os.path.join(ROOT, "tests", ".tmp_variant_factory", st.new_id("t"))
    os.makedirs(d, exist_ok=True)
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except SystemExit:
            # 本机 CLI 的删除保护 shim 在"单轮删除数超阈值"时 raise SystemExit(1)，
            # ignore_errors=True 拦不住 SystemExit；清理失败不得把测试判成失败。
            pass


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """产物目录 → 临时目录。读写根共用同一覆盖（见 `compose._workspace_root`）。"""
    ws = os.path.join(str(tmp_path), "ws")
    os.makedirs(ws, exist_ok=True)
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", ws)
    return ws


@pytest.fixture()
def http():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _source_image():
    """浅灰渐变背景 + 中心深色椭圆（模拟"人物"）。必须有低频背景供曲面拟合。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), (205, 208, 214))
    d = ImageDraw.Draw(img)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=(200 + y // 30, 203 + y // 30, 209 + y // 30))
    d.ellipse([110, 120, 290, 480], fill=(46, 48, 58))
    return img


def _background_image(color=(52, 96, 178)):
    from PIL import Image

    return Image.new("RGB", (W, H), color)


def _err(resp) -> str:
    body = resp.json()
    assert "error" in body, f"错误响应必须是 {{error: ...}} 结构：{body}"
    return body["error"]["code"]


def _setup(tmp_path, *, bg_scene="浅灰蓝影棚"):
    """建一个人物：定妆原图 + 背景底片 + 背景权属记录（默认 unverified）。

    原图/背景用**绝对路径**登记（与 `test_compose.py` 同一做法）：这两张是"外部素材"，
    而合成产物与 mask 必须写进工作区相对路径，才可能被 `/api/v1/media` 读出来。
    """
    hero_png = _png(_source_image())
    bg_png = _png(_background_image())
    hero_path = os.path.join(str(tmp_path), "hero.png")
    bg_path = os.path.join(str(tmp_path), "bg.png")
    with open(hero_path, "wb") as f:
        f.write(hero_png)
    with open(bg_path, "wb") as f:
        f.write(bg_png)

    ident = registry.create_identity_candidate(
        name="夹具人物", persona="合成图夹具；仅用于验证变体生产链，不代表任何真人",
        note="夹具构造", origin="generated")
    ident_id = ident["id"]

    hero = registry.register_asset(
        identity_id=ident_id, kind="hero", storage_uri=hero_path,
        file_hash=_sha(hero_png), source_evidence="夹具构造：程序生成的合成图",
        width=W, height=H)
    lic = licensing.register(
        subject=f"背景素材 {bg_scene}（夹具）", kind="image_source", scope="unverified",
        evidence="夹具构造：程序生成，未取得任何授权材料")
    bg = registry.register_asset(
        identity_id=ident_id, kind="background", storage_uri=bg_path,
        file_hash=_sha(bg_png), source_evidence="夹具构造：程序生成的纯色背景",
        license_id=lic["id"], width=W, height=H)

    # 原图挂回身份：compose 只从 identity.hero_asset_id 取源图
    ident = registry.get_identity(ident_id)
    ident["hero_asset_id"] = hero["id"]
    st.put("identities", ident_id, ident)
    return {"identity_id": ident_id, "hero": hero, "background": bg, "license": lic,
            "hero_path": hero_path}


def _attest(http, license_id: str) -> None:
    """走真实的人工确认端点把背景置为可商用（不是直接改 scope）。

    刻意不直接把 `scope` 写成 commercial_ok：那样测的就不是产品里的人确认路径了，
    而"权属怎么变成可商用"正是这条链里最容易被绕过的一步。
    """
    resp = http.post(f"/api/v1/avatar-licenses/{license_id}/attest",
                     json={"reviewer": FIXTURE_USER,
                           "basis": "夹具构造：程序生成素材，仅供测试使用",
                           "ack_token": ATTEST_TOKEN})
    assert resp.status_code == 200, resp.text
    assert resp.json()["license"]["scope"] == "commercial_ok"
    assert licensing.ATTEST_TOKEN == ATTEST_TOKEN, "确认标记版本变化须同步更新本用例"


def _draft_mask(http, ident_id: str) -> dict:
    resp = http.post(f"/api/v1/identities/{ident_id}/masks",
                     json={"hard_protect": HARD})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _confirmed_mask_asset_id(http, ident_id: str) -> str:
    draft = _draft_mask(http, ident_id)
    asset_id = draft["asset"]["id"]
    resp = http.post(f"/api/v1/avatar-masks/{asset_id}/confirm",
                     json={"reviewer": FIXTURE_USER, "outcome": "pass",
                           "note": "夹具：人工目视确认轮廓"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "confirmed"
    return asset_id


def _compose(http, ident_id: str, mask_asset_id: str, bg_asset_id: str, **over):
    payload = {"mask_asset_id": mask_asset_id, "background_asset_id": bg_asset_id,
               "edge_mode": "unmix", "outfit": "news_suit", "background_label": "影棚"}
    payload.update(over)
    return http.post(f"/api/v1/identities/{ident_id}/variants/compose", json=payload)


# ---------------------------------------------------------------- mask 草稿与确认

def test_mask_is_registered_as_draft_only(http, tmp_path):
    """自动分割只产出草稿，且明确拒绝"指标全绿即定版"。"""
    ctx = _setup(tmp_path)
    body = _draft_mask(http, ctx["identity_id"])
    assert body["state"] == "draft"
    assert body["mask_spec"]["confirm_outcome"] is None
    assert body["asset"]["kind"] == "mask"
    assert body["asset"]["approval_state"] == "pending"
    assert body["usable"] is True  # "没触发已知失效"，不等于"质量合格"
    assert any("未经人工目视确认" in w for w in body["warnings"])


def test_compose_blocks_until_mask_is_confirmed_by_human(http, tmp_path):
    ctx = _setup(tmp_path)
    _attest(http, ctx["license"]["id"])
    draft = _draft_mask(http, ctx["identity_id"])

    resp = _compose(http, ctx["identity_id"], draft["asset"]["id"], ctx["background"]["id"])
    # 未确认 → 409（等人补条件），不是 422（请求写错了）
    assert resp.status_code == 409, resp.text
    assert _err(resp) == "MASK_NOT_CONFIRMED"
    assert resp.json()["error"]["retryable"] is True


def test_mask_confirm_rejects_non_human_actor(http, tmp_path):
    """mask 定版的权限来自"人"本身：系统不得自行核发。"""
    ctx = _setup(tmp_path)
    draft = _draft_mask(http, ctx["identity_id"])
    resp = http.post(f"/api/v1/avatar-masks/{draft['asset']['id']}/confirm",
                     json={"reviewer": "agent:auto-mask", "outcome": "pass"})
    assert resp.status_code == 409, resp.text
    assert _err(resp) == "ACTOR_NOT_USER"
    assert resp.json()["error"]["retryable"] is False
    # 未核发即未确认：草稿状态不得被改动
    assert registry.get_asset(draft["asset"]["id"]) is not None
    assert http.get(f"/api/v1/identities/{ctx['identity_id']}/masks").json()[
        "masks"][0]["state"] == "draft"


# ---------------------------------------------------------------- 权属与合成

def test_compose_blocks_when_background_license_unverified(http, tmp_path):
    """背景权属未核验 → 409（等人确认权属），不是 422。"""
    ctx = _setup(tmp_path)
    mask_id = _confirmed_mask_asset_id(http, ctx["identity_id"])
    resp = _compose(http, ctx["identity_id"], mask_id, ctx["background"]["id"])
    assert resp.status_code == 409, resp.text
    assert _err(resp) == "LICENSE_UNVERIFIED"
    assert resp.json()["error"]["retryable"] is True


def test_unknown_edge_mode_is_422_not_409(http, tmp_path):
    """参数写错与条件不具备必须能被区分开。"""
    ctx = _setup(tmp_path)
    _attest(http, ctx["license"]["id"])
    mask_id = _confirmed_mask_asset_id(http, ctx["identity_id"])
    resp = _compose(http, ctx["identity_id"], mask_id, ctx["background"]["id"],
                    edge_mode="magic")
    assert resp.status_code == 422, resp.text
    assert _err(resp) == "EDGE_MODE_UNSUPPORTED"


def test_composed_variant_is_bit_exact_in_hard_protect(http, tmp_path, workspace):
    """整件事的核心断言：产物在硬保护区内与源图**逐像素相同**。

    这里不读接口自报的结论，而是把落盘产物读回来自己比一遍——
    自报值只能证明"函数认为相等"，读文件才能证明"文件真的没被动过"。
    """
    import numpy as np
    from PIL import Image

    ctx = _setup(tmp_path)
    _attest(http, ctx["license"]["id"])
    mask_id = _confirmed_mask_asset_id(http, ctx["identity_id"])

    resp = _compose(http, ctx["identity_id"], mask_id, ctx["background"]["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["registered"] is True
    assert body["pixel_identical"] is True
    assert body["max_channel_diff"] == 0
    assert body["asset"]["kind"] == "variant"
    assert body["asset"]["approval_state"] == "pending"

    # 独立复核：源图与产物在硬保护区内必须完全一致（person_scale=1.0 → 偏移 0）
    out_path = os.path.join(workspace, body["storage_uri"])
    assert os.path.exists(out_path), "产物必须真的落盘，否则 /api/v1/media 读不到"
    src = np.array(Image.open(ctx["hero_path"]).convert("RGB")).astype(int)
    out = np.array(Image.open(out_path).convert("RGB")).astype(int)
    x0, y0, x1, y1 = HARD[0]
    diff = np.abs(src[y0:y1, x0:x1] - out[y0:y1, x0:x1])
    assert int(diff.max()) == 0, f"硬保护区内被改动了，最大通道差 {int(diff.max())}"

    # 产物要能被只读媒体通道取出来（否则界面显示不了）
    media = http.get(body["image_url"])
    assert media.status_code == 200
    assert len(media.content) == len(open(out_path, "rb").read())

    # 来源链必须写明派生自哪张原图，且不得声称做过身份识别
    ev = body["asset"]["source_evidence"]
    assert ctx["hero"]["id"] in ev
    assert "未做任何身份识别" in ev


def test_preview_commit_false_registers_nothing(http, tmp_path, workspace):
    """commit=False 只给预览：不登记资产，避免"先造垃圾再清理"。"""
    ctx = _setup(tmp_path)
    _attest(http, ctx["license"]["id"])
    mask_id = _confirmed_mask_asset_id(http, ctx["identity_id"])
    before = len(st.all_rows("avatar_assets"))

    resp = _compose(http, ctx["identity_id"], mask_id, ctx["background"]["id"],
                    commit=False)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["registered"] is False
    assert "preview_id" in body
    assert len(st.all_rows("avatar_assets")) == before, "预览不得登记资产"
    assert os.path.exists(os.path.join(workspace, body["storage_uri"]))


# ---------------------------------------------------------------- 与准入/底片衔接

def test_composed_variant_can_pass_identity_admission(http, tmp_path, workspace):
    """闭环：合成 → 登记变体 → 人工审核 pass → 准入通过。

    这条链此前是断的：拿不出合格变体，"底片"就永远卡 `VARIANT_NOT_APPROVED`。
    """
    ctx = _setup(tmp_path)
    _attest(http, ctx["license"]["id"])
    ident_id, bg_id = ctx["identity_id"], ctx["background"]["id"]
    mask_id = _confirmed_mask_asset_id(http, ident_id)

    composed = _compose(http, ident_id, mask_id, bg_id)
    assert composed.status_code == 200, composed.text
    variant_asset_id = composed.json()["asset"]["id"]

    # 身份基准摘要：准入会把审核摘要与它比对（夹具直接写入，不做真实比对）
    ident = registry.get_identity(ident_id)
    ident["identity_digest"] = ctx["hero"]["file_hash"]
    st.put("identities", ident_id, ident)
    digest = ident["identity_digest"]

    work = http.post("/api/v1/works", json={"topic": "夹具：变体准入"}).json()
    stored = st.get("works", work["id"])
    stored["identity_id"] = ident_id
    st.put("works", work["id"], stored)

    # 登记 AI 变体时不再重复登记一份资产（否则同一张图会有两个 id，来源链断掉）
    assets_before = len(st.all_rows("avatar_assets"))
    made = http.post(f"/api/v1/works/{work['id']}/variants",
                     json={"asset_id": variant_asset_id, "outfit": "news_suit",
                           "background": "影棚", "identity_digest": digest})
    assert made.status_code == 200, made.text
    variant = made.json()
    assert variant["asset_id"] == variant_asset_id
    assert len(st.all_rows("avatar_assets")) == assets_before

    admit = lambda style: http.post(  # noqa: E731
        f"/api/v1/works/{work['id']}/variants/{variant['id']}/admit", json={"style": style})

    # 反例先跑：没声明画风 → 不得判 pass（"无法判断"不等于"通过"）
    negative = admit("")
    assert negative.status_code == 200
    assert negative.json()["ok"] is False
    assert any("未声明画风" in r for r in negative.json()["reasons"])

    # 人工审核 pass：审核摘要必须与冻结基准一致，否则按换脸候选拒收
    reviewed = http.post(f"/api/v1/identities/{ident_id}/assets/{variant_asset_id}/review",
                         json={"reviewer": FIXTURE_USER, "outcome": "pass",
                               "identity_digest": digest, "method": "人工对照"})
    assert reviewed.status_code == 200, reviewed.text
    assert registry.get_asset(variant_asset_id)["approval_state"] == "approved"

    ok = admit("photoreal")
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True, ok.json()["reasons"]

    # 换一份不匹配的摘要再判：必须是换脸候选，不得凭"已 approved"放行
    http.post(f"/api/v1/identities/{ident_id}/assets/{variant_asset_id}/review",
              json={"reviewer": FIXTURE_USER, "outcome": "pass",
                    "identity_digest": "sha256:" + "0" * 64, "method": "人工对照"})
    drifted = admit("photoreal")
    assert drifted.status_code == 200
    assert drifted.json()["ok"] is False
    assert any("换脸候选" in r for r in drifted.json()["reasons"])
