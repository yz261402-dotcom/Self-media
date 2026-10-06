"""探针：确定性抠图质量实测（两种算法对比 + 前置适配性预检）。

背景：gpt-image-2 中转忽略 mask 参数，同一人物换背景必然重绘人脸（IE-001/IE-002 已证）。
改用确定性路线：算法抠图 → 人物区域逐像素复制到新背景，人脸零改动。

为什么不用 rembg/U2Net：其权重许可与 InsightFace 同类问题（训练数据与授权不明确），本工程不引入。
GrabCut / 颜色键控都是**无预训练权重**的算法，许可随 OpenCV（Apache-2.0）。

前置预检（background_contrast）：
  自动指标只能发现"多保留/边缘破碎"，无法发现"该保留的被切掉"。
  CS-001 首轮即为此类失败——指标全达标但整块西装被切。
  故先测"人物与背景的可分性"，不可分时直接判 review，不产出废品。

用法：
  .venv/Scripts/python.exe scripts/probe_matting.py --method colorkey
"""
from __future__ import annotations

import argparse
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

HERO_REL = "assets/固定主播/主播B/版本1/基础人物.png"
OUT_DIR_REL = "experiments/抠图合成验证/CS-001"

# 硬保护区（必须逐像素保留，不允许算法改动）。
# 教训（CS-001 第三轮）：早期把"头部矩形"也设为硬保护，矩形内含背景，
# 合成后头顶留下一块原背景方斑。硬保护区必须**紧贴身份特征且不含背景**。
HARD_PROTECT: List[Tuple[int, int, int, int]] = [
    (330, 200, 560, 500),   # 人脸核心区：眉到下巴，紧贴面部
]
# 仅作算法提示（不作硬约束）：头部与发型大致范围
HEAD_ELLIPSE = (440, 365, 272, 350)  # cx, cy, rx, ry
# 背景采样区（四角），用于估计背景色统计
CORNER = 110
# 用于适配性预检的"人物核心带"（一定属于人物的区域）
SUBJECT_CORE: Tuple[int, int, int, int] = HARD_PROTECT[0]


def _hash_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def background_contrast(img_bgr) -> Dict[str, Any]:
    """适配性预检：人物核心区与背景采样区的颜色可分性。

    返回人物→背景的最近距离中位数（越小越不可分）。
    """
    import cv2
    import numpy as np

    h, w = img_bgr.shape[:2]
    corners = []
    for (x0, y0) in [(0, 0), (w - CORNER, 0), (0, h - CORNER), (w - CORNER, h - CORNER)]:
        corners.append(img_bgr[y0:y0 + CORNER, x0:x0 + CORNER].reshape(-1, 3).astype(np.float32))
    bg = np.vstack(corners)

    x0, y0, x1, y1 = SUBJECT_CORE
    subj = img_bgr[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)

    # 采样以控制计算量
    subj = subj[::7]
    bg = bg[::11]
    # 每个"人物"像素到最近背景色样本的距离中位数
    d = np.linalg.norm(subj[:, None, :] - bg[None, :, :], axis=2)
    nearest = d.min(axis=1)
    bg_std = float(bg.std(axis=0).mean())
    return {
        "background_sample_std": round(bg_std, 2),
        "nearest_bg_distance_median": round(float(np.median(nearest)), 2),
        "nearest_bg_distance_p10": round(float(np.percentile(nearest, 10)), 2),
        "samples": {"subject": int(subj.shape[0]), "background": int(bg.shape[0])},
    }


def run_grabcut(img_bgr, rect: Tuple[int, int, int, int], iters: int,
                protect: List[Tuple[int, int, int, int]], border: int = 10):
    """GrabCut 初始化。

    教训（CS-001 首轮）：若把 rect 内缩一段作为"可能背景"，而人物本身贴到画面边缘，
    那段边框会压在人物衣物上，导致整块外套被判为背景。故改为：
      · rect 覆盖全画面；只有最外圈 border 像素标为"确定背景"
      · rect 内一律"可能前景"，不预置"可能背景"
    """
    import cv2
    import numpy as np

    h, w = img_bgr.shape[:2]
    gc = np.zeros((h, w), dtype=np.uint8)
    gc[:] = cv2.GC_PR_FGD

    x0, y0, x1, y1 = rect
    gc[:max(0, y0 + border), :] = cv2.GC_BGD
    gc[max(0, y1 - border):, :] = cv2.GC_BGD
    gc[:, :max(0, x0 + border)] = cv2.GC_BGD
    gc[:, max(0, x1 - border):] = cv2.GC_BGD
    for (bx0, by0, bx1, by1) in protect:
        gc[by0:by1, bx0:bx1] = cv2.GC_FGD

    bgd_model = np.zeros((1, 65), dtype=np.float64)
    fgd_model = np.zeros((1, 65), dtype=np.float64)
    cv2.grabCut(img_bgr, gc, None, bgd_model, fgd_model, iters, cv2.GC_INIT_WITH_MASK)
    return np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)


def run_colorkey(img_bgr, protect: List[Tuple[int, int, int, int]],
                 k: float = 3.0, close: int = 9, open_: int = 5):
    """颜色键控：背景为均匀影棚渐变时，按"到背景色样本集的最近距离"分离前景。

    阈值由背景自身的距离分布自适应：thr = 背景内部距离 p99 * k + 余量。
    相比 GrabCut，本方法对"人物与背景色差小"的容忍度更低但不会凭空切块，
    且距离图可直接量化，便于判定是否可用。
    """
    import cv2
    import numpy as np

    h, w = img_bgr.shape[:2]
    px = img_bgr.astype(np.float32)

    corners = []
    for (x0, y0) in [(0, 0), (w - CORNER, 0), (0, h - CORNER), (w - CORNER, h - CORNER)]:
        corners.append(px[y0:y0 + CORNER, x0:x0 + CORNER].reshape(-1, 3))
    bg = np.vstack(corners)
    if bg.shape[0] > 300:
        step = bg.shape[0] // 300
        bg = bg[::step][:300]

    # 到背景样本的最小距离图：按行条带分块，避免 (H, W, N, 3) 爆内存
    dist = np.empty((h, w), dtype=np.float32)
    band = max(1, int(2.4e7 / (w * bg.shape[0])))  # 控制单块元素数
    for y0 in range(0, h, band):
        y1 = min(h, y0 + band)
        seg = px[y0:y1]                                    # (b, w, 3)
        d = np.linalg.norm(seg[:, :, None, :] - bg[None, None, :, :],
                           axis=3).min(axis=2)             # (b, w)
        dist[y0:y1] = d

    # 背景内部的距离基准：只看四角区内部。
    # 教训：若用"画面最外圈"，人物贴到画面下沿时会把衣物像素算进背景基准，
    # 导致阈值被抬到极大、前景几乎为空（CS-001 第二轮）。
    m = 6
    rim_parts = []
    for (x0, y0) in [(0, 0), (w - CORNER, 0), (0, h - CORNER), (w - CORNER, h - CORNER)]:
        rim_parts.append(dist[y0 + m:y0 + CORNER - m, x0 + m:x0 + CORNER - m].ravel())
    rim = np.concatenate(rim_parts)
    bg_p99 = float(np.percentile(rim, 99.5))
    thr = bg_p99 * 1.5 + 3.0

    fg = (dist > thr).astype(np.uint8) * 255
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close, close))
    k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (open_, open_))
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k_close)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k_open)
    for (bx0, by0, bx1, by1) in protect:
        fg[by0:by1, bx0:bx1] = 255
    return fg, {"bg_rim_p99": round(bg_p99, 2), "threshold": round(thr, 2),
                "dist_grid": "逐像素到背景样本集的最小欧氏距离"}


def run_bgmodel(img_bgr, protect: List[Tuple[int, int, int, int]],
                iters: int = 5, deg: int = 3, thr_scale: float = 2.2):
    """背景曲面建模：把背景当作"空间低频渐变"拟合，用残差分离前景。

    动机：米白西装与浅灰背景的**绝对色差**太小，任何按绝对颜色分割的方法都会失手。
    但背景是平滑渐变（低频），衣物有结构（高频）。先用四角初筛出背景像素，
    鲁棒拟合多项式颜色曲面，再按残差分割——消除渐变后可分性显著提高。
    """
    import cv2
    import numpy as np

    h, w = img_bgr.shape[:2]
    px = img_bgr.astype(np.float32) / 255.0
    rgb = px.reshape(-1, 3)

    yy, xx = np.mgrid[0:h, 0:w]
    X = (xx / w).astype(np.float32).ravel()
    Y = (yy / h).astype(np.float32).ravel()
    # 阶数越高越能贴合"渐变 + 局部光斑"，但过高会开始拟合人物本身（deg<=4 为经验上限）
    terms = [np.ones_like(X), X, Y]
    if deg >= 2:
        terms += [X * X, X * Y, Y * Y]
    if deg >= 3:
        terms += [X ** 3, X * X * Y, X * Y * Y, Y ** 3]
    if deg >= 4:
        terms += [X ** 4, X ** 3 * Y, X * X * Y * Y, X * Y ** 3, Y ** 4]
    A = np.stack(terms, axis=1)                     # (N, k)

    # 初筛：与四角中位色接近的像素才可能是背景
    corners = []
    for (x0, y0) in [(0, 0), (w - CORNER, 0), (0, h - CORNER), (w - CORNER, h - CORNER)]:
        corners.append(px[y0:y0 + CORNER, x0:x0 + CORNER].reshape(-1, 3))
    med = np.median(np.vstack(corners), axis=0)
    sel = np.linalg.norm(rgb - med, axis=1) < 0.16

    coef = None
    for _ in range(iters):
        if sel.sum() < 500:
            break
        coef, *_ = np.linalg.lstsq(A[sel], rgb[sel], rcond=None)
        resid = np.linalg.norm(rgb - A @ coef, axis=1)
        thr = float(np.percentile(resid[sel], 80))
        new_sel = resid <= thr
        if new_sel.sum() < 500:
            break
        sel = new_sel

    if coef is None:
        return np.zeros((h, w), np.uint8), {"error": "背景拟合失败", "selected": int(sel.sum())}

    resid = np.linalg.norm(rgb - A @ coef, axis=1)
    rmap = resid.reshape(h, w).astype(np.float32)
    # 阈值：背景残差的稳健上界。宁可把边缘判给背景（宁缺勿残留）：
    # 残留的背景像素在换背景后会形成明显灰斑，而少保留 1–2px 发丝不影响身份。
    base = float(np.percentile(rmap[sel.reshape(h, w)], 99.5))
    thr = base * thr_scale + 0.02

    fg = (rmap > thr).astype(np.uint8) * 255
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k_close)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k_open)
    for (bx0, by0, bx1, by1) in protect:
        fg[by0:by1, bx0:bx1] = 255
    return fg, {"bg_residual_p99_5": round(base, 4), "threshold": round(thr, 4),
                "open_kernel": 11, "fitted_terms": len(terms),
                "selected_bg_pixels": int(sel.sum()),
                "note": "按拟合残差分割，不受绝对色差限制；阈值偏严格以避免背景残留"}


def fill_holes(fg):
    """填充前景内部孔洞：宁可多保留，不可把人物挖空。"""
    import cv2
    import numpy as np

    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(fg)
    cv2.drawContours(filled, contours, -1, 255, thickness=cv2.FILLED)
    return filled


def keep_subject_component(fg):
    """只保留包含画面中部的人物主连通块，去掉孤立噪块。"""
    import cv2
    import numpy as np

    n, labels, stats, cents = cv2.connectedComponentsWithStats(fg)
    h, w = fg.shape
    cx, cy = w / 2.0, h * 0.45
    best, best_d = -1, 1e18
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < 500:
            continue
        d = (cents[i][0] - cx) ** 2 + (cents[i][1] - cy) ** 2
        if d < best_d:
            best, best_d = i, d
    if best < 0:
        return fg
    return np.where(labels == best, 255, 0).astype(np.uint8)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", choices=["grabcut", "colorkey", "bgmodel"], default="bgmodel")
    ap.add_argument("--iters", type=int, default=8)
    ap.add_argument("--deg", type=int, default=4, help="背景曲面多项式阶数")
    ap.add_argument("--erode", type=int, default=2, help="边缘腐蚀像素数")
    ap.add_argument("--thr-scale", type=float, default=2.2, help="残差阈值系数")
    ap.add_argument("--border", type=int, default=10)
    ap.add_argument("--rect", default="", help="留空表示全画面")
    ap.add_argument("--bg", default="#dfe4ec", help="预览用纯色背景")
    args = ap.parse_args()

    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    hero_path = os.path.join(WORKSPACE, HERO_REL)
    hero_bytes = open(hero_path, "rb").read()
    hero = Image.open(io.BytesIO(hero_bytes)).convert("RGB")
    w, h = hero.size
    img_bgr = cv2.cvtColor(np.array(hero), cv2.COLOR_RGB2BGR)

    precheck = background_contrast(img_bgr)

    algo: Dict[str, Any] = {"name": args.method, "license": "Apache-2.0 (OpenCV)",
                            "pretrained_weights": None}
    if args.method == "grabcut":
        rect = (0, 0, w, h) if not args.rect else tuple(int(v) for v in args.rect.split(","))
        fg_raw = run_grabcut(img_bgr, rect, args.iters, HARD_PROTECT, args.border)
        algo.update({"iterations": args.iters, "border": args.border, "rect": list(rect)})
    elif args.method == "colorkey":
        fg_raw, key_info = run_colorkey(img_bgr, HARD_PROTECT)
        algo["colorkey"] = key_info
    else:
        fg_raw, bg_info = run_bgmodel(img_bgr, HARD_PROTECT, iters=args.iters, deg=args.deg,
                                      thr_scale=args.thr_scale)
        algo["name"] = f"bgmodel_deg{args.deg}"
        algo["bgmodel"] = bg_info

    fg_holes = fill_holes(fg_raw)
    fg = keep_subject_component(fg_holes)

    # 边缘精修：先轻微腐蚀（去掉贴轮廓的残留凸起），再生成软 alpha 羽化过渡。
    # 腐蚀会收掉 1–2px 发丝外缘，但不影响身份特征；残留灰斑在换背景后远比这显眼。
    erode = args.erode
    if erode > 0:
        ke = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (erode * 2 + 1, erode * 2 + 1))
        fg = cv2.erode(fg, ke)
    for (bx0, by0, bx1, by1) in HARD_PROTECT:
        fg[by0:by1, bx0:bx1] = 255
    alpha_soft = cv2.GaussianBlur(fg, (0, 0), sigmaX=1.2)
    for (bx0, by0, bx1, by1) in HARD_PROTECT:
        alpha_soft[by0:by1, bx0:bx1] = 255

    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)

    fg_bool = fg > 0
    fg_ratio = float(fg_bool.sum()) / (w * h)
    protect_cov = []
    for box in HARD_PROTECT:
        bx0, by0, bx1, by1 = box
        protect_cov.append(round(float(fg_bool[by0:by1, bx0:bx1].mean()), 4))
    n_comp = _components(fg) if hasattr(np, "ndarray") else 0
    kernel = np.ones((5, 5), np.uint8)
    border_px = cv2.morphologyEx(fg, cv2.MORPH_GRADIENT, kernel)
    edge_ratio = float((border_px > 0).sum()) / max(1, int(fg_bool.sum()))

    mask_img = Image.fromarray(fg)
    mask_path = os.path.join(out_dir, f"mask-{args.method}.png")
    mask_img.save(mask_path)
    alpha_path = os.path.join(out_dir, f"mask-alpha-{args.method}.png")
    Image.fromarray(alpha_soft).save(alpha_path)

    ov = hero.copy()
    d = ImageDraw.Draw(ov)
    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        pts = [tuple(map(int, p[0])) for p in c[::3]]
        if len(pts) > 2:
            d.line(pts + [pts[0]], fill=(230, 40, 40), width=3)
    ov_path = os.path.join(out_dir, f"轮廓叠加-{args.method}.png")
    ov.save(ov_path)

    bg_rgb = tuple(int(args.bg.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    bg_img = Image.new("RGB", (w, h), bg_rgb)
    alpha = Image.fromarray(alpha_soft)
    comp = Image.composite(hero, bg_img, alpha)
    comp_path = os.path.join(out_dir, f"合成预览-{args.method}.png")
    comp.save(comp_path)

    arr_a = np.array(hero)
    arr_c = np.array(comp)
    protect_zero = True
    for box in HARD_PROTECT:
        bx0, by0, bx1, by1 = box
        if int(np.abs(arr_a[by0:by1, bx0:bx1].astype(int)
                     - arr_c[by0:by1, bx0:bx1].astype(int)).max()) != 0:
            protect_zero = False

    result: Dict[str, Any] = {
        "probe": "matting",
        "method": args.method,
        "source": HERO_REL,
        "source_sha256": _hash_bytes(hero_bytes),
        "source_pixel_size": [w, h],
        "algorithm": algo,
        "precheck_background_contrast": precheck,
        "hard_protect_boxes": [list(b) for b in HARD_PROTECT],
        "metrics": {
            "foreground_ratio": round(fg_ratio, 4),
            "protect_box_coverage": protect_cov,
            "connected_components": n_comp,
            "edge_pixel_ratio": round(edge_ratio, 4),
            "edge_erode_px": args.erode,
            "alpha_feather_sigma": 1.2,
        },
        "outputs": {
            "mask_binary": f"{OUT_DIR_REL}/mask-{args.method}.png",
            "mask_sha256": _hash_bytes(open(mask_path, "rb").read()),
            "mask_alpha": f"{OUT_DIR_REL}/mask-alpha-{args.method}.png",
            "mask_alpha_sha256": _hash_bytes(open(alpha_path, "rb").read()),
            "overlay": f"{OUT_DIR_REL}/轮廓叠加-{args.method}.png",
            "composite_preview": f"{OUT_DIR_REL}/合成预览-{args.method}.png",
        },
        "protect_region_pixel_identical": protect_zero,
        "quality_verdict": _verdict(fg_ratio, protect_cov, n_comp, precheck),
        "metric_limitation": "自动指标只能发现'多保留/边缘破碎'，无法发现'该保留的被切掉'"
                             "（CS-001 首轮即为此类：指标全达标但整块西装被切）。"
                             "故一律要求人工目视确认后才能定版。",
    }
    with io.open(os.path.join(out_dir, f"指标-{args.method}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"预检 背景色标准差：{precheck['background_sample_std']}")
    print(f"预检 人物→背景最近距离 中位数/p10：{precheck['nearest_bg_distance_median']}"
          f" / {precheck['nearest_bg_distance_p10']}")
    print(f"方法：{args.method}")
    print(f"前景占比：{fg_ratio:.4f}｜保护框覆盖：{protect_cov}｜大块数：{n_comp}｜边缘占比：{edge_ratio:.4f}")
    print(f"保护框内逐像素一致：{protect_zero}")
    print(f"判定：{result['quality_verdict']}")
    print(f"产出：{OUT_DIR_REL}/")
    return 0


def _components(fg) -> int:
    import cv2

    area = fg.shape[0] * fg.shape[1]
    n2, _labels, stats, _ = cv2.connectedComponentsWithStats(fg)
    return sum(1 for i in range(1, n2) if stats[i, cv2.CC_STAT_AREA] >= area * 0.0005)


def _verdict(fg_ratio: float, protect_cov: List[float], n_comp: int,
             precheck: Dict[str, Any]) -> str:
    problems = []
    if precheck["nearest_bg_distance_p10"] < 12:
        problems.append(f"预检不通过：人物与背景像素最近距离 p10="
                        f"{precheck['nearest_bg_distance_p10']}（<12 视为色差不足，"
                        f"自动分割易切掉同色衣物）")
    if min(protect_cov) < 1.0:
        problems.append("保护框未被完整包含进前景")
    if fg_ratio < 0.08 or fg_ratio > 0.9:
        problems.append(f"前景占比异常（{fg_ratio:.2f}）")
    if n_comp != 1:
        problems.append(f"前景被切成 {n_comp} 块")
    return "自动分割候选可用，仍需人工目视确认" if not problems else "需人工介入：" + "；".join(problems)


if __name__ == "__main__":
    raise SystemExit(main())
