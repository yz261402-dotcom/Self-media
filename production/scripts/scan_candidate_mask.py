"""候选图 mask 参数扫描：为"人物置身场景"挑出真正能用的抠图配置。

为什么需要它（CS-006 实测的矛盾）：
  · C（全身 80%）与 D（全身 88%）**构图最理想**——人物头到鞋完整入画，
    左右/上都有真实像素余量，脚下还有地板与投影。这意味着 CS-005 里
    "缩小必露断面"的死结被解开了：人物根本没被画框裁断。
  · 但它们的**抠图最差**：米白西装压在浅灰无缝纸上，色差小；
    而且背景含"墙面→地面"两个平面 + 人物投影，单一四阶多项式曲面解释不了，
    残差图上于是冒出成片背景斑块，被算成前景（见 诊断mask叠加-*.png）。
  · F/E 背景换成蓝/灰后抠图好一些，但人物在小腿处被画框裁断 → 又回到断面问题。

本脚本对每张候选扫描 (deg, thr_scale)，如实报告：
  · 前景占比
  · **四边贴边像素**（人物若本就没贴边，任何贴边像素都是"背景被粘进来"）
  · 最大连通块 bbox / 面积占比 / 长宽比
  · 前景被切成几块、硬保护区覆盖率、usable / problems

判定完全由数字给出，不含主观评分：合格 = 贴边像素全 0 且前景为单连通块。
同时落盘 overlay 证据图，供人工目视复核（数字合格不等于视觉可用）。

红线：不做身份判断；不自动确认 mask（confirm_mask 必须人工）。
"""
from __future__ import annotations

import glob
import io
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

GEN_DIR_REL = "assets/固定主播/主播B/版本2候选/生成"
OUT_DIR_REL = "experiments/抠图合成验证/CS-006"
PROBE_REPORT_REL = OUT_DIR_REL + "/重绘探针报告.json"

# 只扫这几张：A/B 已被构图判死（人物贴住左右或整幅），扫了也没意义，还白烧内存。
CANDIDATES = [
    ("重绘C-全身80%", "02-50-29"),
    ("重绘D-全身88%", "02-50-32"),
    ("重绘E-中灰底·全身", "02-54-52"),
    ("重绘F-中蓝底·全身", "02-54-48"),
]
# deg 只扫 4/6/8：原实现 _poly_basis 静默把 >4 当成 4（见 compose.py 的缺陷修正说明），
# 修好后 6/8 阶才真正生效——本批候选的背景含"墙→地"两个平面 + 绘画性纹理，
# 正是需要更高阶的地方。
DEGS = (4, 6, 8)
THR_SCALES = (2.6, 3.0, 4.0, 6.0, 8.0)
MASK_COMMON = {"iters": 5, "alpha_source": "matting"}


def _face_cores() -> Dict[str, List[int]]:
    """从探针报告里取各候选图的人脸核心框（由人工核验过的基准模板对齐推出）。"""
    p = os.path.join(WORKSPACE, PROBE_REPORT_REL)
    if not os.path.exists(p):
        raise SystemExit(f"缺少探针报告，请先跑 probe_reference_redraw.py：{p}")
    d = json.load(open(p, encoding="utf-8"))
    out: Dict[str, List[int]] = {}
    for tag, g in d["geometry"].items():
        out[tag] = list(g["hard_protect"])
    return out


def _find(ts: str) -> str:
    hits = glob.glob(os.path.join(WORKSPACE, GEN_DIR_REL, f"*{ts}.png"))
    if not hits:
        raise SystemExit(f"找不到候选图（时间戳 {ts}）")
    return hits[0]


def _border_leak(ab, np) -> Dict[str, int]:
    return {"left": int(ab[:, 0].sum()), "right": int(ab[:, -1].sum()),
            "top": int(ab[0, :].sum()), "bottom": int(ab[-1, :].sum())}


def main() -> int:
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    from app.avatar import compose

    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)
    cores = _face_cores()

    def _font(size: int = 20):
        from PIL import ImageFont
        for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
        return ImageFont.load_default()

    fo = _font(20)
    report: Dict[str, Any] = {
        "purpose": "为候选图扫描 mask 参数，找出「不切人物、也不粘背景」的可用配置",
        "criterion": "合格 = 四边贴边像素全 0 且前景为单连通块（数字判据，不代表视觉可用）",
        "known_issue": "C/D 构图最理想但背景含墙+地两平面与投影，四阶曲面解释不了 → 残差成片；"
                       "F/E 抠图稍好但人物在小腿处被裁断。",
        "scan": {"deg": list(DEGS), "thr_scale": list(THR_SCALES), **MASK_COMMON},
        "candidates": {},
    }

    for tag, ts in CANDIDATES:
        path = _find(ts)
        hp = cores.get(tag)
        if not hp:
            print(f"[{tag}] 报告里没有该图的人脸核心框 → 跳过")
            continue
        rgb = np.array(Image.open(path).convert("RGB"))
        h, w = rgb.shape[:2]
        payload = io.BytesIO()
        Image.fromarray(rgb).save(payload, format="PNG")
        png = payload.getvalue()
        del rgb
        print(f"\n{'=' * 78}\n[{tag}]  {w}×{h}   人脸核心框 {hp}\n{'=' * 78}")

        rows: List[Dict[str, Any]] = []
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        best_ab = None
        for deg in DEGS:
            for ts_ in THR_SCALES:
                try:
                    est = compose.estimate_mask(png, method="bgmodel", hard_protect=[hp],
                                                deg=deg, thr_scale=ts_, **MASK_COMMON)
                except Exception as e:                       # 内存/拟合失败也要如实记账
                    print(f"  deg={deg} thr={ts_:>4}  → 失败：{type(e).__name__}: {e}")
                    rows.append({"deg": deg, "thr_scale": ts_, "error": f"{type(e).__name__}: {e}"})
                    continue
                alpha = np.array(Image.open(io.BytesIO(est["mask_alpha_png"])).convert("L"))
                ab = alpha > compose.MASK_BINARY_MAX
                del alpha
                leak = _border_leak(ab, np)
                leak_total = sum(leak.values())
                n_lab, lab, stats, _c = cv2.connectedComponentsWithStats(ab.astype(np.uint8))
                if n_lab > 1:
                    main = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                    mx, my = int(stats[main, cv2.CC_STAT_LEFT]), int(stats[main, cv2.CC_STAT_TOP])
                    mw, mh = int(stats[main, cv2.CC_STAT_WIDTH]), int(stats[main, cv2.CC_STAT_HEIGHT])
                    main_area = float(stats[main, cv2.CC_STAT_AREA]) / (w * h)
                    n_comp = n_lab - 1
                else:
                    mx = my = 0
                    mw, mh = w, h
                    main_area, n_comp = 1.0, 0
                aspect = round(mw / max(1, mh), 3)
                cov = est["metrics"]["protect_coverage"]
                row = {
                    "deg": deg, "thr_scale": ts_,
                    "foreground_ratio": est["metrics"]["foreground_ratio"],
                    "border_leak": leak, "border_leak_total": leak_total,
                    "main_bbox": [mx, my, mx + mw, my + mh],
                    "main_area_ratio": round(main_area, 4),
                    "main_aspect": aspect,
                    "connected_components": n_comp,
                    "protect_coverage": cov,
                    "usable": est["usable"], "problems": est["problems"],
                    "thr": est["params"].get("thr"),
                    "bg_residual_p99_5": est["params"].get("bg_residual_p99_5"),
                    "matting_usable": (est["params"].get("matting") or {}).get("usable"),
                }
                rows.append(row)
                ok = leak_total == 0 and n_comp == 1 and min(cov) >= 1.0
                flag = "✅" if ok else "  "
                print(f"{flag} deg={deg} thr={ts_:>4}  前景={row['foreground_ratio']:.3f}"
                      f"  贴边合计={leak_total:>6}  块数={n_comp}"
                      f"  主块占比={main_area * 100:5.1f}%  长宽比={aspect:.3f}"
                      f"  保护框={min(cov):.2f}")
                # 排序键：贴边最少优先，其次块数少，再其次主块占比合理（0.15~0.45）
                sane = 0.0 if 0.15 <= main_area <= 0.45 else abs(main_area - 0.28)
                score = (leak_total, n_comp, sane, -min(cov))
                if best is None or score < best[0]:
                    best, best_ab = (score, row), ab.copy()
        report["candidates"][tag] = {"file": os.path.relpath(path, WORKSPACE).replace("\\", "/"),
                                     "face_core": hp, "runs": rows}
        if best and best_ab is not None and best[0][0] == 0 and best[0][1] == 1:
            r = best[1]
            report["candidates"][tag]["best"] = r
            vis = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
            cnts, _ = cv2.findContours(best_ab.astype(np.uint8), cv2.RETR_LIST,
                                       cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(vis, cnts, -1, (0, 220, 0), 2)
            bx, by, bx1, by1 = r["main_bbox"]
            cv2.rectangle(vis, (bx, by), (bx1 - 1, by1 - 1), (255, 0, 220), 3)
            cv2.rectangle(vis, (hp[0], hp[1]), (hp[2] - 1, hp[3] - 1), (0, 200, 255), 3)
            im = Image.fromarray(vis)
            d = ImageDraw.Draw(im)
            d.text((10, 10), f"{tag}  最优配置 deg={r['deg']} thr_scale={r['thr_scale']}  "
                             f"前景={r['foreground_ratio']}", fill=(255, 255, 0), font=fo)
            d.text((10, 38), f"绿=mask边界  品红=最大连通块  青=人脸硬保护区  "
                             f"贴边={r['border_leak_total']}  块数={r['connected_components']}",
                   fill=(255, 255, 0), font=fo)
            safe = "".join(ch for ch in tag if ch.isalnum() or ch in "-_")
            im.save(os.path.join(out_dir, f"诊断mask最优-{safe}.png"))
            print(f"  → 找到合格配置，证据图：{OUT_DIR_REL}/诊断mask最优-{safe}.png")

    with io.open(os.path.join(out_dir, "候选mask扫描.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n扫描报告：{OUT_DIR_REL}/候选mask扫描.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
