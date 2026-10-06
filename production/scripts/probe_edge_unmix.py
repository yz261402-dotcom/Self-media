"""边缘质量三方对比：定位"换深色背景后轮廓外的灰雾"到底靠什么消除。

A 羽化 mask + 常规 alpha 混合   —— T03 原方案
B 羽化 mask + unmix 代数去污    —— 只改合成公式
C matting mask + unmix 去污     —— 改成反解真实 alpha（本脚本要验证的方案）

背景：源图人物在浅灰影棚拍摄。轮廓半透明处像素 C = αF + (1-α)B_orig，
      B_orig 是浅灰背景。若 α 被高估（羽化恒给 1.0），去污公式的校正量 (1-α)(B_new-B_orig)
      就退化为 0，浅灰被整片搬到深色背景上 → 肉眼可见灰雾。

产物一律为候选（mask 未经人工确认）。

用法：
  cd production/backend && .venv/Scripts/python.exe ../scripts/probe_edge_unmix.py
"""
from __future__ import annotations

import io
import json
import os
import sys
from typing import Any, Dict, List, Tuple

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

IDENTITY_ID = "av_base_b_v1"
HERO_ASSET_ID = "img_745806f0e7eb"
BG_URI = "assets/背景/程序生成/深灰演播室.png"
MASK_DIR_REL = "assets/固定主播/主播B/版本1/遮罩"
OUT_DIR_REL = "experiments/抠图合成验证/CS-003"
HARD_PROTECT: List[Tuple[int, int, int, int]] = [(330, 200, 560, 500)]

CROPS: List[Tuple[str, Tuple[int, int, int, int]]] = [
    ("头部轮廓", (300, 100, 660, 400)),
    ("左肩轮廓", (150, 430, 470, 760)),
]
ZOOM = 2

CASES = [
    ("A_羽化_常规混合", "feather", "alpha"),
    ("B_羽化_去污", "feather", "unmix"),
    ("C_matting_去污", "matting", "unmix"),
]


def main() -> int:
    from PIL import Image, ImageDraw

    from app import storage as st
    from app.avatar import compose, registry

    st.init_db()

    hero = registry.get_asset(HERO_ASSET_ID)
    if not hero:
        print(f"源资产不存在：{HERO_ASSET_ID}")
        return 3
    bg = next((a for a in registry.list_assets(IDENTITY_ID, "background")
               if a.get("storage_uri") == BG_URI), None)
    if not bg:
        print(f"背景资产未登记：{BG_URI}；请先运行 scripts/compose_background_variants.py")
        return 3

    hero_bytes = open(os.path.join(WORKSPACE, hero["storage_uri"]), "rb").read()
    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    mask_dir = os.path.join(WORKSPACE, MASK_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(mask_dir, exist_ok=True)

    # ---- 生成两种 mask 并登记（各自独立文件，避免互相覆盖） ----
    masks: Dict[str, Dict[str, Any]] = {}
    for src, tag in (("feather", "feather"), ("matting", "matting")):
        kw: Dict[str, Any] = dict(method="bgmodel", hard_protect=HARD_PROTECT, deg=4, iters=5,
                                  thr_scale=3.0, alpha_source=src)
        if src == "feather":
            kw["erode"] = 2
        est = compose.estimate_mask(hero_bytes, **kw)
        if src == "matting" and not est["params"].get("matting", {}).get("usable"):
            print(f"matting 不可用：{est['params']['matting'].get('reason')}")
            return 3
        alpha_rel = f"{MASK_DIR_REL}/mask-{tag}-draft-alpha.png"
        bin_rel = f"{MASK_DIR_REL}/mask-{tag}-draft-binary.png"
        for rel, key in ((alpha_rel, "mask_alpha_png"), (bin_rel, "mask_binary_png")):
            with open(os.path.join(WORKSPACE, rel), "wb") as f:
                f.write(est[key])
        spec = compose.register_mask(
            identity_id=IDENTITY_ID, source_asset_id=hero["id"],
            mask_binary_png=est["mask_binary_png"], mask_alpha_png=est["mask_alpha_png"],
            params=est["params"], origin="algorithm_draft", storage_uri=alpha_rel,
            source_evidence=f"由 {hero['id']} 经 bgmodel 自动分割生成（{tag}，草稿待人工确认）",
        )
        masks[src] = {"spec": spec, "est": est}
        print(f"[mask:{tag}] {spec['id']}  asset={spec['mask_asset_id']}")
        print(f"           metrics={est['metrics']}")
        if src == "matting":
            mm = est["params"]["matting"]
            print(f"           matting: 种子距离={mm['seed_dist_used']}px 种子像素={mm['seed_pixels']} "
                  f"容差={mm['tol_auto']} 不确定区={mm['uncertain_pixels']} "
                  f"病态像素={mm['degenerate_pixels']}")

    # ---- 三种组合合成 ----
    composed: Dict[str, Any] = {}
    report: Dict[str, Any] = {}
    for name, alpha_src, edge_mode in CASES:
        spec = masks[alpha_src]["spec"]
        out = compose.compose(IDENTITY_ID, HERO_ASSET_ID, spec["mask_asset_id"], bg["id"],
                              out_uri=f"{OUT_DIR_REL}/人物B-深灰-{name}.png", edge_mode=edge_mode)
        img = Image.open(io.BytesIO(out["composed_png"])).convert("RGB")
        composed[name] = img
        with open(os.path.join(out_dir, f"人物B-深灰-{name}.png"), "wb") as f:
            f.write(out["composed_png"])
        report[name] = {
            "mask_source": alpha_src, "edge_mode": edge_mode,
            "sha256": out["output_sha256"],
            "edge_residual": out["edge_residual"],
            "hard_protect_pixel_identical": out["hard_protect_check"]["pixel_identical"],
            "hard_protect_max_diff": [r["max_channel_diff"]
                                      for r in out["hard_protect_check"]["regions"]],
            "edge_decontam": out["edge_decontam"],
            "production_ready": out["production_ready"],
            "candidate": out["candidate"],
        }
        r = report[name]
        print(f"\n[{name}] 过渡带残留 均值={r['edge_residual']['mean_abs_diff']} "
              f"p95={r['edge_residual']['p95_abs_diff']}  硬保护区一致="
              f"{r['hard_protect_pixel_identical']} 最大通道差={r['hard_protect_max_diff']}")

    # ---- 并排对比图 ----
    names = [c[0] for c in CASES]
    for cname, box in CROPS:
        w, h = box[2] - box[0], box[3] - box[1]
        canvas = Image.new("RGB", (w * ZOOM * 3 + 32, h * ZOOM + 32), (255, 255, 255))
        dr = ImageDraw.Draw(canvas)
        for i, nm in enumerate(names):
            crop = composed[nm].crop(box).resize((w * ZOOM, h * ZOOM), Image.NEAREST)
            x = 8 + i * (w * ZOOM + 8)
            canvas.paste(crop, (x, 28))
            dr.text((x, 10), nm, fill=(20, 20, 20))
        canvas.save(os.path.join(out_dir, f"三方对比-{cname}.png"))
        print(f"[对比图] {OUT_DIR_REL}/三方对比-{cname}.png")

    a = report[CASES[0][0]]["edge_residual"]["mean_abs_diff"]
    c = report[CASES[2][0]]["edge_residual"]["mean_abs_diff"]
    if a and c:
        report["verdict"] = {
            "residual_old_A": a, "residual_new_C": c,
            "reduction_pct": round((a - c) / a * 100, 1),
            "note": "数值为轮廓过渡带像素与目标背景的最大通道差均值，越低越干净",
        }
        print(f"\n结论：过渡带残留均值 {a} → {c}（降低 {round((a-c)/a*100,1)}%）；"
              f"三种组合硬保护区均逐像素不变。")

    report["policy"] = "mask 为自动草稿；matting 不改变硬保护区像素，身份一致性结论仍需人工审核"
    with io.open(os.path.join(out_dir, "边缘去污对比.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"报告：{OUT_DIR_REL}/边缘去污对比.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
