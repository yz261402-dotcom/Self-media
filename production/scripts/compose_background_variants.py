"""用确定性抠图合成产出人物 B 的背景变体（T03 A 方案落地）。

链路：真实主图 → 预检 → 草稿 mask → 登记（绑定源图哈希）→ 合成 → 逐像素校验 → 产出候选。
背景素材为**程序生成的纯色/渐变**（不含第三方素材，权属无争议），因此不产生费用。

产物一律标记为**候选**：mask 是自动草稿，未经人工目视确认不得定版（见 compose.confirm_mask）。

用法：
  cd production/backend && .venv/Scripts/python.exe ../scripts/compose_background_variants.py
"""
from __future__ import annotations

import hashlib
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
MASK_DIR_REL = "assets/固定主播/主播B/版本1/遮罩"
BG_DIR_REL = "assets/背景/程序生成"
OUT_DIR_REL = "experiments/抠图合成验证/CS-002"

# 实测最优参数：四阶背景曲面 + 严格残差阈值 + 边缘腐蚀（见 CS-001 指标 json）
MASK_PARAMS = {"deg": 4, "iters": 5, "thr_scale": 3.0, "erode": 2, "feather_sigma": 1.6}

# 硬保护区：人脸核心（紧贴面部，不含背景）
HARD_PROTECT: List[Tuple[int, int, int, int]] = [(330, 200, 560, 500)]

BACKGROUNDS: List[Dict[str, Any]] = [
    {"name": "浅灰蓝影棚", "kind": "solid", "color": (223, 228, 236)},
    {"name": "深灰演播室", "kind": "solid", "color": (58, 62, 72)},
    {"name": "冷调渐变", "kind": "gradient", "top": (232, 236, 244), "bottom": (186, 196, 214)},
    {"name": "暖木色书房", "kind": "gradient", "top": (238, 230, 216), "bottom": (196, 176, 150)},
]


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_background(spec: Dict[str, Any], size: Tuple[int, int]):
    from PIL import Image, ImageDraw

    w, h = size
    if spec["kind"] == "solid":
        return Image.new("RGB", (w, h), spec["color"])
    img = Image.new("RGB", (w, h), spec["top"])
    d = ImageDraw.Draw(img)
    t, b = spec["top"], spec["bottom"]
    for y in range(h):
        k = y / max(1, h - 1)
        d.line([(0, y), (w, y)],
               fill=tuple(int(t[i] + (b[i] - t[i]) * k) for i in range(3)))
    return img


def main() -> int:
    from app import storage as st
    from app.avatar import compose, licensing, registry

    st.init_db()  # 运行库可能还是旧 schema，需补齐新表

    ident = registry.get_identity(IDENTITY_ID)
    if not ident:
        print(f"人物不存在：{IDENTITY_ID}；请先运行 scripts/ingest_base_assets.py")
        return 3

    heroes = [a for a in registry.list_assets(IDENTITY_ID, "hero")]
    if not heroes:
        print("未找到 hero 资产，请先运行 scripts/ingest_base_assets.py")
        return 3
    hero = sorted(heroes, key=lambda a: a.get("created_at") or "")[-1]
    if not hero.get("file_hash"):
        print(f"hero 资产缺哈希，无法参与合成：{hero['id']}")
        return 3

    hero_path = os.path.join(WORKSPACE, hero["storage_uri"])
    hero_bytes = open(hero_path, "rb").read()
    if _sha(hero_bytes) != hero["file_hash"]:
        print("主图哈希与登记不符，已中止（需重新核验素材）")
        return 3

    print(f"源资产：{hero['id']}  {hero['storage_uri']}")
    print(f"文件哈希一致：{hero['file_hash'][:19]}…  {hero.get('width')}×{hero.get('height')}")

    # 1) 预检
    pre = compose.precheck_subject(hero_bytes, HARD_PROTECT)
    print(f"\n[预检] 人物→背景最近距离 p10={pre['nearest_bg_distance_p10']}"
          f" 中位数={pre['nearest_bg_distance_median']} 背景标准差={pre['background_sample_std']}")
    print(f"[预检] {'通过' if pre['ok'] else '不通过'}"
          + ("" if pre["ok"] else "：" + "；".join(pre["reasons"])))

    # 2) 草稿 mask
    est = compose.estimate_mask(hero_bytes, method="bgmodel", hard_protect=HARD_PROTECT,
                                **{k: v for k, v in MASK_PARAMS.items()
                                   if k in ("deg", "iters", "thr_scale", "erode", "feather_sigma")})
    print(f"\n[mask] 方法=bgmodel 参数={est['params']}")
    print(f"[mask] 前景占比={est['metrics']['foreground_ratio']} "
          f"硬保护覆盖={est['metrics']['protect_coverage']} "
          f"大块数={est['metrics']['connected_components']} "
          f"边缘占比={est['metrics']['edge_pixel_ratio']}")
    print(f"[mask] 状态={est['state']} 可用性={'未见已知失效' if est['usable'] else '有问题'}")
    for p in est["problems"]:
        print(f"        · {p}")

    # 3) 落盘 + 登记（绑定源图哈希与版本）
    mask_dir = os.path.join(WORKSPACE, MASK_DIR_REL)
    os.makedirs(mask_dir, exist_ok=True)
    alpha_rel = f"{MASK_DIR_REL}/mask-bgmodel-draft-alpha.png"
    bin_rel = f"{MASK_DIR_REL}/mask-bgmodel-draft-binary.png"
    with open(os.path.join(WORKSPACE, alpha_rel), "wb") as f:
        f.write(est["mask_alpha_png"])
    with open(os.path.join(WORKSPACE, bin_rel), "wb") as f:
        f.write(est["mask_binary_png"])

    spec = compose.register_mask(
        identity_id=IDENTITY_ID, source_asset_id=hero["id"],
        mask_binary_png=est["mask_binary_png"], mask_alpha_png=est["mask_alpha_png"],
        params=est["params"], origin="algorithm_draft", storage_uri=alpha_rel,
        source_evidence=f"由 {hero['id']} 经 bgmodel 自动分割生成（草稿，待人工目视确认）",
    )
    print(f"\n[mask 登记] spec={spec['id']} asset={spec['mask_asset_id']} 状态={spec['state']}")

    # 4) 背景素材（程序生成，无第三方权利）
    bg_dir = os.path.join(WORKSPACE, BG_DIR_REL)
    os.makedirs(bg_dir, exist_ok=True)
    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)

    produced: List[Dict[str, Any]] = []
    for bg_spec in BACKGROUNDS:
        if bg_spec["kind"] == "solid":
            src = "程序生成纯色背景，不含第三方素材"
        else:
            src = f"程序生成渐变背景（{bg_spec['top']}→{bg_spec['bottom']}），不含第三方素材"
        bg_img = make_background(bg_spec, (hero["width"], hero["height"]))
        fname = f"{bg_spec['name']}.png"
        bg_rel = f"{BG_DIR_REL}/{fname}"
        bg_bytes = _png_bytes(bg_img)
        with open(os.path.join(WORKSPACE, bg_rel), "wb") as f:
            f.write(bg_bytes)

        lic = licensing.register(subject=f"背景素材 {bg_spec['name']}", kind="image_source",
                                 scope="commercial_ok",
                                 evidence=src + "；由本工程程序生成，无第三方权利",
                                 verified_by="project",
                                 note="程序生成素材；如需对外发布请自行复核")
        bg_asset = registry.register_asset(
            identity_id=IDENTITY_ID, kind="background", storage_uri=bg_rel,
            file_hash=_sha(bg_bytes), source_evidence=src, license_id=lic["id"],
            width=hero["width"], height=hero["height"],
            note=f"背景变体素材：{bg_spec['name']}",
        )

        out = compose.compose(IDENTITY_ID, hero["id"], spec["mask_asset_id"], bg_asset["id"],
                              out_uri=f"{OUT_DIR_REL}/人物B-{bg_spec['name']}.png")
        out_path = os.path.join(WORKSPACE, OUT_DIR_REL, f"人物B-{bg_spec['name']}.png")
        with open(out_path, "wb") as f:
            f.write(out["composed_png"])

        rec = {
            "background": bg_spec["name"],
            "background_asset_id": bg_asset["id"],
            "file": f"{OUT_DIR_REL}/人物B-{bg_spec['name']}.png",
            "output_sha256": out["output_sha256"],
            "production_ready": out["production_ready"],
            "candidate": out["candidate"],
            "hard_protect_pixel_identical": out["hard_protect_check"]["pixel_identical"],
            "hard_protect_max_diff": [r["max_channel_diff"]
                                      for r in out["hard_protect_check"]["regions"]],
            "hard_protect_enforced": out["hard_protect_enforced"],
            "blocking_reasons": out["blocking_reasons"],
            "identity_claim": out["identity_claim"],
        }
        produced.append(rec)
        print(f"\n[合成] {bg_spec['name']}：{rec['file']}")
        print(f"       硬保护区逐像素一致={rec['hard_protect_pixel_identical']}"
              f"（最大通道差 {rec['hard_protect_max_diff']}）")
        print(f"       production_ready={rec['production_ready']} 候选={rec['candidate']}")
        if rec["blocking_reasons"]:
            print(f"       阻塞原因：{rec['blocking_reasons']}")

    report = {
        "source_asset_id": hero["id"],
        "source_file_hash": hero["file_hash"],
        "mask_spec_id": spec["id"],
        "mask_asset_id": spec["mask_asset_id"],
        "mask_state": spec["state"],
        "mask_params": est["params"],
        "mask_metrics": est["metrics"],
        "precheck": pre,
        "backgrounds": produced,
        "policy": "mask 为自动草稿，未确认前一律为候选，不得定版进入生产",
    }
    with io.open(os.path.join(out_dir, "合成报告.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告：{OUT_DIR_REL}/合成报告.json")
    print(f"全部 {len(produced)} 组均为候选（需人工确认 mask 后才可定版）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
