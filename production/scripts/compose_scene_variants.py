"""场景背景合成：matting mask + unmix 代数去污，产出人物 B 的场景化候选。

与纯色/渐变背景（CS-002）的区别：
  · mask 改用 matting 反解的真实 alpha（羽化版恒为 1，去污无从生效）
  · 合成走 unmix，把轮廓半透明处**原影棚背景的贡献**换成场景背景的贡献
  · 场景背景为 AI 生成，权属按生成服务条款登记（见 CS-004/场景背景入库.json）

构图保持 1:1 像素复制（人物不缩放），因此 comose 的"硬保护区逐像素与源图相同"
断言继续成立——身份零漂移的可验证依据不被削弱。

产物一律为候选（mask 为自动草稿，未经人工目视确认）。

用法：
  cd production/backend && .venv/Scripts/python.exe ../scripts/compose_scene_variants.py
"""
from __future__ import annotations

import io
import json
import os
import sys
from typing import Any, Dict, List, Optional

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

IDENTITY_ID = "av_base_b_v1"
HERO_ASSET_ID = "img_745806f0e7eb"
MASK_DIR_REL = "assets/固定主播/主播B/版本1/遮罩"
OUT_DIR_REL = "experiments/抠图合成验证/CS-004"
BG_PREFIX = "assets/背景/场景AI/规范/"
HARD_PROTECT = [(330, 200, 560, 500)]
MASK_PARAMS = {"deg": 4, "iters": 5, "thr_scale": 3.0, "alpha_source": "matting"}


def _sha(b: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(b).hexdigest()


def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def ensure_matting_mask(hero) -> Dict[str, Any]:
    """复用已登记的 matting mask；没有则现算一次并登记。"""
    from app import storage as st
    from app.avatar import compose

    for spec in st.all_rows("mask_specs"):
        p = spec.get("params") or {}
        if (spec.get("source_asset_id") == hero["id"]
                and p.get("alpha_source") == "matting"
                and (p.get("matting") or {}).get("usable")):
            return spec

    hero_bytes = open(os.path.join(WORKSPACE, hero["storage_uri"]), "rb").read()
    est = compose.estimate_mask(hero_bytes, method="bgmodel", hard_protect=HARD_PROTECT,
                                **MASK_PARAMS)
    if not est["params"].get("matting", {}).get("usable"):
        raise SystemExit(f"matting 不可用：{est['params']['matting'].get('reason')}")
    mask_dir = os.path.join(WORKSPACE, MASK_DIR_REL)
    os.makedirs(mask_dir, exist_ok=True)
    alpha_rel = f"{MASK_DIR_REL}/mask-matting-draft-alpha.png"
    bin_rel = f"{MASK_DIR_REL}/mask-matting-draft-binary.png"
    for rel, key in ((alpha_rel, "mask_alpha_png"), (bin_rel, "mask_binary_png")):
        with open(os.path.join(WORKSPACE, rel), "wb") as f:
            f.write(est[key])
    spec = compose.register_mask(
        identity_id=IDENTITY_ID, source_asset_id=hero["id"],
        mask_binary_png=est["mask_binary_png"], mask_alpha_png=est["mask_alpha_png"],
        params=est["params"], origin="algorithm_draft", storage_uri=alpha_rel,
        source_evidence=f"由 {hero['id']} 经 bgmodel+局部 F/B matting 生成（草稿，待人工确认）",
    )
    return spec


def main() -> int:
    from PIL import Image

    from app import storage as st
    from app.avatar import compose, registry

    st.init_db()
    hero = registry.get_asset(HERO_ASSET_ID)
    if not hero:
        print(f"源资产不存在：{HERO_ASSET_ID}")
        return 3

    bgs = [a for a in registry.list_assets(IDENTITY_ID, "background")
           if str(a.get("storage_uri", "")).startswith(BG_PREFIX)]
    if not bgs:
        print(f"未找到场景背景资产；请先运行 scripts/ingest_scene_backgrounds.py")
        return 3
    # 同一路径只保留最新一条，避免重复登记造成的重复合成
    uniq: Dict[str, Dict[str, Any]] = {}
    for a in sorted(bgs, key=lambda x: x.get("created_at") or ""):
        uniq[a["storage_uri"]] = a
    bgs = list(uniq.values())

    spec = ensure_matting_mask(hero)
    print(f"源资产 {hero['id']}  mask {spec['id']}（{spec['state']}）")
    mm = (spec.get("params") or {}).get("matting") or {}
    print(f"matting: 种子距离={mm.get('seed_dist_used')}px 不确定区={mm.get('uncertain_pixels')} "
          f"病态={mm.get('degenerate_pixels')}")
    print(f"场景背景 {len(bgs)} 张\n")

    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)

    produced: List[Dict[str, Any]] = []
    for bg in bgs:
        name = os.path.splitext(os.path.basename(bg["storage_uri"]))[0]
        out = compose.compose(IDENTITY_ID, HERO_ASSET_ID, spec["mask_asset_id"], bg["id"],
                              out_uri=f"{OUT_DIR_REL}/人物B-{name}.png", edge_mode="unmix")
        fname = f"人物B-{name}.png"
        with open(os.path.join(out_dir, fname), "wb") as f:
            f.write(out["composed_png"])
        rec = {
            "scene": name,
            "background_asset_id": bg["id"],
            "background_license_id": bg.get("license_id"),
            "file": f"{OUT_DIR_REL}/{fname}",
            "output_sha256": out["output_sha256"],
            "edge_mode": out["edge_mode"],
            "edge_residual": out["edge_residual"],
            "source_bg_corner_median": (out["edge_decontam"] or {}).get(
                "source_bg_corner_median_rgb255"),
            "hard_protect_pixel_identical": out["hard_protect_check"]["pixel_identical"],
            "hard_protect_max_diff": [r["max_channel_diff"]
                                      for r in out["hard_protect_check"]["regions"]],
            "production_ready": out["production_ready"],
            "candidate": out["candidate"],
            "blocking_reasons": out["blocking_reasons"],
            "identity_claim": out["identity_claim"],
        }
        produced.append(rec)
        print(f"[合成] {name}")
        print(f"       原始影棚背景色 {rec['source_bg_corner_median']} → 场景背景")
        print(f"       过渡带残留 均值={rec['edge_residual']['mean_abs_diff']} "
              f"p95={rec['edge_residual']['p95_abs_diff']}")
        print(f"       硬保护区逐像素一致={rec['hard_protect_pixel_identical']} "
              f"最大通道差={rec['hard_protect_max_diff']}  候选={rec['candidate']}")

    report = {
        "source_asset_id": hero["id"],
        "mask_spec_id": spec["id"], "mask_asset_id": spec["mask_asset_id"],
        "mask_state": spec["state"], "mask_params": spec.get("params"),
        "scenes": produced,
        "policy": "mask 为自动草稿，未经人工确认前一律为候选，不得定版进入生产；"
                  "合成不改变硬保护区像素，身份一致性结论仍需人工/审核",
    }
    with io.open(os.path.join(out_dir, "场景合成报告.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告：{OUT_DIR_REL}/场景合成报告.json")
    print(f"全部 {len(produced)} 组均为候选")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
