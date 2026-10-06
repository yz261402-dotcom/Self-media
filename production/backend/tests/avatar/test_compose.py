"""T03 确定性抠图合成测试：硬保证（逐像素）与全部门禁。

重点不是"算法好不好看"，而是：
  · 硬保护区内像素必须**严格**相同（差 1 即失败），这是"身份零漂移"的可验证依据；
  · mask 与源图绑定，源文件一换即失效；
  · 未确认的自动草稿不得产出 production_ready 产物；
  · 模块不得声称做了身份识别。
"""
from __future__ import annotations

import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402
from app.avatar import compose, licensing, registry  # noqa: E402


@pytest.fixture()
def tmp_path():
    """项目内临时目录：Windows 系统临时目录在本环境不可写（WinError 5）。"""
    import shutil

    d = os.path.join(ROOT, "tests", ".tmp_compose", st.new_id("t"))
    os.makedirs(d, exist_ok=True)
    try:
        yield d
    finally:
        # 清理临时目录绝不能把测试判成失败，所以这里显式吞掉两类异常：
        #   · Exception —— 文件被占用等常规清理失败（ignore_errors=True 已覆盖）
        #   · SystemExit —— 本机 CLI 的删除保护 shim 会在"单轮删除数超阈值"时
        #     直接 raise SystemExit(1)。SystemExit 不是 Exception，
        #     ignore_errors=True **拦不住**它，于是 22 个用例被记为 teardown error，
        #     看起来像产品缺陷。实测：单独跑 test_compose.py 全绿，
        #     只有全量跑（删除计数累积）才出现——是环境交互，不是代码问题。
        # 不吞 KeyboardInterrupt：那是用户主动中断，必须继续向上传播。
        try:
            shutil.rmtree(d, ignore_errors=True)
        except SystemExit:
            pass

W, H = 400, 600
HARD = [(120, 180, 280, 420)]


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _source_image():
    """浅灰渐变背景 + 中心深色椭圆（模拟"人物"）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), (205, 208, 214))
    d = ImageDraw.Draw(img)
    for y in range(H):  # 轻微竖直渐变，供背景曲面拟合
        d.line([(0, y), (W, y)], fill=(200 + y // 30, 203 + y // 30, 209 + y // 30))
    d.ellipse([110, 120, 290, 480], fill=(46, 48, 58))
    return img


def _background_image(color=(52, 96, 178)):
    from PIL import Image

    return Image.new("RGB", (W, H), color)


def _write(tmp_path, name, img) -> str:
    """落盘并返回相对工作区的 URI。"""
    path = os.path.join(str(tmp_path), name)
    img.save(path)
    return path


def _setup(tmp_path, tag: str, hero_img=None, bg_color=(52, 96, 178)):
    """建身份 + hero 资产 + 背景资产（权属商用已核验）。"""
    ident = registry.create_identity_candidate(name=f"测试人物-{tag}", persona="p")
    src_path = _write(tmp_path, f"hero_{tag}.png",
                      hero_img if hero_img is not None else _source_image())
    image_lic = licensing.register(subject=f"测试主图-{tag}", kind="image_source",
                                   scope="commercial_ok", evidence="项目自有素材",
                                   verified_by="tester")
    hero = registry.register_asset(identity_id=ident["id"], kind="hero", storage_uri=src_path,
                                   file_hash=compose._sha(open(src_path, "rb").read()),
                                   source_evidence="测试素材", license_id=image_lic["id"],
                                   width=W, height=H)
    bg_path = _write(tmp_path, f"bg_{tag}.png", _background_image(bg_color))
    bg_lic = licensing.register(subject=f"测试背景-{tag}", kind="image_source",
                                scope="commercial_ok", evidence="项目自有素材",
                                verified_by="tester")
    bg = registry.register_asset(identity_id=ident["id"], kind="background",
                                 storage_uri=bg_path,
                                 file_hash=compose._sha(open(bg_path, "rb").read()),
                                 source_evidence="测试背景", license_id=bg_lic["id"],
                                 width=W, height=H)
    return ident, hero, bg


def _geometric_mask(fg_box, invert=False):
    """用几何区域造 mask（不依赖算法，便于精确断言）。"""
    from PIL import Image, ImageDraw

    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    if invert:
        d.rectangle([0, 0, W, H], fill=255)
        d.ellipse(fg_box, fill=0)
    else:
        d.ellipse(fg_box, fill=255)
    return m


def _register_mask(tmp_path, tag, ident, hero, mask_img, origin="manual"):
    mask_bytes = _png(mask_img)
    path = os.path.join(str(tmp_path), f"mask_{tag}.png")
    with open(path, "wb") as f:
        f.write(mask_bytes)
    return compose.register_mask(
        identity_id=ident["id"], source_asset_id=hero["id"],
        mask_binary_png=mask_bytes, mask_alpha_png=mask_bytes,
        params={"hard_protect": [list(b) for b in HARD], "method": "manual"},
        origin=origin, storage_uri=path, source_evidence="测试 mask",
    )


# ------------------------------------------------------------ 硬保证


def test_hard_protect_region_is_pixel_identical(tmp_path):
    """核心保证：硬保护区内像素与源图逐像素相同——这是身份零漂移的可验证依据。"""
    ident, hero, bg = _setup(tmp_path, "hard")
    # mask 只保留一个偏离人脸的圆（模拟"算法把人物切掉了"）
    spec = _register_mask(tmp_path, "hard", ident, hero, _geometric_mask([20, 20, 90, 90]))
    compose.confirm_mask(spec["mask_asset_id"], reviewer="user", outcome="pass", note="目视通过")
    out = compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"])
    assert out["hard_protect_check"]["pixel_identical"] is True
    for region in out["hard_protect_check"]["regions"]:
        assert region["max_channel_diff"] == 0


def test_verify_preservation_is_strict_zero_not_threshold():
    """差 1 个通道值也必须判失败——不允许"近似相同"。"""
    from PIL import Image

    src = _png(_source_image())
    near = _source_image()
    near.putpixel((200, 300), (47, 48, 58))  # 原为 (46,48,58)，仅 +1
    out = compose.verify_preservation(src, _png(near), HARD)
    assert out["pixel_identical"] is False
    assert out["regions"][0]["max_channel_diff"] == 1


def test_verify_preservation_detects_change_inside_region():
    from PIL import Image

    src_png = _png(_source_image())
    altered = _source_image()
    altered.putpixel((150, 200), (255, 0, 0))
    assert compose.verify_preservation(src_png, _png(altered), HARD)["pixel_identical"] is False


def test_verify_preservation_ignores_change_outside_region():
    """保护区外的改动不算身份漂移——换背景本来就要改那里。"""
    src_png = _png(_source_image())
    altered = _source_image()
    altered.putpixel((10, 10), (0, 0, 0))
    assert compose.verify_preservation(src_png, _png(altered), HARD)["pixel_identical"] is True


def test_module_declares_no_identity_recognition():
    """模块自身声明不做身份识别；硬保护区默认值必须有效（紧贴身份特征）。"""
    assert "不做身份识别" in compose.__doc__
    x0, y0, x1, y1 = compose.DEFAULT_HARD_PROTECT[0]
    assert x1 > x0 and y1 > y0
    assert compose.MIN_SEPARABILITY_P10 == 12.0


# ------------------------------------------------------------ mask 绑定与失效


def test_mask_size_mismatch_rejected(tmp_path):
    from PIL import Image

    ident, hero, _bg = _setup(tmp_path, "size")
    small = Image.new("L", (W // 2, H // 2), 255)
    with pytest.raises(compose.ComposeRejected) as e:
        _register_mask(tmp_path, "size", ident, hero, small)
    assert e.value.code == "MASK_SIZE_MISMATCH"


def test_mask_from_other_source_is_rejected(tmp_path):
    """mask 派生自别的源图（等于拿别人的抠图套在自己脸上）必须拒绝。"""
    ident, hero, bg = _setup(tmp_path, "other")
    other_path = _write(tmp_path, "other_hero.png", _background_image((10, 20, 30)))
    other = registry.register_asset(identity_id=ident["id"], kind="variant",
                                    storage_uri=other_path,
                                    file_hash=compose._sha(open(other_path, "rb").read()),
                                    source_evidence="另一张图", width=W, height=H)
    spec = _register_mask(tmp_path, "other", ident, other, _geometric_mask([110, 120, 290, 480]))
    compose.confirm_mask(spec["mask_asset_id"], reviewer="user", outcome="pass")
    ok, reasons = compose.mask_is_usable(spec["mask_asset_id"], hero["id"])
    assert ok is False
    assert any("派生自" in r for r in reasons)


def test_source_file_change_invalidates_mask(tmp_path):
    """源图被替换后，旧 mask 立即失效。"""
    ident, hero, bg = _setup(tmp_path, "stale")
    spec = _register_mask(tmp_path, "stale", ident, hero, _geometric_mask([110, 120, 290, 480]))
    compose.confirm_mask(spec["mask_asset_id"], reviewer="user", outcome="pass")

    new_path = _write(tmp_path, "hero_replaced.png", _background_image((200, 30, 30)))
    registry.replace_asset_file(hero["id"], new_path, compose._sha(open(new_path, "rb").read()))
    ok, reasons = compose.mask_is_usable(spec["mask_asset_id"], hero["id"])
    assert ok is False
    assert any("源文件已被替换" in r or "版本已变更" in r for r in reasons)


def test_unconfirmed_mask_yields_candidate_only(tmp_path):
    """自动草稿未经人工确认：只能产出候选，不得进生产。"""
    ident, hero, bg = _setup(tmp_path, "draft")
    spec = _register_mask(tmp_path, "draft", ident, hero,
                          _geometric_mask([110, 120, 290, 480]), origin="algorithm_draft")
    assert spec["state"] == "draft"
    out = compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"])
    assert out["production_ready"] is False
    assert out["candidate"] is True
    assert any("未经人工目视确认" in r for r in out["blocking_reasons"])
    assert out["hard_protect_check"]["pixel_identical"] is True  # 保证仍成立


def test_confirmed_mask_can_be_production_ready(tmp_path):
    ident, hero, bg = _setup(tmp_path, "ok")
    spec = _register_mask(tmp_path, "ok", ident, hero, _geometric_mask([110, 120, 290, 480]))
    compose.confirm_mask(spec["mask_asset_id"], reviewer="user", outcome="pass", note="目视通过")
    out = compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"])
    assert out["production_ready"] is True
    assert out["candidate"] is False
    assert out["identity_claim"] == "not_run"
    assert "不做身份识别" in out["identity_claim_note"]


def test_confirm_mask_requires_reviewer_and_valid_outcome(tmp_path):
    ident, hero, _bg = _setup(tmp_path, "confirm")
    spec = _register_mask(tmp_path, "confirm", ident, hero, _geometric_mask([110, 120, 290, 480]))
    with pytest.raises(compose.ComposeRejected) as e:
        compose.confirm_mask(spec["mask_asset_id"], reviewer="  ", outcome="pass")
    assert e.value.code == "MASK_REVIEWER_REQUIRED"
    with pytest.raises(compose.ComposeRejected) as e2:
        compose.confirm_mask(spec["mask_asset_id"], reviewer="agent", outcome="auto_pass")
    assert e2.value.code == "MASK_OUTCOME_INVALID"

    compose.confirm_mask(spec["mask_asset_id"], reviewer="user", outcome="fail", note="切到了西装")
    assert compose.get_mask_spec(spec["mask_asset_id"])["state"] == "rejected"


def test_mask_origin_must_be_known(tmp_path):
    ident, hero, _bg = _setup(tmp_path, "origin")
    mask_bytes = _png(_geometric_mask([110, 120, 290, 480]))
    path = os.path.join(str(tmp_path), "mask_origin.png")
    with open(path, "wb") as f:
        f.write(mask_bytes)
    with pytest.raises(compose.ComposeRejected) as e:
        compose.register_mask(identity_id=ident["id"], source_asset_id=hero["id"],
                              mask_binary_png=mask_bytes, mask_alpha_png=mask_bytes,
                              params={}, origin="ai_generated", storage_uri=path)
    assert e.value.code == "MASK_ORIGIN_INVALID"


# ------------------------------------------------------------ 资产与权属门禁


def test_background_requires_license(tmp_path):
    ident, hero, bg = _setup(tmp_path, "lic")
    bg_no_lic = registry.get_asset(bg["id"])
    bg_no_lic["license_id"] = None
    st.put("avatar_assets", bg["id"], bg_no_lic, {"identity_id": ident["id"]})
    spec = _register_mask(tmp_path, "lic", ident, hero, _geometric_mask([110, 120, 290, 480]))
    with pytest.raises(compose.ComposeRejected) as e:
        compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"])
    assert e.value.code == "BACKGROUND_LICENSE_MISSING"


def test_background_license_must_be_commercial(tmp_path):
    """背景是生产素材：权属未核验可商用即阻塞。"""
    ident, hero, _bg = _setup(tmp_path, "lic2")
    bg2_path = _write(tmp_path, "bg_unverified.png", _background_image((30, 30, 30)))
    lic = licensing.register(subject="未核验背景", kind="image_source", scope="unverified")
    bg2 = registry.register_asset(identity_id=ident["id"], kind="background",
                                  storage_uri=bg2_path,
                                  file_hash=compose._sha(open(bg2_path, "rb").read()),
                                  source_evidence="来源待核", license_id=lic["id"],
                                  width=W, height=H)
    spec = _register_mask(tmp_path, "lic2", ident, hero, _geometric_mask([110, 120, 290, 480]))
    with pytest.raises(licensing.LicenseBlocked) as e:
        compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg2["id"])
    assert e.value.code == "LICENSE_UNVERIFIED"


def test_wrong_kind_asset_cannot_be_background(tmp_path):
    """把人物资产当背景用必须拒绝。"""
    ident, hero, bg = _setup(tmp_path, "kind")
    spec = _register_mask(tmp_path, "kind", ident, hero, _geometric_mask([110, 120, 290, 480]))
    with pytest.raises(compose.ComposeRejected) as e:
        compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], hero["id"])
    assert e.value.code == "BACKGROUND_KIND_INVALID"


def test_mock_asset_rejected_from_compose(tmp_path):
    """Mock 后代不得进入生产，也不得继承质量通过结论。"""
    ident, _hero, bg = _setup(tmp_path, "mock")
    mock_path = _write(tmp_path, "mock_hero.png", _background_image((8, 8, 8)))
    mock_hero = registry.register_asset(identity_id=ident["id"], kind="hero",
                                        storage_uri=mock_path,
                                        file_hash=compose._sha(open(mock_path, "rb").read()),
                                        source_evidence="mock 占位", is_mock=True,
                                        width=W, height=H)
    spec = _register_mask(tmp_path, "mock", ident, mock_hero,
                          _geometric_mask([110, 120, 290, 480]))
    with pytest.raises(compose.ComposeRejected) as e:
        compose.compose(ident["id"], mock_hero["id"], spec["mask_asset_id"], bg["id"])
    assert e.value.code == "ASSET_IS_MOCK"


# ------------------------------------------------------------ 预检与草稿


def test_precheck_flags_low_separability(tmp_path):
    """纯色图：人物区与背景同色 → 预检必须报"色差不足"，不允许盲目分割。"""
    from PIL import Image

    flat = Image.new("RGB", (W, H), (205, 208, 214))
    path = os.path.join(str(tmp_path), "flat.png")
    flat.save(path)
    out = compose.precheck_subject(open(path, "rb").read(), HARD)
    assert out["ok"] is False
    assert any("色差不足" in r for r in out["reasons"])


def test_precheck_passes_on_separable_image():
    out = compose.precheck_subject(_png(_source_image()), HARD)
    assert out["ok"] is True
    assert out["nearest_bg_distance_p10"] >= compose.MIN_SEPARABILITY_P10


def test_estimate_mask_returns_draft_with_hashes():
    out = compose.estimate_mask(_png(_source_image()), method="bgmodel", hard_protect=HARD)
    assert out["state"] == "draft"
    assert out["mask_binary_sha256"].startswith("sha256:")
    assert out["mask_alpha_sha256"].startswith("sha256:")
    assert out["pixel_size"] == [W, H]
    assert isinstance(out["problems"], list)
    # 硬保护区必须被完整覆盖，否则说明切到了身份特征
    assert min(out["metrics"]["protect_coverage"]) == 1.0


def test_unknown_mask_method_rejected():
    with pytest.raises(compose.ComposeRejected) as e:
        compose.estimate_mask(_png(_source_image()), method="magic_ai", hard_protect=HARD)
    assert e.value.code == "MASK_METHOD_UNSUPPORTED"


# ------------------------------------------------------------ 边缘去污（unmix）与 matting

def _soft_edge_source(blur=2.0, fg=(46, 48, 58), bg=(205, 208, 214)):
    """构造带"半透明边缘"的源图：色彩 = F·α + B·(1-α)。

    真实影棚照就是这么来的——轮廓外的像素混着原背景的贡献。这正是换深色/异色
    背景后出现灰雾的成因，也是 unmix 与 matting 要处理的对象。
    返回 (图, 真值 alpha)。
    """
    from PIL import Image, ImageDraw, ImageFilter

    a = Image.new("L", (W, H), 0)
    ImageDraw.Draw(a).ellipse([110, 120, 290, 480], fill=255)
    a = a.filter(ImageFilter.GaussianBlur(blur))
    img = Image.composite(Image.new("RGB", (W, H), fg), Image.new("RGB", (W, H), bg), a)
    return img, a


def _band_mask(alpha_img, lo=0.05, hi=0.95):
    """半透明带（排除硬保护区——那里被强制为 1，两种模式都等于源图）。"""
    import numpy as np

    a = np.array(alpha_img).astype(float) / 255.0
    band = (a > lo) & (a < hi)
    x0, y0, x1, y1 = HARD[0]
    band[y0:y1, x0:x1] = False
    return a, band


def test_estimate_source_background_reads_alpha_zero_pixels():
    """B_orig 必须来自 mask 判定为背景的像素，且能还原出真实背景色。"""
    img, a = _soft_edge_source()
    est = compose.estimate_source_background(_png(img), _png(a))
    assert est["fallback"] is False
    assert est["samples"] > 2000
    assert all(abs(v - t) < 3 for v, t in zip(est["corner_median_rgb255"], (205, 208, 214)))


def test_estimate_source_background_falls_back_without_alpha():
    """没有 alpha 可依据时回落到四角中位数常量，并如实标记 fallback。"""
    img, _a = _soft_edge_source()
    est = compose.estimate_source_background(_png(img), None)
    assert est["fallback"] is True
    assert est["samples"] == 0
    assert est["method"] == "corner_median_constant"


def test_unmix_reconstructs_target_exactly_on_true_alpha(tmp_path):
    """unmix 的数学承诺：给到真实 alpha 时，结果精确等于"前景不变、背景换新"。

    期望值 T = F·α + B_new·(1-α) 是可解析算出的，不依赖任何主观判断。
    常规 alpha 混合达不到：它在半透明带里把 α 又乘了一遍，误差为 α(1-α)(B_orig-F)。
    """
    import numpy as np
    from PIL import Image

    fg_c, bg_orig, bg_new = (46, 48, 58), (205, 208, 214), (52, 96, 178)
    img, a = _soft_edge_source(fg=fg_c, bg=bg_orig)
    ident, hero, bg = _setup(tmp_path, "unmix_exact", hero_img=img, bg_color=bg_new)
    spec = _register_mask(tmp_path, "unmix_exact", ident, hero, a)
    out_u = compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                            edge_mode="unmix")
    out_a = compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                            edge_mode="alpha")

    A, band = _band_mask(a)
    assert band.sum() > 200
    F = np.array(fg_c, float)
    Bn = np.array(bg_new, float)
    expect = F[None, None, :] * A[..., None] + Bn[None, None, :] * (1 - A[..., None])

    got_u = np.array(Image.open(io.BytesIO(out_u["composed_png"])).convert("RGB")).astype(int)
    got_a = np.array(Image.open(io.BytesIO(out_a["composed_png"])).convert("RGB")).astype(int)
    d_u = np.abs(got_u - expect)[band].max()
    d_a = np.abs(got_a - expect)[band].max()
    assert d_u <= 4, f"unmix 未还原目标合成结果，最大偏差 {d_u}"
    assert d_u < d_a, f"unmix({d_u}) 应优于常规混合({d_a})"


def test_unmix_keeps_hard_protect_pixel_identical(tmp_path):
    """去污不得削弱核心保证：alpha==1 处校正量严格为 0。"""
    img, a = _soft_edge_source()
    ident, hero, bg = _setup(tmp_path, "unmix_hard", hero_img=img)
    spec = _register_mask(tmp_path, "unmix_hard", ident, hero, a)
    out = compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                          edge_mode="unmix")
    assert out["hard_protect_check"]["pixel_identical"] is True
    assert all(r["max_channel_diff"] == 0 for r in out["hard_protect_check"]["regions"])
    assert out["edge_mode"] == "unmix"
    assert out["edge_decontam"]["formula"] == "C' = C + (1-alpha)*(B_new - B_orig)"


def test_unknown_edge_mode_rejected(tmp_path):
    ident, hero, bg = _setup(tmp_path, "badedge")
    spec = _register_mask(tmp_path, "badedge", ident, hero, _geometric_mask([110, 120, 290, 480]))
    with pytest.raises(compose.ComposeRejected) as e:
        compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                        edge_mode="magic_ai")
    assert e.value.code == "EDGE_MODE_UNSUPPORTED"


def test_edge_residual_reports_band():
    import numpy as np

    comp = np.full((H, W, 3), 100, np.uint8)
    bg = np.full((H, W, 3), 100, np.uint8)
    alpha = np.zeros((H, W), np.uint8)
    alpha[100:200, 100:200] = 128
    out = compose.edge_residual(comp, bg, alpha)
    assert out["band_pixels"] == 100 * 100
    assert out["mean_abs_diff"] == 0.0


def test_matting_recovers_known_alpha():
    """matting 的核心价值：能从图像反解出与真值接近的 alpha。

    羽化版给不出这个——它只会把二值 mask 模糊一遍，细发丝处一律接近 1，
    去污公式的校正量随之退化为 0。
    """
    import numpy as np
    from PIL import Image

    img, a = _soft_edge_source()
    est = compose.estimate_mask(_png(img), method="bgmodel", hard_protect=HARD,
                                alpha_source="matting")
    info = est["params"]["matting"]
    assert info["usable"] is True, info.get("reason")
    got = np.array(Image.open(io.BytesIO(est["mask_alpha_png"])).convert("L")).astype(float) / 255.0
    _truth, band = _band_mask(a)
    truth = np.array(a).astype(float) / 255.0
    assert band.sum() > 200
    err = float(np.abs(got - truth)[band].mean())
    assert err < 0.15, f"matting 与真值 alpha 的平均误差过大：{err}"


def test_matting_marks_soft_edges_not_binary():
    """羽化 vs matting：同一张图，matting 必须产出真正的中间值 alpha。"""
    import numpy as np
    from PIL import Image

    img, _a = _soft_edge_source()
    feather = compose.estimate_mask(_png(img), method="bgmodel", hard_protect=HARD,
                                    alpha_source="feather")
    matting = compose.estimate_mask(_png(img), method="bgmodel", hard_protect=HARD,
                                    alpha_source="matting")
    assert feather["metrics"].get("soft_edge_pixels", 0) == 0  # 羽化版不统计该项
    soft = matting["metrics"]["soft_edge_pixels"]
    assert soft > 200, f"matting 未产出足够的中间值 alpha：{soft}"
    assert matting["params"]["alpha_source"] == "matting"


def test_matting_unusable_without_background_seed():
    """全前景 mask 时没有背景种子可依据，必须如实报不可用，而不是硬编一个 alpha。"""
    import numpy as np
    import cv2

    rgb = np.array(_source_image())
    fg = np.full((H, W), 255, np.uint8)
    a, info = compose._alpha_by_matting(rgb, fg, cv2, np)
    assert a is None
    assert info["usable"] is False


# ------------------------------------------------------------ 画框式缩放（人物大小）
#
# 缩放的本质是"人物在画面里占多大"，不是"人物有多少像素"。故人物层永远 1:1 复制，
# 缩放只改画框 —— 硬保护区断言在缩放前后**同样是"差 1 即失败"**。

def _mask_ellipse_and_torso():
    """头肩椭圆 + 伸到画框下沿的躯干：模拟"人物在画框下沿被裁断"。"""
    from PIL import ImageDraw

    m = _geometric_mask([110, 120, 290, 480])
    ImageDraw.Draw(m).rectangle([60, 400, 340, H - 1], fill=255)
    return m


def _mask_touching_edges():
    """剪影直接贴着左/右/下三条边：模拟"胸像特写"素材（无法外扩画框）。"""
    from PIL import Image, ImageDraw

    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).rectangle([0, 200, W - 1, H - 1], fill=255)
    return m


def _rgb(composed_png):
    import numpy as np
    from PIL import Image

    return np.array(Image.open(io.BytesIO(composed_png)).convert("RGB"))


def test_zoom_in_crops_frame_and_keeps_person_pixels_identical(tmp_path):
    """放大 = 内收画框：画框变小、人物像素一个不改（与不缩放时同一批像素）。"""
    import numpy as np

    ident, hero, bg = _setup(tmp_path, "zin")
    spec = _register_mask(tmp_path, "zin", ident, hero, _geometric_mask([110, 120, 290, 480]))
    base = compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"])
    zoom = compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                           person_scale=1.25)
    assert zoom["pixel_size"][0] < base["pixel_size"][0]
    assert zoom["pixel_size"][1] < base["pixel_size"][1]
    assert zoom["frame"]["preservation_strength"] == "bit_exact"
    assert zoom["frame"]["person_scale_effective"] > 1.2
    assert zoom["hard_protect_check"]["pixel_identical"] is True
    assert all(r["max_channel_diff"] == 0 for r in zoom["hard_protect_check"]["regions"])

    x0, y0, x1, y1 = HARD[0]
    ox, oy = zoom["frame"]["source_offset"]
    a = _rgb(base["composed_png"])[y0:y1, x0:x1]
    b = _rgb(zoom["composed_png"])[y0 + oy:y1 + oy, x0 + ox:x1 + ox]
    assert a.shape == b.shape
    assert int(np.abs(a.astype(int) - b.astype(int)).max()) == 0, "放大改变了人物像素"


def test_zoom_scale_out_of_range_rejected(tmp_path):
    ident, hero, bg = _setup(tmp_path, "zrange")
    spec = _register_mask(tmp_path, "zrange", ident, hero, _geometric_mask([110, 120, 290, 480]))
    for bad in (0.1, 3.0):
        with pytest.raises(compose.ComposeRejected) as e:
            compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                            person_scale=bad)
        assert e.value.code == "ZOOM_OUT_OF_RANGE"


def test_zoom_in_cutting_head_rejected(tmp_path):
    """放大到把头顶顶出画框必须拒绝——上限由素材头顶余量决定，不能靠"大概能行"。"""
    ident, hero, bg = _setup(tmp_path, "zhead")
    spec = _register_mask(tmp_path, "zhead", ident, hero, _geometric_mask([110, 120, 290, 480]))
    with pytest.raises(compose.ComposeRejected) as e:
        compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                        person_scale=1.5)
    assert e.value.code == "FRAME_CUTS_HEAD"
    assert "头顶" in e.value.message


def test_zoom_out_revealing_source_cut_is_gated(tmp_path):
    """外扩画框会露出源图裁断的断面 → 默认硬拒绝；显式覆盖时如实记录量化结果。"""
    ident, hero, bg = _setup(tmp_path, "zout")
    spec = _register_mask(tmp_path, "zout", ident, hero, _mask_touching_edges())
    with pytest.raises(compose.ComposeRejected) as e:
        compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                        person_scale=0.8)
    assert e.value.code == "FRAME_REVEALS_SOURCE_CUT"
    assert e.value.detail["reveal_visible"]

    ok = compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                         person_scale=0.8, allow_frame_reveal=True)
    assert ok["pixel_size"][0] > W and ok["pixel_size"][1] > H
    assert ok["frame"]["revealed_cut"], "覆盖放行后仍须如实记录断面"
    assert ok["frame"]["frame_reveal_acknowledged"] is True
    assert all(r["max_channel_diff"] == 0 for r in ok["hard_protect_check"]["regions"])


def test_explicit_frame_keeps_person_and_fills_scene(tmp_path):
    """显式画框：人物 1:1 放到指定位置，多出来的区域必须是场景背景。"""
    import numpy as np

    ident, hero, bg = _setup(tmp_path, "frame")
    mask = _mask_ellipse_and_torso()
    spec = _register_mask(tmp_path, "frame", ident, hero, mask)
    fit = compose.fit_smaller_frame(_png(mask), HARD, canvas_h=H + 100)
    ox, oy, cw, ch = fit["frame"]
    assert fit["person_height_ratio"] < 0.8, "零断面构图应确实让人物占比下降"
    out = compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                          frame=(ox, oy, cw, ch))
    assert out["pixel_size"] == [cw, ch]
    assert out["frame"]["revealed_cut"] == []
    assert out["hard_protect_check"]["pixel_identical"] is True
    got = _rgb(out["composed_png"])
    assert tuple(int(v) for v in got[oy // 2, cw // 2]) == (52, 96, 178), "补边区域不是场景背景"


def test_frame_and_person_scale_conflict(tmp_path):
    ident, hero, bg = _setup(tmp_path, "zconf")
    spec = _register_mask(tmp_path, "zconf", ident, hero, _geometric_mask([110, 120, 290, 480]))
    with pytest.raises(compose.ComposeRejected) as e:
        compose.compose(ident["id"], hero["id"], spec["mask_asset_id"], bg["id"],
                        person_scale=1.2, frame=(0, 0, W, H))
    assert e.value.code == "FRAME_CONFLICT"


def test_fit_smaller_frame_rejects_when_no_clean_region(tmp_path):
    """剪影一开始就贴着左右边（胸像特写）→ 不存在零断面的缩小构图，如实报错。"""
    with pytest.raises(compose.ComposeRejected) as e:
        compose.fit_smaller_frame(_png(_mask_touching_edges()), HARD, canvas_h=H)
    assert e.value.code == "NO_CLEAN_FRAME"


def test_allowed_scale_range_reflects_source_edges():
    """可用区间由素材几何自动求得：剪影不贴边 → 可缩小；贴边 → 下限被顶到 1.0。"""
    import numpy as np

    free = compose.allowed_scale_range(np.array(_geometric_mask([110, 120, 290, 480])) > 0, HARD)
    assert free["contains_no_zoom"] is True
    assert free["min"] <= 0.4 + free["step"], "剪影不贴边时应能缩小"
    assert free["max"] < compose.MAX_PERSON_SCALE

    edged = compose.allowed_scale_range(np.array(_mask_touching_edges()) > 0, HARD)
    # 剪影贴边时，缩小空间被压到 MIN_REVEAL_STRIP_PX（6px）阈值以内：外扩仅几像素时
    # 断面肉眼不可见，属有意放行；再往外扩就拒。故下限只是略小于 1.0。
    assert 0.95 < edged["min"] < 1.0, f"贴边素材的缩小空间应被压到阈值内，实得 {edged['min']}"
    assert edged["min"] > free["min"] + 0.5, "贴边素材的可缩小空间应远小于不贴边的素材"


# ---------------------------------------------------------------- 背景拟合阶数（回归）

def _rippled_source():
    """带竖直波纹的背景 + 中心深色椭圆。

    波纹是关键：平滑渐变用 2 阶多项式就能拟合，各阶产出的 mask 会完全一样，
    测不出"阶数是否真的生效"。波纹需要更高的阶数才能解释，各阶结果必然不同。
    """
    import math

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), (205, 208, 214))
    d = ImageDraw.Draw(img)
    for y in range(H):
        v = 205 + 18 * math.sin(y / 23.0)
        d.line([(0, y), (W, y)], fill=(int(v), int(v) + 3, int(v) + 8))
    d.ellipse([110, 120, 290, 480], fill=(46, 48, 58))
    return img


def test_poly_basis_degree_is_not_silently_clamped():
    """回归（CS-006 扫描暴露）：deg 必须真实生效，且 deg≤4 与历史实现逐项同序。

    原实现的基函数只硬编码到 4 次项，传 deg=5/6/8 会被**静默当成 4**：
    调用方以为自己提高了拟合阶数，实际产物逐位不变。这类"参数看似生效实则无效"
    比直接报错危险得多，必须用测试钉死。
    """
    import numpy as np

    counts = {d: compose._poly_basis(40, 60, d).shape[1] for d in (2, 3, 4, 5, 6, 8)}
    assert counts == {2: 6, 3: 10, 4: 15, 5: 21, 6: 28, 8: 45}

    b4 = compose._poly_basis(40, 60, 4)
    b6 = compose._poly_basis(40, 60, 6)
    assert b6.shape[1] > b4.shape[1]
    # 前 15 项恰好是 4 阶基且同序 → 阶数 ≤4 的历史行为与既有产物完全不变
    assert np.allclose(b6[:, :15], b4)
    assert not np.allclose(b6[:, 15:], b4[:, :1])


def test_mask_degree_out_of_range_rejected():
    """超出允许范围的阶数必须报错，而不是静默降级成一个别的阶数。"""
    for deg in (0, 1, 9, 12, -2):
        with pytest.raises(compose.ComposeRejected) as e:
            compose.estimate_mask(_png(_source_image()), method="bgmodel",
                                  hard_protect=HARD, deg=deg)
        assert e.value.code == "MASK_DEG_OUT_OF_RANGE"


def test_higher_degree_changes_mask_on_textured_background():
    """阶数提高后产物必须真的不同——这是"deg 生效"的行为级证据。"""
    import hashlib

    src = _png(_rippled_source())
    out = {}
    for deg in (2, 4, 6, 8):
        r = compose.estimate_mask(src, method="bgmodel", hard_protect=HARD, deg=deg)
        out[deg] = {"terms": r["params"]["fitted_terms"], "thr": r["params"]["thr"],
                    "sha": hashlib.sha256(r["mask_binary_png"]).hexdigest()}
    assert [out[d]["terms"] for d in (2, 4, 6, 8)] == [6, 15, 28, 45]
    assert len({out[d]["thr"] for d in (2, 4, 6, 8)}) == 4, "不同阶数的阈值应各不相同"
    assert len({out[d]["sha"] for d in (2, 4, 6, 8)}) == 4, "不同阶数应产出不同的 mask"


# ---------------------------------------------------------------- 背景拟合阶数（回归）

def _rippled_source():
    """带竖直波纹的背景 + 中心深色椭圆。

    波纹是关键：平滑渐变用 2 阶多项式就能拟合，各阶产出的 mask 会完全一样，
    测不出"阶数是否真的生效"。波纹需要更高的阶数才能解释，各阶结果必然不同。
    """
    import math

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), (205, 208, 214))
    d = ImageDraw.Draw(img)
    for y in range(H):
        v = 205 + 18 * math.sin(y / 23.0)
        d.line([(0, y), (W, y)], fill=(int(v), int(v) + 3, int(v) + 8))
    d.ellipse([110, 120, 290, 480], fill=(46, 48, 58))
    return img


def test_poly_basis_degree_is_not_silently_clamped():
    """回归（CS-006 扫描暴露）：deg 必须真实生效，且 deg≤4 与历史实现逐项同序。

    原实现的基函数只硬编码到 4 次项，传 deg=5/6/8 会被**静默当成 4**：
    调用方以为自己提高了拟合阶数，实际产物逐位不变。这类"参数看似生效实则无效"
    比直接报错危险得多，必须用测试钉死。
    """
    import numpy as np

    counts = {d: compose._poly_basis(40, 60, d).shape[1] for d in (2, 3, 4, 5, 6, 8)}
    assert counts == {2: 6, 3: 10, 4: 15, 5: 21, 6: 28, 8: 45}

    b4 = compose._poly_basis(40, 60, 4)
    b6 = compose._poly_basis(40, 60, 6)
    assert b6.shape[1] > b4.shape[1]
    # 前 15 项恰好是 4 阶基且同序 → 阶数 ≤4 的历史行为与既有产物完全不变
    assert np.allclose(b6[:, :15], b4)
    assert not np.allclose(b6[:, 15:], b4[:, :1])


def test_mask_degree_out_of_range_rejected():
    """超出允许范围的阶数必须报错，而不是静默降级成一个别的阶数。"""
    for deg in (0, 1, 9, 12, -2):
        with pytest.raises(compose.ComposeRejected) as e:
            compose.estimate_mask(_png(_source_image()), method="bgmodel",
                                  hard_protect=HARD, deg=deg)
        assert e.value.code == "MASK_DEG_OUT_OF_RANGE"


def test_higher_degree_changes_mask_on_textured_background():
    """阶数提高后产物必须真的不同——这是"deg 生效"的行为级证据。"""
    import hashlib

    src = _png(_rippled_source())
    out = {}
    for deg in (2, 4, 6, 8):
        r = compose.estimate_mask(src, method="bgmodel", hard_protect=HARD, deg=deg)
        out[deg] = {"terms": r["params"]["fitted_terms"], "thr": r["params"]["thr"],
                    "sha": hashlib.sha256(r["mask_binary_png"]).hexdigest()}
    assert [out[d]["terms"] for d in (2, 4, 6, 8)] == [6, 15, 28, 45]
    assert len({out[d]["thr"] for d in (2, 4, 6, 8)}) == 4, "不同阶数的阈值应各不相同"
    assert len({out[d]["sha"] for d in (2, 4, 6, 8)}) == 4, "不同阶数应产出不同的 mask"


# ---------------------------------------------------------------- 连通块策略（seed）

def _source_with_bg_blob():
    """平滑渐变背景 + 中心"人物"椭圆 + 左下角一块贴着画面边的背景斑块。

    模拟 CS-006 的真实素材：AI 重绘的全身照背景带绘画性纹理与"墙→地"两个平面，
    残差图上会冒出成片背景斑块，被"保留所有面积达标前景块"的策略收进 mask，
    于是剪影"贴边"，人物再也无法缩小置入场景。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), (205, 208, 214))
    d = ImageDraw.Draw(img)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=(200 + y // 30, 203 + y // 30, 209 + y // 30))
    d.ellipse([110, 120, 290, 480], fill=(46, 48, 58))     # 人物
    d.rectangle([0, 470, 62, 556], fill=(58, 60, 70))      # 贴左边界的背景斑块
    return img


def test_component_mode_all_keeps_background_blob_and_touches_border():
    """历史行为：所有面积达标的块都保留 → 背景斑块害得剪影贴边。"""
    out = compose.estimate_mask(_png(_source_with_bg_blob()), method="bgmodel",
                                hard_protect=HARD, component_mode="all")
    assert out["params"]["border_touch_pixels"]["left"] > 0, "斑块贴着左边界，应被计入贴边"
    assert out["metrics"]["connected_components"] >= 2


def test_component_mode_seed_drops_background_blob_and_clears_border():
    """seed 策略：只留含人脸种子的那一块 → 贴边归零，且丢弃清单入档。"""
    out = compose.estimate_mask(_png(_source_with_bg_blob()), method="bgmodel",
                                hard_protect=HARD, component_mode="seed")
    p = out["params"]
    assert p["component_mode"] == "seed"
    assert p["border_touch_pixels"] == {"left": 0, "right": 0, "top": 0, "bottom": 0}
    assert p["border_touching_edges"] == []
    assert p["dropped_component_count"] >= 1
    assert p["dropped_area"] > 0
    assert p["kept_component_area"] > 0
    assert out["metrics"]["connected_components"] == 1
    assert p["hard_protect"] == [list(HARD[0])]


def test_component_mode_seed_always_requires_visual_review():
    """seed 是有损操作：必须永远没有自动通过路径（usable 恒为 False）。"""
    out = compose.estimate_mask(_png(_source_with_bg_blob()), method="bgmodel",
                                hard_protect=HARD, component_mode="seed",
                                alpha_source="matting")
    assert out["usable"] is False
    assert any("seed" in r for r in out["problems"]), out["problems"]


def test_unknown_component_mode_rejected():
    with pytest.raises(compose.ComposeRejected) as e:
        compose.estimate_mask(_png(_source_image()), method="bgmodel",
                              hard_protect=HARD, component_mode="largest")
    assert e.value.code == "MASK_COMPONENT_MODE_UNSUPPORTED"


# ---------------------------------------------------------------- 连通块策略（seed）

def _source_with_bg_blob():
    """平滑渐变背景 + 中心"人物"椭圆 + 左下角一块贴着画面边的背景斑块。

    模拟 CS-006 的真实素材：AI 重绘的全身照背景带绘画性纹理与"墙→地"两个平面，
    残差图上会冒出成片背景斑块，被"保留所有面积达标前景块"的策略收进 mask，
    于是剪影"贴边"，人物再也无法缩小置入场景。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), (205, 208, 214))
    d = ImageDraw.Draw(img)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=(200 + y // 30, 203 + y // 30, 209 + y // 30))
    d.ellipse([110, 120, 290, 480], fill=(46, 48, 58))     # 人物
    d.rectangle([0, 470, 62, 556], fill=(58, 60, 70))      # 贴左边界的背景斑块
    return img


def test_component_mode_all_keeps_background_blob_and_touches_border():
    """历史行为：所有面积达标的块都保留 → 背景斑块害得剪影贴边。"""
    out = compose.estimate_mask(_png(_source_with_bg_blob()), method="bgmodel",
                                hard_protect=HARD, component_mode="all")
    assert out["params"]["border_touch_pixels"]["left"] > 0, "斑块贴着左边界，应被计入贴边"
    assert out["metrics"]["connected_components"] >= 2


def test_component_mode_seed_drops_background_blob_and_clears_border():
    """seed 策略：只留含人脸种子的那一块 → 贴边归零，且丢弃清单入档。"""
    out = compose.estimate_mask(_png(_source_with_bg_blob()), method="bgmodel",
                                hard_protect=HARD, component_mode="seed")
    p = out["params"]
    assert p["component_mode"] == "seed"
    assert p["border_touch_pixels"] == {"left": 0, "right": 0, "top": 0, "bottom": 0}
    assert p["border_touching_edges"] == []
    assert p["dropped_component_count"] >= 1
    assert p["dropped_area"] > 0
    assert p["kept_component_area"] > 0
    assert out["metrics"]["connected_components"] == 1
    assert p["hard_protect"] == [list(HARD[0])]


def test_component_mode_seed_always_requires_visual_review():
    """seed 是有损操作：必须永远没有自动通过路径（usable 恒为 False）。"""
    out = compose.estimate_mask(_png(_source_with_bg_blob()), method="bgmodel",
                                hard_protect=HARD, component_mode="seed",
                                alpha_source="matting")
    assert out["usable"] is False
    assert any("seed" in r for r in out["problems"]), out["problems"]


def test_unknown_component_mode_rejected():
    with pytest.raises(compose.ComposeRejected) as e:
        compose.estimate_mask(_png(_source_image()), method="bgmodel",
                              hard_protect=HARD, component_mode="largest")
    assert e.value.code == "MASK_COMPONENT_MODE_UNSUPPORTED"
