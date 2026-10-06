"""把"版本2候选主图"合成进教室/办公室/公园场景——真正的"人物置身场景讲课"。

为什么需要新脚本（CS-006 的结论）：
  · 版本1 主图是胸像特写，躯干贴住左/右/下三边，画框一外扩就露断面（CS-005）。
  · 版本2 候选 C（全身 80%、浅灰无缝纸）人物**完整含鞋、四周都有真实像素**，
    贴边为 0 → 可以把人物缩小放进 9:16 画框而**不露任何断面**。
  · 但 C 的背景是绘画性纹理的无缝纸、含"墙→地"两个平面与投影，
    四阶曲面解释不了 → 残差冒出成片背景斑块，被"保留所有达标块"的策略收进 mask，
    于是剪影"贴边"，把构图判死。改用 component_mode="seed"（只留含人脸种子的连通块）
    后，全 mask 四边贴边从 194px 降到 **0**，人形长宽比 0.284。

产物一律为**候选**：mask 是自动草稿且 seed 策略有损，未经人工目视确认不得定版。
不做身份判定，不声称"这是同一个人"；只声明"硬保护区内逐像素与源图相同"这一数学事实。

用法：
  cd production/backend && .venv/Scripts/python.exe ../scripts/compose_candidate_scene_variants.py
"""
from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

IDENTITY_ID = "av_base_b_v1"
CAND_TS = "02-50-29"                 # 重绘C-全身80%（浅灰无缝纸）
CAND_TAG = "重绘C-全身80%"
CAND_SRC_REL = "assets/固定主播/主播B/版本2候选/生成"
CAND_OUT_REL = "assets/固定主播/主播B/版本2候选/规范/全身80-浅灰底.png"
MASK_DIR_REL = "assets/固定主播/主播B/版本2候选/遮罩"
OUT_DIR_REL = "experiments/抠图合成验证/CS-006/场景合成"
PROBE_REPORT_REL = "experiments/抠图合成验证/CS-006/重绘探针报告.json"
BG_PREFIX = "assets/背景/场景AI/规范/"

# 扫描+诊断得出的可用配置（见 CS-006/候选mask扫描.json、诊断连通块报告.json）
MASK_PARAMS = {"deg": 4, "iters": 5, "thr_scale": 8.0, "alpha_source": "matting",
               "component_mode": "seed"}
CANVAS_ASPECT = 9 / 16               # 竖屏短视频
TARGET_PERSON_RATIO = 0.62           # 人物占画框高度的目标比例
TARGET_HEAD_TOP = 0.08               # 头顶落在画框高度的 8% 处


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _face_core() -> List[int]:
    p = os.path.join(WORKSPACE, PROBE_REPORT_REL)
    d = json.load(open(p, encoding="utf-8"))
    box = d["geometry"][CAND_TAG]["hard_protect"]
    return [int(v) for v in box]


def normalize_candidate() -> Tuple[str, Dict[str, Any]]:
    """规范化 = 落盘到稳定路径并记录哈希。

    **不做任何重采样/裁切**：项目哲学是"人物层 1:1 逐像素复制"，任何缩放都会
    破坏"硬保护区逐像素相同"这条可验证保证；平台水印位于人物轮廓之外的背景区
    （mask 里 α=0），合成时会被新场景像素覆盖，不需要靠裁切去排除——
    这一点在下面用水印框内的 α 前景占比实测校验，而不是口头声明。
    """
    src = glob.glob(os.path.join(WORKSPACE, CAND_SRC_REL, f"*{CAND_TS}.png"))
    if not src:
        raise SystemExit(f"找不到候选图（时间戳 {CAND_TS}）")
    dst = os.path.join(WORKSPACE, CAND_OUT_REL)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    raw = open(src[0], "rb").read()
    if not os.path.exists(dst) or open(dst, "rb").read() != raw:
        shutil.copyfile(src[0], dst)
    from PIL import Image

    w, h = Image.open(dst).size
    info = {"source_file": os.path.relpath(src[0], WORKSPACE).replace("\\", "/"),
            "normalized_file": CAND_OUT_REL, "size": [w, h],
            "sha256": _sha(open(dst, "rb").read()),
            "pixel_ops": "无（1:1 字节复制；不重采样、不裁切、不修饰）"}
    if _sha(open(dst, "rb").read()) != _sha(raw):
        raise SystemExit("规范化过程中文件字节发生变化：违反 1:1 复制约束")
    return CAND_OUT_REL, info


def main() -> int:
    import numpy as np
    from PIL import Image, ImageDraw

    from app import storage as st
    from app.avatar import compose, registry

    st.init_db()
    ident = registry.get_identity(IDENTITY_ID)
    if not ident:
        raise SystemExit(f"人物不存在：{IDENTITY_ID}")
    v1_hero_id = ident.get("hero_asset_id") or ""
    v1_hero = registry.get_asset(v1_hero_id)
    if not v1_hero:
        raise SystemExit(f"版本1 主图资产不存在：{v1_hero_id}")

    rel, norm = normalize_candidate()
    print("一、规范化候选主图")
    print(f"   {norm['source_file']} → {rel}   {norm['size'][0]}×{norm['size'][1]}")
    print(f"   像素操作：{norm['pixel_ops']}")
    print(f"   哈希：{norm['sha256'][:26]}…")

    hero = registry.register_asset(
        identity_id=IDENTITY_ID, kind="hero", storage_uri=rel, file_hash=norm["sha256"],
        source_evidence=f"AI 图生图重绘（参考图={v1_hero_id}）；"
                        f"通道与局限见 {PROBE_REPORT_REL}；非真实人物，未做身份判定",
        license_id=v1_hero.get("license_id"),
        supersedes_id=v1_hero_id,
        width=norm["size"][0], height=norm["size"][1],
        note="版本2候选主图：更宽构图（人物完整含鞋、四周留余量）。"
             "候选状态，未定版、未冻结身份",
    )
    print(f"\n二、登记为候选主图资产（不覆盖版本1）")
    print(f"   hero 资产 {hero['id']}  version={hero['version']}  "
          f"supersedes={hero['supersedes_id']}  approval={hero['approval_state']}")
    print(f"   身份仍引用版本1 主图：hero_asset_id={ident.get('hero_asset_id')}"
          f"  frozen={ident.get('frozen')}")

    # ---- mask ----
    hp = _face_core()
    print(f"\n三、生成 mask（deg=4, thr_scale=8.0, component_mode=seed）")
    print(f"   人脸核心框（人工核验的基准模板对齐推出）：{hp}")
    hero_bytes = open(os.path.join(WORKSPACE, rel), "rb").read()
    est = compose.estimate_mask(hero_bytes, method="bgmodel", hard_protect=[hp], **MASK_PARAMS)
    p = est["params"]
    print(f"   前景占比={est['metrics']['foreground_ratio']}  块数={est['metrics']['connected_components']}")
    print(f"   四边贴边={p['border_touch_pixels']} → {p['border_touching_edges'] or '无（可自由缩小）'}")
    print(f"   丢弃非种子块 {p['dropped_component_count']} 个，合计 {p['dropped_area']} 像素")
    mm = p.get("matting") or {}
    print(f"   matting usable={mm.get('usable')}  种子距离={mm.get('seed_dist_used')}px "
          f"不确定区={mm.get('uncertain_pixels')} 病态={mm.get('degenerate_pixels')}")
    if not mm.get("usable"):
        raise SystemExit(f"matting 不可用：{mm.get('reason')}")
    if p["border_touching_edges"]:
        raise SystemExit(f"剪影仍贴边 {p['border_touching_edges']}：缩小置入场景会露断面")

    mask_dir = os.path.join(WORKSPACE, MASK_DIR_REL)
    os.makedirs(mask_dir, exist_ok=True)
    alpha_rel = f"{MASK_DIR_REL}/版本2C-seed-matting-draft-alpha.png"
    bin_rel = f"{MASK_DIR_REL}/版本2C-seed-matting-draft-binary.png"
    for r, key in ((alpha_rel, "mask_alpha_png"), (bin_rel, "mask_binary_png")):
        with open(os.path.join(WORKSPACE, r), "wb") as f:
            f.write(est[key])
    spec = compose.register_mask(
        identity_id=IDENTITY_ID, source_asset_id=hero["id"],
        mask_binary_png=est["mask_binary_png"], mask_alpha_png=est["mask_alpha_png"],
        params=est["params"], origin="algorithm_draft", storage_uri=alpha_rel,
        source_evidence=f"由 {hero['id']} 经 bgmodel(deg=4,thr_scale=8.0)+局部F/B matting"
                        f"+seed 连通块策略生成（草稿，待人工确认）",
    )
    print(f"   mask 资产 {spec['mask_asset_id']}  state={spec['state']}  usable={est['usable']}")

    # 水印是否落在背景区（决定它会不会被带进产物）
    ab = np.array(Image.open(io.BytesIO(est["mask_alpha_png"]))).astype(np.int32) > compose.MASK_BINARY_MAX
    pr = json.load(open(os.path.join(WORKSPACE, PROBE_REPORT_REL), encoding="utf-8"))
    wm = pr["watermark_hits"].get(CAND_TAG)
    wm_ratio = None
    if wm:
        x0, y0, x1, y1 = [int(v) for v in wm]
        pad = 6
        sub = ab[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
        wm_ratio = round(float(sub.mean()), 6)
        print(f"   水印框 {wm} 内 mask 前景占比 = {wm_ratio}"
              f"（0 表示该区域是背景，合成时由场景像素覆盖，水印不会被带入产物）")

    # ---- 画框：人物缩小置入 9:16 画布 ----
    rows = np.where(ab.any(axis=1))[0]
    cols = np.where(ab.any(axis=0))[0]
    px0, py0, px1, py1 = int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())
    src_w, src_h = norm["size"]
    ph = py1 - py0 + 1
    ch = int(round(ph / TARGET_PERSON_RATIO))
    cw = int(round(ch * CANVAS_ASPECT))
    ox = int(round(cw / 2 - (px0 + px1) / 2))
    oy = int(round(TARGET_HEAD_TOP * ch - py0))
    frame = (ox, oy, cw, ch)
    print(f"\n四、构图（人物 1:1 复制，只改画框）")
    print(f"   源图人物剪影 bbox={[px0, py0, px1, py1]}  高 {ph}px（占源图 {ph / src_h * 100:.1f}%）")
    print(f"   画框 {cw}×{ch} @ 偏移 ({ox},{oy})  9:16={abs(cw / ch - 9 / 16) < 1e-3}")
    print(f"   人物在画框中占高 {ph / ch * 100:.1f}%，头顶留白 {py0 + oy}px")

    # ---- 逐场景合成 ----
    bgs = [a for a in registry.list_assets(IDENTITY_ID, "background")
           if str(a.get("storage_uri", "")).startswith(BG_PREFIX)]
    uniq: Dict[str, Dict[str, Any]] = {}
    for a in sorted(bgs, key=lambda x: x.get("created_at") or ""):
        uniq[a["storage_uri"]] = a
    bgs = list(uniq.values())
    if not bgs:
        raise SystemExit("未找到场景背景资产；请先运行 scripts/ingest_scene_backgrounds.py")

    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n五、合成到 {len(bgs)} 个场景")
    produced: List[Dict[str, Any]] = []
    thumbs: List[Tuple[str, Any]] = []
    for bg in bgs:
        name = os.path.splitext(os.path.basename(bg["storage_uri"]))[0]
        fname = f"人物B-版本2C-{name}.png"
        out = compose.compose(IDENTITY_ID, hero["id"], spec["mask_asset_id"], bg["id"],
                              out_uri=f"{OUT_DIR_REL}/{fname}", edge_mode="unmix", frame=frame)
        with open(os.path.join(out_dir, fname), "wb") as f:
            f.write(out["composed_png"])
        fr = out["frame"]
        rec = {
            "scene": name,
            "background_asset_id": bg["id"],
            "background_license_id": bg.get("license_id"),
            "file": f"{OUT_DIR_REL}/{fname}",
            "output_sha256": out["output_sha256"],
            "edge_mode": out["edge_mode"],
            "edge_residual": out["edge_residual"],
            "hard_protect_pixel_identical": out["hard_protect_check"]["pixel_identical"],
            "hard_protect_max_diff": [r["max_channel_diff"]
                                      for r in out["hard_protect_check"]["regions"]],
            "frame": fr,
            "person_ratio_in_canvas": round(ph / fr["canvas_size"][1], 4),
            "revealed_cut": fr["revealed_cut"],
            "production_ready": out["production_ready"],
            "candidate": out["candidate"],
            "blocking_reasons": out["blocking_reasons"],
            "identity_claim": out["identity_claim"],
            "watermark_region_alpha_fg_ratio": wm_ratio,
        }
        produced.append(rec)
        print(f"   [合成] {name:<10} {fr['canvas_size'][0]}×{fr['canvas_size'][1]}  "
              f"人物占高 {rec['person_ratio_in_canvas'] * 100:.1f}%  "
              f"背景放大 {fr['background_upscale']}×  "
              f"断面={fr['revealed_cut'] or '无'}  "
              f"硬保护区逐像素一致={rec['hard_protect_pixel_identical']} "
              f"最大差={rec['hard_protect_max_diff']}")
        im = Image.open(os.path.join(out_dir, fname)).convert("RGB")
        thumbs.append((name, im.resize((300, int(300 * im.height / im.width)))))

    # ---- 对照图 ----
    gap, lab = 12, 30
    sheet = Image.new("RGB", (len(thumbs) * (300 + gap) + gap,
                              max(t[1].height for t in thumbs) + lab + gap), (250, 250, 248))
    d = ImageDraw.Draw(sheet)
    for i, (name, im) in enumerate(thumbs):
        x = gap + i * (300 + gap)
        sheet.paste(im, (x, lab))
        d.text((x, 6), name, fill=(20, 20, 20))
    sheet.save(os.path.join(out_dir, "对照-版本2C-场景合成.png"))

    report = {
        "purpose": "把版本2候选主图（全身 80%、浅灰无缝纸）合成进场景，"
                   "实现真正的「人物置身场景」，且不露任何断面",
        "identity_id": IDENTITY_ID,
        "candidate": {"tag": CAND_TAG, "normalized": norm, "hero_asset_id": hero["id"],
                      "supersedes": v1_hero_id, "approval_state": hero["approval_state"],
                      "identity_still_uses": ident.get("hero_asset_id")},
        "mask": {"mask_spec_id": spec["id"], "mask_asset_id": spec["mask_asset_id"],
                 "state": spec["state"], "usable": est["usable"],
                 "problems": est["problems"], "params": est["params"],
                 "geometry": {"silhouette_bbox": [px0, py0, px1, py1],
                              "person_height_px": ph,
                              "person_height_ratio_in_source": round(ph / src_h, 4),
                              "margins_px": {"left": px0, "right": src_w - 1 - px1,
                                             "top": py0, "bottom": src_h - 1 - py1},
                              "border_touching_edges": p["border_touching_edges"]}},
        "frame": {"canvas": [cw, ch], "offset": [ox, oy],
                  "target_person_ratio": TARGET_PERSON_RATIO,
                  "note": "人物层 1:1 逐像素复制；缩放只改画框，硬保护区断言强度不下降"},
        "scenes": produced,
        "policy": "mask 为自动草稿且采用了有损的 seed 连通块策略，未经人工目视确认"
                  "一律为候选，不得定版进入生产；不做身份识别，不声称身份一致；"
                  "身份是否成立由人工目视对照图判定",
    }
    with io.open(os.path.join(out_dir, "场景合成报告.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n对照图：{OUT_DIR_REL}/对照-版本2C-场景合成.png")
    print(f"报告：{OUT_DIR_REL}/场景合成报告.json")
    print(f"全部 {len(produced)} 组均为候选"
          f"（production_ready={[r['production_ready'] for r in produced]}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
