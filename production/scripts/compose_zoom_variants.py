"""人物大小的三条路线实测：放大 / 缩小（露断面）/ 零断面缩小构图。

背景：源图是"胸像特写"，人物躯干在左、右、下三条边都被源图裁断（实测贴边行：
左 x=0 自 y=951 起、右 x=940 自 y=921 起、下 y=1671 整行）。这决定了三条路线的代价：

  1. 放大（画框内收）—— 无副作用。上限由头顶余量决定（本素材 1.415×）。
  2. 缩小（画框外扩）—— **会露出源图裁断的断面**：左右各一条 720~750px 长的直线切口。
     默认被 compose 硬拒绝；本脚本显式 allow_frame_reveal=True 产出，仅供看清楚成因。
  3. 零断面缩小 —— 不整体外扩，而是把画框**下移**：让人物在画框下沿被裁断（断面正好
     落在画框边缘，看不到），上半部分交给场景。人物像素仍是 1:1 逐像素复制。

用法：
  cd production/backend && .venv/Scripts/python.exe ../scripts/compose_zoom_variants.py
"""
from __future__ import annotations

import io
import json
import os
import sys
from typing import Any, Dict, List

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

IDENTITY_ID = "av_base_b_v1"
HERO_ASSET_ID = "img_745806f0e7eb"
MASK_ALPHA_REL = "assets/固定主播/主播B/版本1/遮罩/mask-matting-draft-alpha.png"
OUT_DIR_REL = "experiments/抠图合成验证/CS-005"
HARD_PROTECT = [(330, 200, 560, 500)]
SCENE = "教室课堂"


def _load_mask_alpha():
    import numpy as np
    from PIL import Image

    return np.array(Image.open(os.path.join(WORKSPACE, MASK_ALPHA_REL)).convert("L"))


def annotate_reveal(img, reveal: List[Dict[str, Any]]):
    """把暴露出来的断面区域框成红色，便于一眼看出问题在哪。"""
    from PIL import ImageDraw

    im = img.copy()
    d = ImageDraw.Draw(im)
    for r in reveal:
        y0, y1 = r["canvas_span"]
        if r["edge"] == "left":
            box = [0, y0, r["canvas_coord"], y1]
        elif r["edge"] == "right":
            box = [r["canvas_coord"], y0, r["canvas_coord"] + r["exposed_strip_px"], y1]
        elif r["edge"] == "top":
            box = [y0, 0, y1, r["canvas_coord"]]        # canvas_span 存的是 x 范围
        else:
            box = [y0, r["canvas_coord"], y1, r["canvas_coord"] + r["exposed_strip_px"]]
        d.rectangle(box, outline=(220, 30, 30), width=5)
        d.text((box[0] + 8, box[1] + 8), f"{r['edge']} cut {r['span_px']}px",
               fill=(220, 30, 30))
    return im


def main() -> int:
    from PIL import Image

    from app import storage as st
    from app.avatar import compose, registry

    st.init_db()
    hero = registry.get_asset(HERO_ASSET_ID)
    if not hero:
        print(f"源资产不存在：{HERO_ASSET_ID}")
        return 3

    spec = None
    for row in st.all_rows("mask_specs"):
        p = row.get("params") or {}
        if (row.get("source_asset_id") == hero["id"]
                and p.get("alpha_source") == "matting"
                and (p.get("matting") or {}).get("usable")):
            spec = row
    if spec is None:
        print("未找到 matting mask；请先运行 scripts/compose_scene_variants.py")
        return 3

    bgs = [a for a in registry.list_assets(IDENTITY_ID, "background")
           if SCENE in str(a.get("storage_uri", ""))]
    if not bgs:
        print("未找到教室场景背景；请先运行 scripts/ingest_scene_backgrounds.py")
        return 3
    bg = sorted(bgs, key=lambda x: x.get("created_at") or "")[-1]

    ab = _load_mask_alpha() > compose.MASK_BINARY_MAX
    src_h, src_w = ab.shape
    rng = compose.allowed_scale_range(ab, HARD_PROTECT)
    r_last = compose.fit_smaller_frame(
        open(os.path.join(WORKSPACE, MASK_ALPHA_REL), "rb").read(),
        HARD_PROTECT, src_h)["clean_last_row"]

    print(f"源图 {src_w}×{src_h}   场景={SCENE}   mask={spec['id']}")
    print(f"自动求得的可用缩放区间（含 1.0 的连续段）：[{rng['min']}, {rng['max']}]"
          f"   步长 {rng['step']}")
    print(f"左右贴边起始行：干净可见到第 {r_last} 行（之下开始贴住画框左右边）\n")

    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)
    produced: List[Dict[str, Any]] = []

    cases = [
        ("放大-1.15", dict(person_scale=1.15), False),
        ("放大-1.30", dict(person_scale=1.30), False),
        ("缩小-0.70-露断面", dict(person_scale=0.70, allow_frame_reveal=True), True),
    ]
    for name, kw, mark in cases:
        out = compose.compose(IDENTITY_ID, HERO_ASSET_ID, spec["mask_asset_id"], bg["id"],
                              out_uri=f"{OUT_DIR_REL}/人物B-{name}.png",
                              edge_mode="unmix", **kw)
        img = Image.open(io.BytesIO(out["composed_png"]))
        with open(os.path.join(out_dir, f"人物B-{name}.png"), "wb") as f:
            f.write(out["composed_png"])
        fr = out["frame"]
        rec = {
            "case": name, "file": f"{OUT_DIR_REL}/人物B-{name}.png",
            "canvas_size": fr["canvas_size"], "source_offset": fr["source_offset"],
            "person_scale_effective": fr["person_scale_effective"],
            "背景放大倍数": fr["background_upscale"],
            "头顶余量px": fr["head_margin_px"],
            "露出的断面": fr["revealed_cut"],
            "硬保护区逐像素一致": out["hard_protect_check"]["pixel_identical"],
            "硬保护区最大通道差": [r["max_channel_diff"]
                                   for r in out["hard_protect_check"]["regions"]],
            "output_sha256": out["output_sha256"],
            "production_ready": out["production_ready"],
            "candidate": out["candidate"],
        }
        produced.append(rec)
        if mark and fr["revealed_cut"]:
            ann = annotate_reveal(img, fr["revealed_cut"])
            with open(os.path.join(out_dir, f"人物B-{name}-标注断面.png"), "wb") as f:
                ann.save(f, format="PNG")
        print(f"[{name}] 画框={fr['canvas_size']} 偏移={fr['source_offset']} "
              f"头顶余量={fr['head_margin_px']}px 背景放大={fr['background_upscale']}×")
        print(f"        硬保护区逐像素一致={rec['硬保护区逐像素一致']} "
              f"最大通道差={rec['硬保护区最大通道差']}")
        if fr["revealed_cut"]:
            for r in fr["revealed_cut"]:
                print(f"        断面 {r['edge']}: 暴露 {r['exposed_strip_px']}px 宽、"
                      f"切口长 {r['span_px']}px")

    # 路线 3：零断面缩小构图（画框下移，断面压在画框下沿）
    for canvas_h, tag in ((src_h, "同尺寸画框"), (int(src_h * 1.18), "更大画框")):
        fit = compose.fit_smaller_frame(
            open(os.path.join(WORKSPACE, MASK_ALPHA_REL), "rb").read(),
            HARD_PROTECT, canvas_h)
        ox, oy, cw, ch = fit["frame"]
        out = compose.compose(IDENTITY_ID, HERO_ASSET_ID, spec["mask_asset_id"], bg["id"],
                              out_uri=f"{OUT_DIR_REL}/人物B-零断面缩小-{tag}.png",
                              edge_mode="unmix", frame=(ox, oy, cw, ch))
        with open(os.path.join(out_dir, f"人物B-零断面缩小-{tag}.png"), "wb") as f:
            f.write(out["composed_png"])
        fr = out["frame"]
        person_h_ratio = fit["person_height_ratio"]
        produced.append({
            "case": f"零断面缩小-{tag}", "file": f"{OUT_DIR_REL}/人物B-零断面缩小-{tag}.png",
            "canvas_size": fr["canvas_size"], "source_offset": fr["source_offset"],
            "人物占画面高度比": person_h_ratio,
            "头顶余量px": fr["head_margin_px"],
            "露出的断面": fr["revealed_cut"],
            "硬保护区逐像素一致": out["hard_protect_check"]["pixel_identical"],
            "硬保护区最大通道差": [r["max_channel_diff"]
                                   for r in out["hard_protect_check"]["regions"]],
            "output_sha256": out["output_sha256"],
            "candidate": out["candidate"],
        })
        print(f"[零断面缩小-{tag}] 画框={fr['canvas_size']} 偏移={fr['source_offset']} "
              f"人物占画面高度={person_h_ratio:.1%} 断面={fr['revealed_cut'] or '无'}")
        print(f"        硬保护区逐像素一致={out['hard_protect_check']['pixel_identical']} "
              f"最大通道差={[r['max_channel_diff'] for r in out['hard_protect_check']['regions']]}")

    report = {
        "source_asset_id": hero["id"], "mask_spec_id": spec["id"], "scene": SCENE,
        "source_size": [src_w, src_h],
        "allowed_scale_range": rng,
        "clean_visible_last_row": r_last,
        "source_edge_contact": {
            "left_x0_first_row": 951, "right_x944_first_row": 921, "bottom_row_full_width": True,
            "note": "源图是胸像特写：躯干在左/右/下三边被源图裁断，故画框外扩必露断面",
        },
        "cases": produced,
        "policy": "人物层全程 1:1 逐像素复制；缩放与构图只改画框。断面暴露默认硬拒绝，"
                  "本报告中的'缩小-0.70-露断面'为显式覆盖的对照实验，不作为生产路径。",
    }
    with io.open(os.path.join(out_dir, "人物大小实测报告.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告：{OUT_DIR_REL}/人物大小实测报告.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
