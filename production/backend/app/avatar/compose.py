"""确定性抠图合成：用"算法分割 + 人物区逐像素复制"实现背景变体。

为什么需要这个模块（T03 实证结论，见 experiments/图像编辑验证/结论.md 与 抠图合成验证/结论.md）：
  · gpt-image 系列中转忽略 mask 参数，任何"同参考图编辑"都会重绘人脸 → 身份漂移，不满足 AC-AV-02。
  · 因此同一人物换背景只能走确定性路线：人物像素逐个复制，不做生成。

红线（本模块的硬约束）：
  1. **不做身份识别**：模块不声称"这就是同一个人"。它保证的是数学事实——硬保护区内像素
     与源图**完全相同**（最大通道差 == 0）。整体身份结论仍由人工/审核门禁负责。
  2. **自动 mask 只是草稿**：未经人工目视确认的 mask 只能产出候选，不得定版进入生产。
  3. **不使用预训练权重模型**：只用无权重算法（GrabCut / 颜色键控 / 背景曲面拟合），
     避免重蹈 InsightFace 权重不可商用的覆辙。
  4. **宁可少保留，不可留残留**：残留的原背景在换背景后形成醒目灰斑；少 1–2px 发丝不影响身份。
  5. **边缘去污不改变硬保护区**：unmix 校正量在 alpha==1 处恒为 0，因此身份断言不受影响。

合成模式（edge_mode）：
  · "alpha" —— 常规 alpha 混合。半透明边缘直接取源图像素，故源图原背景的贡献被保留，
               换成深色/异色背景时会在轮廓外形成一圈可见"灰晕"。纯色浅背景时代价很小。
  · "unmix" —— 代数去污。利用原图成像方程 C = αF + (1-α)B_orig 与目标方程
               C' = αF + (1-α)B_new 相减，得 C' = C + (1-α)·(B_new - B_orig)。
               无需除以 α（不放大噪声）、无需估计 F（不引入生成），是确定性闭式解。
               α==1 处校正量严格为 0 → 硬保护区逐像素不变。

人物大小（person_scale / frame）——**画框式缩放**：
  缩放的本质是"人物在画面里占多大"，而不是"人物有多少像素"。本模块据此把两者解耦：
  人物层**永远 1:1 逐像素复制**，缩放只改**画框**（crop / pad）。因此
  `person_scale != 1` 时硬保护区逐像素相同的断言**依然成立**（断言按整数偏移映射）。
    · person_scale > 1（人物更大）—— 画框内收（裁切）。副作用：输出分辨率下降，
      且必须给头顶留出余量，否则会把头切掉（FRAME_CUTS_HEAD）。
    · person_scale < 1（人物更小）—— 画框外扩（补边），多出的区域由场景背景填补。
      代价：**源图是一张有边界的照片**。人物被源图边界裁断之处，一旦该边界落进画框内部，
      断面就会以一条直线切口暴露出来（人物像纸片）。故逐条边界量化检测（见
      `detect_frame_reveal`），默认**硬拒绝**（FRAME_REVEALS_SOURCE_CUT），
      除非调用方显式 `allow_frame_reveal=True`（用于对照实验，不作为生产路径）。
    真要用更小的画中人物，正解是换一张**更宽构图的主图**（人物四周要有真实像素），
    而不是把现有特写往外放——后者露出的断面无法用算法补出来。
"""
from __future__ import annotations

import hashlib
import io
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import storage as st
from ..contracts import now_iso
from . import licensing, registry


class ComposeRejected(Exception):
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


# 默认硬保护区：人脸核心（眉到下巴）。必须紧贴身份特征且**不含背景**，
# 否则框内背景会被一并保留，合成后留下方斑（CS-001 第三轮的实证教训）。
DEFAULT_HARD_PROTECT: List[Tuple[int, int, int, int]] = [(330, 200, 560, 500)]
# 可分性预检下限：人物像素到背景样本的最近距离 p10 低于此值即判"色差不足"
MIN_SEPARABILITY_P10 = 12.0
# 支持的分割方法（全部无预训练权重）
METHODS = ("bgmodel", "grabcut", "colorkey")
# 合成模式：alpha=常规混合；unmix=边缘代数去污（换深色/异色背景推荐）
EDGE_MODES = ("alpha", "unmix")
# 连通块保留策略：
#   all  —— 保留所有面积达标的前景块（默认，历史行为；CS-002 要求不丢断开的衣物）
#   seed —— 只保留**含硬保护区的那一个连通块**（硬保护区经人工核验在人物内部，
#           故该块必属人物）。用于背景带绘画性纹理、残差被撑出成片背景斑块的素材；
#           属**有损**操作，必须人工目视确认（会自动计入 problems 以阻断自动通过）。
COMPONENT_MODES = ("all", "seed")
MASK_BINARY_MAX = 120  # mask 二值化阈值（0-255）
MIN_SEED_PIXELS = 3000  # matting 前景种子下限：低于此值说明该种子半径已不适用
# 画框式缩放的允许范围（越界即拒，不静默产出）
MIN_PERSON_SCALE = 0.4
MAX_PERSON_SCALE = 2.0
MIN_HEAD_MARGIN = 24      # 头顶到画框上沿的最小余量（px），低于此值判为"切到头顶"
MIN_REVEAL_STRIP_PX = 6   # 断面暴露宽度低于此值不算可见失效（1-2px 为舍入噪声）
MAX_BG_UPSCALE = 2.0      # 背景底板放大倍数上限，超出则提示背景会发虚


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _load(image_bytes: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(image_bytes))


def _to_bgr(image_bytes: bytes):
    import cv2
    import numpy as np

    rgb = np.array(_load(image_bytes).convert("RGB"))
    return rgb, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


# ---------------------------------------------------------------- 预检


def precheck_subject(image_bytes: bytes, hard_protect: Optional[Sequence[Sequence[int]]] = None,
                     corner: int = 110) -> Dict[str, Any]:
    """适配性预检：人物区与背景的可分性。

    自动指标只能发现"多保留/边缘破碎"，发现不了"该保留的被切掉"。
    故先判定这张图是否**值得**尝试自动分割：色差不足时直接判 review，不产出废品。
    """
    import numpy as np

    boxes = [tuple(b) for b in (hard_protect or DEFAULT_HARD_PROTECT)]
    rgb, _bgr = _to_bgr(image_bytes)
    h, w = rgb.shape[:2]

    corners = []
    for (x0, y0) in [(0, 0), (w - corner, 0), (0, h - corner), (w - corner, h - corner)]:
        corners.append(rgb[y0:y0 + corner, x0:x0 + corner].reshape(-1, 3).astype(np.float32))
    bg = np.vstack(corners)

    x0, y0, x1, y1 = boxes[0]
    subj = rgb[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)
    subj = subj[::7]
    bg_s = bg[::11]
    if subj.size == 0 or bg_s.size == 0:
        return {"ok": False, "nearest_bg_distance_p10": None,
                "nearest_bg_distance_median": None, "background_sample_std": None,
                "reasons": ["人物区或背景样本为空，无法预检"]}
    # 按行分块。一次性广播会生成 (N_subj × N_bg × 3) 的临时数组：
    # 真实尺寸图上实测为 9759×4400×3 float32 = 491MB，直接抛 ArrayMemoryError。
    # 小测试图看不出来，故此处按内存预算分块，保证大图可用。
    chunk = max(1, int(4_000_000 // max(1, bg_s.shape[0])))
    nearest = np.empty(subj.shape[0], dtype=np.float32)
    for s in range(0, subj.shape[0], chunk):
        blk = subj[s:s + chunk]
        nearest[s:s + chunk] = np.linalg.norm(
            blk[:, None, :] - bg_s[None, :, :], axis=2).min(axis=1)
    p10 = round(float(np.percentile(nearest, 10)), 2)
    median = round(float(np.median(nearest)), 2)

    reasons: List[str] = []
    if p10 < MIN_SEPARABILITY_P10:
        reasons.append(f"人物与背景色差不足（最近距离 p10={p10} < {MIN_SEPARABILITY_P10}）："
                       f"自动分割易切掉同色衣物，建议人工提供 mask")
    return {
        "ok": not reasons,
        "nearest_bg_distance_p10": p10,
        "nearest_bg_distance_median": median,
        "background_sample_std": round(float(bg.std(axis=0).mean()), 2),
        "reasons": reasons,
        "note": "预检通过也不代表分割可用：仍需人工目视确认（指标发现不了'该保留的被切掉'）",
    }


# ---------------------------------------------------------------- 自动草稿 mask


def _fit_background(rgb, deg: int, iters: int, corner: int = 110,
                    max_fit_samples: int = 200_000):
    """拟合背景颜色曲面（低频渐变），返回残差图、基准残差、参与拟合的样本数与项数。

    内存策略（真实尺寸图才暴露的缺陷）：多项式只有 10~15 项，用几万个抽样像素拟合
    已充分稳定，故**拟合只在抽样像素上做**，残差图按行分块计算。原实现一次性构建
    全图设计矩阵并对全图做布尔掩码索引，1024×1536 下峰值超 250MB，实测直接抛
    ArrayMemoryError —— 小测试图看不出来。
    """
    import numpy as np

    h, w = rgb.shape[:2]
    px = rgb.astype(np.float32) / 255.0
    flat = px.reshape(-1, 3)
    n = flat.shape[0]

    corners = []
    for (cx, cy) in [(0, 0), (w - corner, 0), (0, h - corner), (w - corner, h - corner)]:
        corners.append(px[cy:cy + corner, cx:cx + corner].reshape(-1, 3))
    med = np.median(np.vstack(corners), axis=0)

    step = max(1, int(np.ceil(n / float(max_fit_samples))))
    idx = np.arange(0, n, step, dtype=np.int64)
    # 阶数由调用方校验后原样使用。原实现这里是 max(2, deg)，会把 deg=0/1 **静默**抬成 2 ——
    # 又一个"参数看似生效实则无效"的隐患，一并去掉，让 deg 严格等于调用方所给。
    basis_deg = int(deg)
    A = _poly_basis(w, h, basis_deg, idx=idx)
    f_s = flat[idx]
    sel = np.linalg.norm(f_s - med, axis=1) < 0.16

    coef = None
    for _ in range(iters):
        if int(sel.sum()) < 500:
            break
        coef, *_ = np.linalg.lstsq(A[sel], f_s[sel], rcond=None)
        rs = np.linalg.norm(f_s - A @ coef, axis=1)
        thr = float(np.percentile(rs[sel], 80))
        new_sel = rs <= thr
        if int(new_sel.sum()) < 500:
            break
        sel = new_sel
    if coef is None:
        raise ComposeRejected("MASK_FIT_FAILED", "背景曲面拟合失败：可用背景像素过少")

    resid_map = _residual_map_blocks(rgb, coef, basis_deg)
    base = float(np.percentile(resid_map.reshape(-1)[idx][sel], 99.5))
    return resid_map, base, int(sel.sum()), int(A.shape[1])


def _propagate_nearest(img_rgb, seed, rounds: int, cv2, np):
    """把 seed 处已知的颜色向四周逐像素扩散，得到"离我最近的已知颜色"的逐像素估计。

    用于 matting 的**局部** F/B 估计。全局常量或全局曲面无法反映轮廓附近的局部明暗
    （例如头发阴影把紧邻的背景压暗了 40 个灰阶），那正是误判的源头。
    """
    val = np.where(seed[..., None], img_rgb.astype(np.float32), 0.0)
    w = seed.astype(np.float32)
    k = np.ones((3, 3), np.float32)
    for _ in range(max(0, rounds)):
        val_s = cv2.filter2D(val, -1, k, borderType=cv2.BORDER_REPLICATE)
        w_s = cv2.filter2D(w, -1, k, borderType=cv2.BORDER_REPLICATE)
        newly = (w <= 0) & (w_s > 0)
        if not newly.any():
            break
        val[newly] = val_s[newly] / w_s[newly][:, None]
        w[newly] = 1.0
    return val, w


def _alpha_by_matting(rgb, fg_bin, cv2, np, seed_max_dist: int = 24, rounds: int = 72,
                      bg_seed_erode: int = 4, t_bg: Optional[float] = None, t_amb: float = 8.0,
                      eps: float = 4.0, smooth: int = 3):
    """从图像反解真实 alpha（局部 F/B matting，无预训练权重）。

    为什么必须这么做：二值 mask 高斯羽化出的 alpha 与真实混合比例无关。
    头顶那一撮碎发在源图里与浅灰背景混合，真实 alpha 只有 0.1~0.5，羽化却给 1.0；
    而 unmix 去污在 alpha==1 处校正量恒为 0 —— 于是原背景被原样搬到深色背景上，
    形成肉眼可见的灰雾。

    解法是标准成像方程的闭式解：C = αF + (1-α)B  ⟹  α = (C-B)·(F-B) / |F-B|²。

    两个关键点（都是实测踩出来的）：
      1. F、B 必须取**局部**估计（离该像素最近的确定前景/背景），全局常量或全局曲面
         反映不了"头发阴影把紧邻背景压暗几十个灰阶"这种局部现象。
      2. **前景种子不能只按几何取**。若按"mask 腐蚀 r 像素"取种子，而初始 mask 恰好在
         轮廓外多包了一大片混合区，种子就落在混合区内部 —— 于是"离我最近的前景就是我"，
         d_f 恒为 0，alpha 恒为 1，matting 陷入循环论证。
         故种子必须再加一个条件：颜色**显著偏离局部背景**。
    """
    h, w = fg_bin.shape
    fg = fg_bin > 0
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                  (bg_seed_erode * 2 + 1, bg_seed_erode * 2 + 1))
    c = rgb.astype(np.float32)

    # --- 1) 背景种子：确定背景区（不足时退回画面四边条带） ---
    b_seed = cv2.erode((~fg).astype(np.uint8), k).astype(bool)
    if int(b_seed.sum()) < 1000:
        b = max(4, min(h, w) // 40)
        b_seed = np.zeros_like(fg)
        b_seed[:b, :] = True
        b_seed[h - b:, :] = True
        b_seed[:, :b] = True
        b_seed[:, w - b:] = True
        b_seed &= ~fg
    if not b_seed.any():
        return None, {"usable": False, "reason": "无法确定背景种子"}

    b_loc, b_ok = _propagate_nearest(rgb, b_seed, rounds, cv2, np)
    d_b = np.linalg.norm(c - b_loc, axis=2)
    covered_b = b_ok > 0

    # --- 2) 容差：由背景区自身残差分布自适应，而不是拍脑袋定死 ---
    bg_vals = d_b[(~fg) & covered_b]
    if bg_vals.size:
        tol = max(12.0, float(np.percentile(bg_vals, 99.0)) * 1.5)
    else:
        tol = 12.0
    if t_bg is not None:
        tol = max(tol, float(t_bg))

    # --- 3) 前景种子：既要颜色显著偏离背景，又要**几何上离轮廓足够远** ---
    # 只靠颜色不够：一个 50% 混合的像素与一个纯深色像素，颜色可能一模一样，
    # 能区分它们的只有"离背景边界有多远"。用距离变换而非反复腐蚀——后者在形状
    # 碎裂时（前景核心被切成多块）会整块消失，半径稍大就直接解不出 alpha。
    # 半径不写死：从大到小试，取第一个仍有足够核心像素的——过渡带较宽的图自动用大半径。
    cand = fg & covered_b & (d_b > tol)
    dist = cv2.distanceTransform(cand.astype(np.uint8), cv2.DIST_L2, 5)
    f_seed = None
    seed_dist_used = None
    for R in (40, 34, 28, 24, 20, 16, 12, 8, 4):
        if R > seed_max_dist:
            continue
        s = cand & (dist > R)
        if int(s.sum()) >= MIN_SEED_PIXELS:
            f_seed, seed_dist_used = s, R
            break
    if f_seed is None:
        return None, {
            "usable": False,
            "reason": f"找不到兼具'离轮廓够远'与'颜色显著偏离背景'的前景核心"
                      f"（tol={tol:.1f}，候选半径已试到 4px）：该图前景与背景对比不足，"
                      f"matting 不适用，请人工提供 mask",
            "tol": round(tol, 2), "candidate_pixels": int(cand.sum()),
        }

    f_loc, f_ok = _propagate_nearest(rgb, f_seed, rounds, cv2, np)
    d_f = np.linalg.norm(c - f_loc, axis=2)
    f_b_dist = np.linalg.norm(f_loc - b_loc, axis=2)

    # --- 4) trimap + 闭式解 alpha ---
    covered = covered_b & (f_ok > 0)
    m_fg = covered & (d_f <= tol)
    m_bg = covered & (d_b <= tol)
    both = m_fg & m_bg
    prefer_bg = both & ((d_b < d_f) | ((d_b == d_f) & (f_b_dist >= t_amb)))
    certain_fg = (m_fg & ~m_bg) | (both & ~prefer_bg)
    certain_bg = (m_bg & ~m_fg) | prefer_bg
    rest = covered & ~certain_fg & ~certain_bg
    degenerate = rest & (f_b_dist < t_amb)
    uncertain = rest & ~degenerate

    alpha = np.zeros((h, w), dtype=np.float32)
    d = f_loc - b_loc
    dd = (d * d).sum(axis=2)
    valid = uncertain & (dd > eps)
    num = ((c - b_loc) * d).sum(axis=2)
    alpha[valid] = np.clip(num[valid] / dd[valid], 0.0, 1.0)
    alpha[degenerate] = 0.5
    alpha[certain_fg] = 1.0
    alpha[certain_bg] = 0.0
    uncovered = ~covered
    alpha[uncovered] = fg[uncovered].astype(np.float32)

    if smooth >= 3:
        alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=smooth / 3.0)
        alpha[certain_fg | (uncovered & fg)] = 1.0
        alpha[certain_bg | (uncovered & ~fg)] = 0.0

    a8 = (alpha * 255.0).round().astype(np.uint8)
    return a8, {
        "usable": True,
        "method": "local_fb_matting_adaptive_trimap",
        "seed_dist_used": seed_dist_used,
        "seed_max_dist": seed_max_dist,
        "propagate_rounds": rounds,
        "tol_auto": round(tol, 2),
        "tol_ambiguous": t_amb,
        "candidate_pixels": int(cand.sum()),
        "seed_pixels": int(f_seed.sum()),
        "certain_fg_pixels": int(certain_fg.sum()),
        "certain_bg_pixels": int(certain_bg.sum()),
        "uncertain_pixels": int(uncertain.sum()),
        "solved_pixels": int(valid.sum()),
        "degenerate_pixels": int(degenerate.sum()),
        "degenerate_note": "前景与背景颜色太接近，方程病态，取 0.5 中性值；该处轮廓需人工复核",
        "alpha_mean_on_uncertain": round(float(alpha[uncertain].mean()), 4) if uncertain.any() else None,
    }


def estimate_mask(image_bytes: bytes, method: str = "bgmodel", *,
                  hard_protect: Optional[Sequence[Sequence[int]]] = None,
                  deg: int = 4, iters: int = 5, thr_scale: float = 2.6,
                  erode: int = 1, feather_sigma: float = 1.2,
                  alpha_source: str = "feather", matting_seed_max_dist: int = 24,
                  matting_tol: Optional[float] = None,
                  component_mode: str = "all") -> Dict[str, Any]:
    """产出 **草稿** mask（二值 + 软 alpha）。

    alpha_source：
      · "feather"（默认，向后兼容）—— 二值 mask 高斯羽化。
      · "matting" —— 局部 F/B matting 反解真实 alpha。换深色/异色背景、或轮廓含细发丝时
        必用；否则 alpha 恒为 1，unmix 去污无从生效。

    component_mode：
      · "all"（默认，历史行为）—— 保留所有面积达标的前景块。适用于背景干净、能一次分对的素材。
      · "seed" —— 只保留**含硬保护区的那一个连通块**。用于背景带绘画性纹理/多平面
        （墙+地）的素材：那里残差会冒出成片背景斑块，被 "all" 全数收进 mask，
        导致剪影"贴边"、无法缩小置入场景（CS-006 实测）。属**有损**操作，
        丢弃清单入档且强制计入 problems（无自动通过路径）。

    返回值中的 `usable` 只是"没触发已知失效条件"，不代表质量合格；
    必须经 `confirm_mask()` 人工目视确认后才能用于定版产出。
    """
    import cv2
    import numpy as np

    if method not in METHODS:
        raise ComposeRejected("MASK_METHOD_UNSUPPORTED", f"不支持的分割方法：{method}")
    # 阶数显式校验：_poly_basis 已支持任意阶，但超出 8 会在全图/分块设计矩阵上失控
    # （9 阶 = 55 项，96×1024 分块块就要 21MB）。宁可报错，也不静默降级。
    if not (2 <= int(deg) <= 8):
        raise ComposeRejected("MASK_DEG_OUT_OF_RANGE",
                              f"背景曲面阶数 deg={deg} 超出允许范围 [2, 8]")
    if component_mode not in COMPONENT_MODES:
        raise ComposeRejected("MASK_COMPONENT_MODE_UNSUPPORTED",
                              f"未知连通块策略：{component_mode}，可选 {list(COMPONENT_MODES)}")
    boxes = [tuple(int(v) for v in b) for b in (hard_protect or DEFAULT_HARD_PROTECT)]
    rgb, bgr = _to_bgr(image_bytes)
    h, w = rgb.shape[:2]
    for (bx0, by0, bx1, by1) in boxes:
        if not (0 <= bx0 < bx1 <= w and 0 <= by0 < by1 <= h):
            raise ComposeRejected("MASK_PROTECT_OUT_OF_RANGE",
                                  f"硬保护区超出画面：{(bx0, by0, bx1, by1)}")

    pre = precheck_subject(image_bytes, boxes)

    params: Dict[str, Any] = {"method": method, "hard_protect": [list(b) for b in boxes]}
    if method == "bgmodel":
        resid, base, n_sel, n_terms = _fit_background(rgb, deg, iters)
        thr = base * thr_scale + 0.02
        raw = (resid > thr).astype(np.uint8) * 255
        params.update({"deg": deg, "iters": iters, "thr_scale": thr_scale,
                       "thr": round(thr, 5), "bg_residual_p99_5": round(base, 5),
                       "selected_bg_pixels": n_sel, "fitted_terms": n_terms})
        # 闭运算核需足够大：衣物被误判出的"凹陷"通过窄口与外部背景连通，
        # 小核封不住口，填充逻辑就够不到（CS-002 的西装破洞即此因）。
        kernel_close, kernel_open = 21, 11
    elif method == "grabcut":
        gc = np.full((h, w), cv2.GC_PR_FGD, dtype=np.uint8)
        b = 10
        gc[:b, :] = cv2.GC_BGD
        gc[h - b:, :] = cv2.GC_BGD
        gc[:, :b] = cv2.GC_BGD
        gc[:, w - b:] = cv2.GC_BGD
        for (bx0, by0, bx1, by1) in boxes:
            gc[by0:by1, bx0:bx1] = cv2.GC_FGD
        m1 = np.zeros((1, 65), dtype=np.float64)
        m2 = np.zeros((1, 65), dtype=np.float64)
        cv2.grabCut(bgr, gc, None, m1, m2, iters, cv2.GC_INIT_WITH_MASK)
        raw = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
        params.update({"iters": iters, "border": b})
        kernel_close, kernel_open = 9, 5
    else:  # colorkey
        px = bgr.astype(np.float32)
        corner = 110
        cs = []
        for (x0, y0) in [(0, 0), (w - corner, 0), (0, h - corner), (w - corner, h - corner)]:
            cs.append(px[y0:y0 + corner, x0:x0 + corner].reshape(-1, 3))
        bg = np.vstack(cs)
        step = max(1, bg.shape[0] // 300)
        bg = bg[::step][:300]
        dist = np.empty((h, w), dtype=np.float32)
        band = max(1, int(2.4e7 / (w * bg.shape[0])))
        for y0 in range(0, h, band):
            y1 = min(h, y0 + band)
            seg = px[y0:y1]
            dist[y0:y1] = np.linalg.norm(seg[:, :, None, :] - bg[None, None, :, :],
                                         axis=3).min(axis=2)
        m = 6
        rim = np.concatenate([
            dist[y0 + m:y0 + corner - m, x0 + m:x0 + corner - m].ravel()
            for (x0, y0) in [(0, 0), (w - corner, 0), (0, h - corner), (w - corner, h - corner)]])
        bg_p99 = float(np.percentile(rim, 99.5))
        thr = bg_p99 * 1.5 + 3.0
        raw = (dist > thr).astype(np.uint8) * 255
        params.update({"bg_rim_p99_5": round(bg_p99, 3), "thr": round(thr, 3)})
        kernel_close, kernel_open = 9, 5

    # 形态学 → 去孤立块 → 填洞（不可把人物挖空）→ 边缘腐蚀（宁可少保留）→ 硬保护区复位
    if alpha_source not in ("feather", "matting"):
        raise ComposeRejected("ALPHA_SOURCE_UNSUPPORTED", f"未知 alpha 来源：{alpha_source}")
    kc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_close, kernel_close))
    ko = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_open, kernel_open))
    fg = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, kc)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, ko)
    fg = _keep_significant_components(fg, cv2)
    fg = _fill_holes(fg, cv2)

    # seed 模式：只留含硬保护区的那一个连通块。
    #
    # 为什么需要它（CS-006）：候选主图（AI 重绘的全身照）背景是**绘画性纹理**的无缝纸，
    # 还叠了"墙面→地面"两个平面与人物投影。单一多项式曲面解释不了，残差图上于是冒出
    # 成片背景斑块，被 `_keep_significant_components`（面积≥0.3% 就留）全数收进 mask。
    # 实测 4 阶 × thr_scale=8 时全 mask 四边贴边 194px、74 个块；但**含人脸种子的那一块**
    # 是人形（长宽比 0.284）且四边贴边为 0 —— 问题不在人物被裁断，而在多留了背景块。
    #
    # 为什么必须"按种子选"而不是"取最大块"：最大块未必是人物（背景斑块可能更大）。
    # 硬保护区是人工核验过、确定位于人物内部的区域，用它当种子是可审计的确定性规则。
    #
    # 为什么不静默做：它会丢弃与躯干断开的前景块（可能含衣物碎片，CS-002 教训）。
    # 故把丢弃清单与面积如实记入 params，并把"须人工目视确认"计入 problems ——
    # 后果是 seed 模式产出的 mask 永远 usable=False、无自动通过路径。
    dropped: List[Dict[str, int]] = []
    kept_component_area = 0
    if component_mode == "seed":
        n_lab, lab, lstats, _lc = cv2.connectedComponentsWithStats(fg)
        keep = np.zeros(max(n_lab, 1), dtype=bool)
        for (bx0, by0, bx1, by1) in boxes:
            sub = lab[by0:by1, bx0:bx1]
            vals, cnts = np.unique(sub[sub > 0], return_counts=True)
            if vals.size:
                keep[int(vals[int(np.argmax(cnts))])] = True
        if not keep.any():
            raise ComposeRejected("MASK_SEED_COMPONENT_MISSING",
                                  "硬保护区内没有前景连通块，无法按种子选取")
        fg = np.where(keep[lab], 255, 0).astype(np.uint8)
        kept_component_area = int(((keep[lab]) & (fg > 0)).sum())
        for j in range(1, n_lab):
            if not keep[j]:
                dropped.append({"label": j, "area": int(lstats[j, cv2.CC_STAT_AREA]),
                                "bbox": [int(lstats[j, cv2.CC_STAT_LEFT]),
                                         int(lstats[j, cv2.CC_STAT_TOP]),
                                         int(lstats[j, cv2.CC_STAT_WIDTH]),
                                         int(lstats[j, cv2.CC_STAT_HEIGHT])]})
        dropped.sort(key=lambda r: -r["area"])

    # matting 模式不做腐蚀：边缘由反解的真实 alpha 决定，"宁可少保留"的腐蚀会把
    # 半透明发丝整段削掉，反而丢掉 matting 本来能正确表达的细节。
    if erode > 0 and alpha_source == "feather":
        ke = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (erode * 2 + 1, erode * 2 + 1))
        fg = cv2.erode(fg, ke)
    for (bx0, by0, bx1, by1) in boxes:
        fg[by0:by1, bx0:bx1] = 255

    matting_info: Optional[Dict[str, Any]] = None
    if alpha_source == "matting":
        a8, matting_info = _alpha_by_matting(rgb, fg, cv2, np,
                                             seed_max_dist=matting_seed_max_dist,
                                             t_bg=matting_tol)
        if a8 is None:
            alpha = cv2.GaussianBlur(fg, (0, 0), sigmaX=feather_sigma)
            matting_info = {**(matting_info or {}), "fallback": "feather"}
        else:
            alpha = a8
    else:
        alpha = cv2.GaussianBlur(fg, (0, 0), sigmaX=feather_sigma)
    for (bx0, by0, bx1, by1) in boxes:
        alpha[by0:by1, bx0:bx1] = 255

    fg_bool = fg > MASK_BINARY_MAX
    fg_ratio = float(fg_bool.sum()) / (w * h)
    coverage = [round(float(fg_bool[by0:by1, bx0:bx1].mean()), 4) for (bx0, by0, bx1, by1) in boxes]
    n_comp = _count_components(fg, cv2)
    edge = cv2.morphologyEx(fg, cv2.MORPH_GRADIENT, np.ones((5, 5), np.uint8))
    edge_ratio = float((edge > 0).sum()) / max(1, int(fg_bool.sum()))

    problems: List[str] = list(pre["reasons"])
    if min(coverage) < 1.0:
        problems.append("硬保护区未被完整包含：自动分割切到了身份特征区，必须人工修 mask")
    if fg_ratio < 0.05 or fg_ratio > 0.95:
        problems.append(f"前景占比异常（{fg_ratio:.3f}）：mask 可能整体判反或未生效")
    if component_mode == "all" and n_comp != 1:
        problems.append(f"前景被切成 {n_comp} 块，需人工合并")
    if component_mode == "seed":
        problems.append(
            f"seed 连通块策略：已丢弃 {len(dropped)} 个非种子前景块"
            f"（合计 {sum(d['area'] for d in dropped)} 像素）——"
            f"若有与躯干断开的衣物碎片被丢弃，属真实丢失，必须人工目视确认")

    mask_bin = _encode_png(fg)
    mask_alpha = _encode_png(alpha)
    params.update({"erode": erode, "feather_sigma": feather_sigma,
                   "alpha_source": alpha_source,
                   "matting_seed_max_dist": matting_seed_max_dist, "matting_tol": matting_tol,
                   "close_kernel": kernel_close, "open_kernel": kernel_open,
                   "component_mode": component_mode})
    if component_mode == "seed":
        # 丢弃清单完整入档：mask 是有损的，产物必须能追溯到"丢了什么、丢了多少"。
        params["dropped_components"] = dropped
        params["dropped_component_count"] = len(dropped)
        params["dropped_area"] = int(sum(d["area"] for d in dropped))
        params["kept_component_area"] = int(kept_component_area)
    # 四边贴边像素：由人物是否被画框裁断决定。它直接决定"能否缩小置入场景"——
    # 剪影贴边时外扩画框会露出源图裁断的断面（CS-005）。
    fg_touch = {"left": int(fg_bool[:, 0].sum()), "right": int(fg_bool[:, -1].sum()),
                "top": int(fg_bool[0, :].sum()), "bottom": int(fg_bool[-1, :].sum())}
    params["border_touch_pixels"] = fg_touch
    params["border_touching_edges"] = [k for k, v in fg_touch.items() if v > 0]
    if matting_info:
        params["matting"] = matting_info
    if alpha_source == "matting" and matting_info and matting_info.get("usable"):
        alphaf = alpha.astype(np.float32) / 255.0
        soft = int(((alphaf > 0.02) & (alphaf < 0.98)).sum())
        metrics_extra = {"soft_edge_pixels": soft,
                         "soft_edge_ratio": round(soft / max(1, int(fg_bool.sum())), 4)}
    else:
        metrics_extra = {}
    return {
        "method": method,
        "params": params,
        "pixel_size": [w, h],
        "precheck": pre,
        "metrics": {"foreground_ratio": round(fg_ratio, 4),
                    "protect_coverage": coverage,
                    "connected_components": n_comp,
                    "edge_pixel_ratio": round(edge_ratio, 4),
                    **metrics_extra},
        "mask_binary_png": mask_bin,
        "mask_alpha_png": mask_alpha,
        "mask_binary_sha256": _sha(mask_bin),
        "mask_alpha_sha256": _sha(mask_alpha),
        "usable": not problems,
        "problems": problems,
        "state": "draft",
        "note": "草稿：必须经人工目视确认（confirm_mask）后才能用于定版产出",
    }


def _keep_significant_components(fg, cv2, min_ratio: float = 0.003):
    """保留所有足够大的前景块，而不是只留"最大的一块"。

    教训（CS-002）：只留最大块时，若衣物与躯干的细连接被形态学断开，
    整块衣摆会被当作噪点丢弃 —— 合成后衣服上出现"破洞"，深色背景下极其明显。
    宁可多留几块，也不能把人物挖空。
    """
    import numpy as np

    area = fg.shape[0] * fg.shape[1]
    n, labels, stats, _ = cv2.connectedComponentsWithStats(fg)
    keep = np.zeros_like(fg)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= area * min_ratio:
            keep[labels == i] = 255
    if not keep.any():
        raise ComposeRejected("MASK_EMPTY", "分割结果为空：无任何可用前景，方法不适用该图")
    return keep


def _fill_holes(fg, cv2):
    import numpy as np

    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(fg)
    cv2.drawContours(filled, contours, -1, 255, thickness=cv2.FILLED)
    return filled


def _count_components(fg, cv2) -> int:
    area = fg.shape[0] * fg.shape[1]
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(fg)
    return sum(1 for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= area * 0.0005)


def _encode_png(arr) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- mask 登记与确认


def register_mask(identity_id: str, source_asset_id: str, mask_binary_png: bytes,
                  mask_alpha_png: bytes, params: Dict[str, Any],
                  origin: str = "algorithm_draft", storage_uri: str = "",
                  source_evidence: str = "", asset_id: Optional[str] = None) -> Dict[str, Any]:
    """登记 mask 资产。

    mask 与**源图哈希 + 尺寸**绑定：换了人物原图或尺寸不一致，该 mask 立即失效。
    origin：algorithm_draft（自动草稿）/ manual（人工提供）/ assisted（草稿+人工修正）。

    `asset_id` 与 `register_asset` 同义：调用方已经按该 id 落了盘，
    再登记时不能另生成一个 id —— 否则"文件名就是资产 id"这条追溯链接会断。
    """
    src = registry.get_asset(source_asset_id)
    if not src:
        raise ComposeRejected("MASK_SOURCE_NOT_FOUND", f"源资产不存在：{source_asset_id}")
    if origin not in ("algorithm_draft", "manual", "assisted"):
        raise ComposeRejected("MASK_ORIGIN_INVALID", f"未知来源类型：{origin}")

    import numpy as np
    from PIL import Image

    bimg = Image.open(io.BytesIO(mask_binary_png))
    aimg = Image.open(io.BytesIO(mask_alpha_png))
    if bimg.size != aimg.size:
        raise ComposeRejected("MASK_SIZE_MISMATCH", "二值 mask 与软 alpha 尺寸不一致")
    if src.get("width") and src.get("height") and bimg.size != (src["width"], src["height"]):
        raise ComposeRejected("MASK_SIZE_MISMATCH",
                              f"mask 尺寸 {bimg.size} 与源资产 {(src['width'], src['height'])} 不一致")
    if not bool((np.array(aimg) > 0).any()):
        raise ComposeRejected("MASK_EMPTY", "mask 全空：不含任何前景")

    if not storage_uri:
        raise ComposeRejected("MASK_NO_FILE", "mask 未落盘：需提供存储位置")
    st_id = st.new_id("mask")
    asset = registry.register_asset(
        identity_id=identity_id, kind="mask", storage_uri=storage_uri,
        file_hash=_sha(mask_alpha_png),
        source_evidence=source_evidence or f"由 {source_asset_id} 派生；origin={origin}",
        width=bimg.size[0], height=bimg.size[1],
        note=f"mask（{origin}）；二值哈希 {_sha(mask_binary_png)[:19]}…",
        asset_id=asset_id,
    )
    spec = {
        "id": st_id,
        "mask_asset_id": asset["id"],
        "identity_id": identity_id,
        "source_asset_id": source_asset_id,
        "source_file_hash": src.get("file_hash", ""),
        "source_version": int(src.get("version", 0)),
        "pixel_size": [bimg.size[0], bimg.size[1]],
        "origin": origin,
        "params": params,
        "mask_binary_sha256": _sha(mask_binary_png),
        "mask_alpha_sha256": _sha(mask_alpha_png),
        "state": "draft",
        "confirmed_by": "",
        "confirmed_at": None,
        "confirm_outcome": None,
        "confirm_note": "",
        "created_at": now_iso(),
    }
    st.put("mask_specs", st_id, spec, {"identity_id": identity_id})
    return spec


def get_mask_spec(mask_asset_id: str) -> Optional[Dict[str, Any]]:
    spec = st.get("mask_specs", mask_asset_id)
    if spec:
        return spec
    for row in st.all_rows("mask_specs"):
        if row.get("mask_asset_id") == mask_asset_id:
            return row
    return None


def confirm_mask(mask_asset_id: str, reviewer: str, outcome: str, note: str = ""):
    """人工目视确认。这是自动 mask 定版的**唯一**入口，且不接受 agent 身份。

    不提供任何"自动通过"路径：指标只能排除已知失效，不能证明 mask 正确。
    """
    spec = get_mask_spec(mask_asset_id)
    if not spec:
        raise ComposeRejected("MASK_NOT_FOUND", f"mask 未登记：{mask_asset_id}")
    if outcome not in ("pass", "fail", "review"):
        raise ComposeRejected("MASK_OUTCOME_INVALID", f"未知确认结论：{outcome}")
    if outcome in ("pass", "fail") and not str(reviewer).strip():
        raise ComposeRejected("MASK_REVIEWER_REQUIRED", "人工确认必须记录确认人")
    spec["confirm_outcome"] = outcome
    spec["confirm_note"] = note
    spec["state"] = {"pass": "confirmed", "fail": "rejected", "review": "draft"}[outcome]
    if outcome in ("pass", "fail"):
        spec["confirmed_by"] = reviewer
        spec["confirmed_at"] = now_iso()
    st.put("mask_specs", spec["id"], spec, {"identity_id": spec.get("identity_id")})
    return spec


def mask_is_usable(mask_asset_id: str, source_asset_id: str) -> Tuple[bool, List[str]]:
    """mask 能否用于**定版**产出：需已确认通过，且绑定关系仍然成立。"""
    reasons: List[str] = []
    spec = get_mask_spec(mask_asset_id)
    if not spec:
        return False, [f"mask 未登记：{mask_asset_id}"]
    src = registry.get_asset(source_asset_id)
    if not src:
        return False, [f"源资产不存在：{source_asset_id}"]
    if spec.get("state") != "confirmed":
        reasons.append(f"mask 未经人工目视确认通过（当前 {spec.get('state')}）：只能产出候选")
    if spec.get("source_asset_id") != source_asset_id:
        reasons.append(f"mask 派生自 {spec.get('source_asset_id')}，与本次源资产 {source_asset_id} 不符")
    if spec.get("source_file_hash") != src.get("file_hash"):
        reasons.append("源文件已被替换，该 mask 失效，需重新生成")
    if spec.get("source_version") != int(src.get("version", 0)):
        reasons.append("源资产版本已变更，该 mask 失效")
    if spec.get("pixel_size") != [src.get("width"), src.get("height")]:
        reasons.append("mask 尺寸与源资产不符")
    return (not reasons), reasons


# ---------------------------------------------------------------- 边缘去污（unmix）

def _poly_basis(w: int, h: int, deg: int, idx=None):
    """低频多项式基（与 _fit_background 同构，用于背景场的拟合与求值）。

    idx 给定时只在**抽样像素**上构建（避免全图设计矩阵撑爆内存）。

    缺陷修正（CS-006 扫描暴露）：原实现只硬编码到 4 次项，`deg` 传 5/6/8
    会被**静默当成 4** —— 调用方以为自己提高了拟合阶数，实际什么都没变
    （实测 deg=4 与 deg=6 的 mask 逐位相同）。这属于"参数看似生效实则无效"，
    比直接报错危险得多。现改为按总次数生成全部单项式：
    (X^i)(Y^j), i+j ≤ deg。枚举顺序刻意与旧实现一致，故 deg ≤ 4 时基矩阵
    逐项同序，历史行为与既有产物完全不变；deg ≥ 5 才真正新增高次项。
    """
    import numpy as np

    deg = int(deg)
    if deg < 0:
        raise ValueError(f"多项式阶数不能为负：{deg}")
    if idx is not None:
        ii = np.asarray(idx, dtype=np.int64)
        X = (ii % max(1, w)).astype(np.float32) / max(1, w)
        Y = (ii // max(1, w)).astype(np.float32) / max(1, h)
    else:
        yy, xx = np.mgrid[0:h, 0:w]
        X = (xx / max(1, w)).astype(np.float32).ravel()
        Y = (yy / max(1, h)).astype(np.float32).ravel()

    def _p(base, p):
        return np.ones_like(base) if p == 0 else base ** p

    terms = []
    for total in range(0, deg + 1):
        for i in range(total, -1, -1):        # X^total, X^(total-1)Y, ..., Y^total
            terms.append(_p(X, i) * _p(Y, total - i))
    return np.stack(terms, axis=1)


def _residual_map_blocks(rgb, coef, deg: int, block_rows: int = 96):
    """按行分块计算背景残差图，避免一次性构建全图设计矩阵。

    1024×1536、15 项的全图基矩阵约 94MB；分块后峰值只与块高有关（96×1024×15 ≈ 5.6MB）。
    """
    import numpy as np

    h, w = rgb.shape[:2]
    px = rgb.astype(np.float32) / 255.0
    out = np.empty((h, w), dtype=np.float32)
    for y0 in range(0, h, block_rows):
        y1 = min(h, y0 + block_rows)
        rows = np.repeat(np.arange(y0, y1, dtype=np.int64), w)
        cols = np.tile(np.arange(w, dtype=np.int64), y1 - y0)
        A = _poly_basis(w, h, deg, idx=rows * w + cols)
        blk = px[y0:y1].reshape(-1, 3)
        out[y0:y1] = np.linalg.norm(blk - A @ coef, axis=1).reshape(y1 - y0, w)
    return out


def estimate_source_background(image_bytes: bytes, alpha_png: Optional[bytes] = None,
                               deg: int = 3, iters: int = 4,
                               alpha_bg_max: int = 10) -> Dict[str, Any]:
    """估计**源图**的低频背景色场 B_orig，供 unmix 去污使用。

    判据来源：mask 中 alpha≈0 的像素即原背景。用它们拟合低频曲面（而非取单一常量），
    以兼容影棚渐晕与布光不均。背景像素不足时回落到四角中位数常量。
    返回的采样数与残差是审计证据：B_orig 从何而来、拟合得多好。
    """
    import numpy as np

    rgb, _bgr = _to_bgr(image_bytes)
    h, w = rgb.shape[:2]
    px = rgb.astype(np.float32) / 255.0
    flat = px.reshape(-1, 3)
    A = _poly_basis(w, h, deg)

    sel = None
    if alpha_png is not None:
        a = np.array(_load(alpha_png).convert("L"))
        if a.shape != (h, w):
            raise ComposeRejected("ALPHA_SIZE_MISMATCH",
                                  f"alpha 尺寸 {a.shape[::-1]} 与源图 {(w, h)} 不一致")
        sel = (a.reshape(-1) <= alpha_bg_max)

    corner = 110
    corners = []
    for (cx, cy) in [(0, 0), (w - corner, 0), (0, h - corner), (w - corner, h - corner)]:
        corners.append(px[cy:cy + corner, cx:cx + corner].reshape(-1, 3))
    corner_median = np.median(np.vstack(corners), axis=0)

    fallback = sel is None or int(sel.sum()) < 2000
    if fallback:
        coef = np.zeros((A.shape[1], 3), dtype=np.float32)
        coef[0] = corner_median
        return {
            "coef": coef, "deg": deg, "basis_terms": int(A.shape[1]),
            "method": "corner_median_constant", "samples": 0,
            "fallback": True, "corner_median_rgb255": [round(float(v) * 255, 1) for v in corner_median],
            "note": "可用背景像素不足 2000，回落到四角中位数常量",
        }

    coef = None
    resid_p99 = None
    for _ in range(max(1, iters)):
        if int(sel.sum()) < 1000:
            break
        coef, *_ = np.linalg.lstsq(A[sel], flat[sel], rcond=None)
        resid = np.linalg.norm(flat - A @ coef, axis=1)
        thr = float(np.percentile(resid[sel], 95))
        new_sel = resid <= thr
        if int(new_sel.sum()) < 1000:
            break
        sel = new_sel
    if coef is None:
        raise ComposeRejected("BG_ESTIMATE_FAILED", "源图背景场估计失败：可用背景像素不足")

    resid = np.linalg.norm(flat - A @ coef, axis=1)
    resid_p99 = float(np.percentile(resid[sel], 99))
    return {
        "coef": coef, "deg": deg, "basis_terms": int(A.shape[1]),
        "method": f"poly_deg{deg}_on_alpha0_pixels", "samples": int(sel.sum()),
        "fallback": False,
        "bg_field_residual_p99": round(resid_p99, 5),
        "corner_median_rgb255": [round(float(v) * 255, 1) for v in corner_median],
        "note": "B_orig 由 mask 中 alpha≈0 的像素拟合得到，用于 unmix 代数去污",
    }


def _eval_bg_field(coef, w: int, h: int, deg: int):
    import numpy as np

    A = _poly_basis(w, h, deg)
    return np.clip((A @ coef).reshape(h, w, 3), 0.0, 1.0) * 255.0


def edge_residual(composed_rgb, bg_rgb, alpha_arr, lo: int = 2, hi: int = 150) -> Dict[str, Any]:
    """量化轮廓过渡带里"没被换干净的原背景"（光晕）。

    取 alpha 处于 (lo, hi) 的半透明带——换背景后理应主要由新背景构成——
    度量其与**新背景**的像素差。数值越低说明边缘越干净。仅作 A/B 比较用，
    不做绝对合格判定（该带内本就含有真实发丝，不可能等于背景）。
    """
    import numpy as np

    band = (alpha_arr > lo) & (alpha_arr < hi)
    n = int(band.sum())
    if n == 0:
        return {"band_pixels": 0, "mean_abs_diff": None, "p95_abs_diff": None}
    d = np.abs(composed_rgb[band].astype(np.float32) - bg_rgb[band].astype(np.float32))
    per_px = d.max(axis=1)
    return {
        "band_pixels": n,
        "mean_abs_diff": round(float(d.mean()), 2),
        "p95_abs_diff": round(float(np.percentile(per_px, 95)), 2),
        "note": "过渡带像素与新背景的最大通道差；越低表示原背景残留越少",
    }


# ---------------------------------------------------------------- 画框式缩放
#
# 关键认识：源图是一张**有边界的照片**。人物被源图边界裁断的地方（例如躯干到了
# 画面左右边缘），源图里并不存在"边界之外"的像素。把画框往外扩，就会把这些
# 断面暴露出来 —— 这不是算法缺陷，是素材本身的限制。故本模块的做法是：
#   1. 人物层永远 1:1 复制（不重采样、不生成）→ 硬保护区断言强度不下降；
#   2. 逐条边界量化"断面会不会露出来、露出多长"，默认硬拒绝；
#   3. 允许调用方显式覆盖（对照实验用），但把量化结果如实写进产物记录。


def _derive_frame(scale: float, rx: float, ry: float, w: int, h: int):
    """由人物缩放比与锚点推导画框：返回 (画框宽, 画框高, 源图左上角在画框中的偏移)。

    锚点取归一化坐标（rx, ry）∈[0,1]²，含义是"源图上相对位置 (rx, ry) 的那个点，
    在画框中仍处于相对位置 (rx, ry)" —— 等价于缩放以该点为不动点。
    """
    cw = max(1, int(round(w / scale)))
    ch = max(1, int(round(h / scale)))
    ox = int(round(rx * (cw - w)))
    oy = int(round(ry * (ch - h)))
    return cw, ch, ox, oy


def _visible_region(ox: int, oy: int, cw: int, ch: int, w: int, h: int):
    """源图在画框内的可见区域：返回 (sx0, sy0, sx1, sy1, cx0, cy0)，不可见则 None。"""
    sx0, sx1 = max(0, -ox), min(w, cw - ox)
    sy0, sy1 = max(0, -oy), min(h, ch - oy)
    if sx1 <= sx0 or sy1 <= sy0:
        return None
    return sx0, sy0, sx1, sy1, sx0 + ox, sy0 + oy


def detect_frame_reveal(alpha_bin, ox: int, oy: int, cw: int, ch: int) -> List[Dict[str, Any]]:
    """逐条源图边界检测"断面暴露"。

    对源图的每条边界（左 x=0 / 右 x=w-1 / 上 y=0 / 下 y=h-1）：
      · 该边界在画框中落在哪一列/行；
      · 边界到画框边缘之间有多少像素是**场景**（即暴露出来的宽度）；
      · 人物剪影在该边界上贴着多长一段（暴露的切口长度）。
    只有"边界在画框内部 + 该处剪影贴着边界"才会形成肉眼可见的断面。
    """
    import numpy as np

    h, w = alpha_bin.shape
    res: List[Dict[str, Any]] = []

    def _ys_at(col):
        ys = np.where(alpha_bin[:, col])[0]
        if ys.size:
            ys = ys[(oy + ys >= 0) & (oy + ys < ch)]
        return ys

    def _xs_at(row):
        xs = np.where(alpha_bin[row, :])[0]
        if xs.size:
            xs = xs[(ox + xs >= 0) & (ox + xs < cw)]
        return xs

    # 左边界：源图 x=0 映射到画布 x=ox；其左侧 ox 列是场景
    if ox > 0:
        ys = _ys_at(0)
        if ys.size:
            res.append({"edge": "left", "canvas_coord": ox, "exposed_strip_px": int(min(ox, cw)),
                        "contact_pixels": int(ys.size),
                        "span_px": int(ys.max() - ys.min() + 1),
                        "canvas_span": [int(oy + ys.min()), int(oy + ys.max())]})
    # 右边界：源图 x=w-1 映射到画布 x=ox+w-1；其右侧是场景
    if ox + w - 1 < cw - 1:
        ys = _ys_at(w - 1)
        if ys.size:
            res.append({"edge": "right", "canvas_coord": int(ox + w - 1),
                        "exposed_strip_px": int(min(cw - 1 - (ox + w - 1), cw)),
                        "contact_pixels": int(ys.size),
                        "span_px": int(ys.max() - ys.min() + 1),
                        "canvas_span": [int(oy + ys.min()), int(oy + ys.max())]})
    # 上边界：源图 y=0 映射到画布 y=oy
    if oy > 0:
        xs = _xs_at(0)
        if xs.size:
            res.append({"edge": "top", "canvas_coord": oy, "exposed_strip_px": int(min(oy, ch)),
                        "contact_pixels": int(xs.size),
                        "span_px": int(xs.max() - xs.min() + 1),
                        "canvas_span": [int(ox + xs.min()), int(ox + xs.max())]})
    # 下边界：源图 y=h-1 映射到画布 y=oy+h-1
    if oy + h - 1 < ch - 1:
        xs = _xs_at(h - 1)
        if xs.size:
            res.append({"edge": "bottom", "canvas_coord": int(oy + h - 1),
                        "exposed_strip_px": int(min(ch - 1 - (oy + h - 1), ch)),
                        "contact_pixels": int(xs.size),
                        "span_px": int(xs.max() - xs.min() + 1),
                        "canvas_span": [int(ox + xs.min()), int(ox + xs.max())]})
    return res


def frame_gates(alpha_bin, hard: Sequence[Sequence[int]], ox: int, oy: int, cw: int, ch: int,
                head_margin: int = MIN_HEAD_MARGIN) -> Dict[str, Any]:
    """画框门禁：返回问题代码与量化指标（不抛异常，供 compose 与范围计算共用）。"""
    import numpy as np

    h, w = alpha_bin.shape
    rows = np.where(alpha_bin.any(axis=1))[0]
    person_top = int(rows.min()) if rows.size else 0
    head_y = int(oy + person_top)
    codes: List[str] = []

    for (bx0, by0, bx1, by1) in hard:
        if not (0 <= ox + bx0 and ox + bx1 <= cw and 0 <= oy + by0 and oy + by1 <= ch):
            codes.append("FRAME_CUTS_IDENTITY_REGION")
            break
    # 头顶门禁只针对"我们自己裁进上边"的情形：oy>=0 时源图自身留白原样保留，
    # 不属于本次构图造成的损失（源图头顶余量小是素材本身的事，另有分色预检负责）。
    if head_y < 0 or (oy < 0 and head_y < head_margin):
        codes.append("FRAME_CUTS_HEAD")
    reveal = detect_frame_reveal(alpha_bin, ox, oy, cw, ch)
    visible = [r for r in reveal if r["exposed_strip_px"] >= MIN_REVEAL_STRIP_PX]
    if visible:
        codes.append("FRAME_REVEALS_SOURCE_CUT")

    return {
        "codes": sorted(set(codes)),
        "person_top_px": person_top,
        "head_margin_px": head_y,
        "head_margin_required": int(head_margin),
        "reveal_all": reveal,
        "reveal_visible": visible,
        "revealed_cut_span_px": max([r["span_px"] for r in visible], default=0),
    }


def _frame_problem_message(code: str, gates: Dict[str, Any], cw: int, ch: int, ox: int, oy: int) -> str:
    if code == "FRAME_CUTS_IDENTITY_REGION":
        return "画框切到了身份特征区（硬保护区）：合成不得裁掉人脸核心"
    if code == "FRAME_CUTS_HEAD":
        return (f"画框切到头顶：头顶到画框上沿仅 {gates['head_margin_px']}px，"
                f"需 ≥{gates['head_margin_required']}px。放大人物的上限由源图头顶余量决定，"
                f"再大就得裁头")
    left = gates["reveal_visible"][0] if gates["reveal_visible"] else None
    if code == "FRAME_REVEALS_SOURCE_CUT":
        detail = "；".join(
            f"{r['edge']} 边暴露 {r['exposed_strip_px']}px 宽的断面、切口长 {r['span_px']}px"
            for r in gates["reveal_visible"])
        return (f"画框外扩会露出源图裁断的断面（{detail}）：源图是特写，"
                f"人物四周没有真实像素可补。要更小的画中人物，请换更宽构图的主图，"
                f"或显式 allow_frame_reveal=True 先看对照效果（不作为生产路径）")
    return f"画框非法（画布 {cw}x{ch}，偏移 {ox},{oy}）：{code}"


def allowed_scale_range(alpha_bin, hard: Sequence[Sequence[int]],
                        anchor: Optional[Sequence[float]] = None,
                        head_margin: int = MIN_HEAD_MARGIN, step: float = 0.005) -> Dict[str, Any]:
    """算出这个素材+锚点下**可用的人物缩放区间**（含 1.0 的那段连续区间）。

    不猜、不拍脑袋：逐个缩放比套用与 compose 完全相同的门禁函数，取连续可行段。
    """
    h, w = alpha_bin.shape
    hx0, hy0, hx1, hy1 = [int(v) for v in hard[0]]
    rx, ry = ((float(anchor[0]), float(anchor[1])) if anchor
              else (round((hx0 + hx1) / 2 / w, 4), round((hy0 + hy1) / 2 / h, 4)))
    feasible: List[float] = []
    s = MIN_PERSON_SCALE
    while s <= MAX_PERSON_SCALE + 1e-9:
        cw, ch, ox, oy = _derive_frame(s, rx, ry, w, h)
        g = frame_gates(alpha_bin, hard, ox, oy, cw, ch, head_margin)
        if not g["codes"]:
            feasible.append(round(s, 4))
        s = round(s + step, 6)
    if not feasible:
        return {"min": None, "max": None, "feasible": [], "anchor": [rx, ry],
                "note": "该素材在给定门禁下没有可行的缩放区间"}
    # 取含 1.0 的连续段；1.0 不可行时退化为全局最小/最大
    idx = min(range(len(feasible)), key=lambda i: abs(feasible[i] - 1.0))
    lo = hi = idx
    while lo - 1 >= 0 and abs(feasible[lo - 1] - feasible[lo]) <= step * 1.5:
        lo -= 1
    while hi + 1 < len(feasible) and abs(feasible[hi + 1] - feasible[hi]) <= step * 1.5:
        hi += 1
    return {
        "min": feasible[lo], "max": feasible[hi],
        "anchor": [rx, ry], "step": step,
        "feasible_count": len(feasible),
        "contains_no_zoom": feasible[lo] <= 1.0 <= feasible[hi],
        "note": "可行区间由 source 几何（头顶余量、硬保护区、源图边界断面）自动求得",
    }


def fit_smaller_frame(mask_alpha_png: bytes, hard: Sequence[Sequence[int]],
                      canvas_h: int, canvas_w: Optional[int] = None,
                      head_margin: int = MIN_HEAD_MARGIN) -> Dict[str, Any]:
    """求一个**零断面**的"缩小画中人物"构图（返回的画框可直接传给 compose(frame=...)）。

    为什么不是简单外扩画框：源图人物躯干在左/右/下三边被源图裁断，外扩必露断面。
    但源图的**上边界没有贴着人物**（头顶之上是留白）——所以把画框整体下移、让人物在
    画框下沿被裁断，断面就正好落在画框边缘而不可见，上半部分交给场景。
    人物在画面中的占比随之下降，而人物像素仍是 1:1 逐像素复制（断言强度不下降）。

    代价（如实记录，不藏）：人物会被挤到画面下半部，人脸位置比常规口播低；
    想让人物小且居中，正解是换更宽构图的主图，而不是把特写往外放。
    """
    import numpy as np

    ab = np.array(_load(mask_alpha_png).convert("L")) > MASK_BINARY_MAX
    h, w = ab.shape
    rows = np.where(ab.any(axis=1))[0]
    if not rows.size:
        raise ComposeRejected("MASK_EMPTY", "mask 全空：无法求解构图")
    person_top = int(rows.min())

    r_last = h - 1
    for y in range(person_top, h):
        if ab[y, 0] or ab[y, w - 1]:
            r_last = y - 1
            break
    if r_last < person_top:
        raise ComposeRejected(
            "NO_CLEAN_FRAME",
            "该素材的左右边界一开头就贴着人物剪影，不存在零断面的缩小构图")

    cw = int(canvas_w) if canvas_w else int(round(canvas_h * w / h))
    cols = np.where(ab[person_top:r_last + 1].any(axis=0))[0]
    center_src = (int(cols.min()) + int(cols.max())) / 2.0
    ox = int(round(cw / 2.0 - center_src))
    oy = int(canvas_h) - (r_last + 1)
    g = frame_gates(ab, hard, ox, oy, cw, canvas_h, head_margin)
    if g["codes"]:
        raise ComposeRejected(g["codes"][0],
                              _frame_problem_message(g["codes"][0], g, cw, canvas_h, ox, oy),
                              {"codes": g["codes"], "frame": [ox, oy, cw, canvas_h]})
    return {
        "frame": [ox, oy, cw, int(canvas_h)],
        "clean_last_row": r_last,
        "person_top_px": person_top,
        "person_height_ratio": round((r_last + 1 - person_top) / canvas_h, 4),
        "silhouette_center_x": center_src,
        "gates": g,
        "note": "断面正好压在画框下沿（不可见）；人物层仍为 1:1 逐像素复制。"
                "代价：人物位于画面下半部，人脸位置偏低。",
    }


# ---------------------------------------------------------------- 合成


def compose(identity_id: str, hero_asset_id: str, mask_asset_id: str, background_asset_id: str,
            out_uri: str = "", require_confirmed: bool = True,
            edge_mode: str = "alpha", person_scale: float = 1.0,
            anchor: Optional[Sequence[float]] = None,
            frame: Optional[Sequence[int]] = None,
            allow_frame_reveal: bool = False,
            head_margin: int = MIN_HEAD_MARGIN) -> Dict[str, Any]:
    """确定性合成：人物区逐像素取自源图。

    edge_mode：
      · "alpha"（默认，向后兼容）—— 常规 alpha 混合。
      · "unmix" —— 代数去污：把半透明边缘里**原背景的贡献**换成新背景的贡献，
        公式 C' = C + (1-α)·(B_new - B_orig)，α==1 处校正量为 0。
        换深色/异色背景时必用，否则轮廓外会留一圈原影棚的灰晕。

    人物大小：
      · person_scale —— 人物在画面中的缩放比（1.0 = 保持源图构图）。
        实现方式是**只改画框、不改人物像素**：>1 内收画框（裁切），<1 外扩画框（补边）。
        锚点 anchor 为归一化坐标，默认取硬保护区（人脸核心）中心。
      · frame —— 直接指定画框 (ox, oy, 画框宽, 画框高)，用于任意构图（如"裁到肩线"
        再把人物放到画框下缘，从而缩小画中人物而**不露断面**）。与 person_scale 互斥。
      · allow_frame_reveal —— 外扩画框会露出源图裁断的断面时，默认硬拒绝；
        置 True 可强制产出（对照实验用），量化结果仍如实写入产物记录。

    只有 `require_confirmed=True` 且 mask 已确认通过时才返回 `production_ready=True`；
    否则一律标记为候选（candidate），不得进入生产。
    """
    import numpy as np
    from PIL import Image

    if edge_mode not in EDGE_MODES:
        raise ComposeRejected("EDGE_MODE_UNSUPPORTED",
                              f"未知合成模式：{edge_mode}，可选 {list(EDGE_MODES)}")

    hero = registry.get_asset(hero_asset_id)
    bg = registry.get_asset(background_asset_id)
    if not hero:
        raise ComposeRejected("ASSET_NOT_FOUND", f"源人物资产不存在：{hero_asset_id}")
    if not bg:
        raise ComposeRejected("ASSET_NOT_FOUND", f"背景资产不存在：{background_asset_id}")
    if bg.get("kind") != "background":
        raise ComposeRejected("BACKGROUND_KIND_INVALID",
                              f"背景资产 kind={bg.get('kind')}，应为 background")

    # 源图与产物必须可追溯：缺哈希/缺来源的资产不得参与合成
    for a, label in ((hero, "源人物资产"), (bg, "背景资产")):
        if not a.get("file_hash") or not a.get("storage_uri"):
            raise ComposeRejected("ASSET_INCOMPLETE", f"{label} 缺文件或哈希：{a['id']}")
        if a.get("is_mock"):
            raise ComposeRejected("ASSET_IS_MOCK", f"{label} 为 Mock 后代，不得进入生产")

    # 背景权属：合成产物是生产素材，背景必须可商用
    if not bg.get("license_id"):
        raise ComposeRejected("BACKGROUND_LICENSE_MISSING",
                              "背景资产未登记权属记录，无法确认可商用")
    licensing.require(bg["license_id"], subject=f"背景资产 {bg['id']}", need_weights=False)

    spec = get_mask_spec(mask_asset_id)
    if not spec:
        raise ComposeRejected("MASK_NOT_FOUND", f"mask 未登记：{mask_asset_id}")
    mask_path = registry.get_asset(spec["mask_asset_id"])
    if not mask_path or not mask_path.get("storage_uri"):
        raise ComposeRejected("MASK_NO_FILE", "mask 资产缺文件")

    hero_img = Image.open(io.BytesIO(_read(hero["storage_uri"]))).convert("RGBA")
    bg_img = Image.open(io.BytesIO(_read(bg["storage_uri"]))).convert("RGBA")
    alpha_img = Image.open(io.BytesIO(_read(mask_path["storage_uri"]))).convert("L")

    if bg_img.size != hero_img.size:
        bg_img = bg_img.resize(hero_img.size, Image.LANCZOS)
    if alpha_img.size != hero_img.size:
        raise ComposeRejected("MASK_SIZE_MISMATCH",
                              f"mask {alpha_img.size} 与源图 {hero_img.size} 不一致")

    # 逐像素校验：mask 与源图的绑定关系（哈希已登记，这里复核实际文件）
    actual_hero_hash = _sha(_read(hero["storage_uri"]))
    if actual_hero_hash != spec.get("source_file_hash"):
        raise ComposeRejected("MASK_SOURCE_STALE",
                              "源文件哈希与 mask 登记不符，该 mask 不适用于当前源图")

    # 硬保护区 = 安全网：合成时强制保留，不依赖 mask 是否恰好覆盖到。
    # 否则 mask 略有偏差就会改动身份特征区——那是不可接受的失败模式。
    # 但也不静默掩盖 mask 缺陷：未覆盖的事实会记录在 mask_protect_coverage / hard_protect_enforced。
    hard = [tuple(int(v) for v in b)
            for b in (spec.get("params", {}).get("hard_protect") or DEFAULT_HARD_PROTECT)]
    src_w, src_h = hero_img.size
    alpha_raw = np.array(alpha_img)  # 未强制硬保护区的原始 mask，用于估计 B_orig
    alpha_bin = alpha_raw > MASK_BINARY_MAX

    # ---- 画框：人物层永远 1:1，缩放只改画框（不重采样、不生成）----
    hx0, hy0, hx1, hy1 = hard[0]
    if frame is not None:
        if float(person_scale) != 1.0:
            raise ComposeRejected("FRAME_CONFLICT",
                                  "frame 与 person_scale 不可同时指定（两条推导路径会互相覆盖）")
        ox, oy, cw, ch = (int(v) for v in frame)
        rx = ry = None
        if cw <= 0 or ch <= 0:
            raise ComposeRejected("FRAME_SIZE_INVALID", f"画框尺寸非法：{cw}×{ch}")
        eff_scale = round(src_w / cw, 4)
    else:
        if not (MIN_PERSON_SCALE <= float(person_scale) <= MAX_PERSON_SCALE):
            raise ComposeRejected("ZOOM_OUT_OF_RANGE",
                                  f"人物缩放 {person_scale} 超出允许范围 "
                                  f"[{MIN_PERSON_SCALE}, {MAX_PERSON_SCALE}]")
        rx, ry = ((float(anchor[0]), float(anchor[1])) if anchor
                  else (round((hx0 + hx1) / 2 / src_w, 4), round((hy0 + hy1) / 2 / src_h, 4)))
        if not (0.0 <= rx <= 1.0 and 0.0 <= ry <= 1.0):
            raise ComposeRejected("ANCHOR_INVALID", f"锚点需为归一化坐标 [0,1]：{anchor}")
        cw, ch, ox, oy = _derive_frame(float(person_scale), rx, ry, src_w, src_h)
        eff_scale = round(src_w / cw, 4)

    gates = frame_gates(alpha_bin, hard, ox, oy, cw, ch, head_margin)
    blocking = [c for c in gates["codes"]
                if not (c == "FRAME_REVEALS_SOURCE_CUT" and allow_frame_reveal)]
    if blocking:
        raise ComposeRejected(blocking[0],
                              _frame_problem_message(blocking[0], gates, cw, ch, ox, oy),
                              {k: gates[k] for k in ("person_top_px", "head_margin_px",
                                                     "reveal_visible", "codes")})
    bg_upscale = round(max(cw / src_w, ch / src_h), 3)

    # unmix 需要先拿到"源图原背景"B_orig：必须用**未被硬保护区改写**的 alpha 判定背景像素
    bg_est: Optional[Dict[str, Any]] = None
    if edge_mode == "unmix":
        bg_est = estimate_source_background(_read(hero["storage_uri"]),
                                            alpha_png=_encode_png(alpha_raw))

    alpha_arr = alpha_raw.copy()
    covered = True
    coverage: List[Dict[str, Any]] = []
    for (x0, y0, x1, y1) in hard:
        region = alpha_arr[y0:y1, x0:x1]
        full = bool((region >= 255).all())
        coverage.append({"box": [x0, y0, x1, y1], "mask_covers": full})
        if not full:
            covered = False
        alpha_arr[y0:y1, x0:x1] = 255
    alpha_img = Image.fromarray(alpha_arr)

    # 画布背景：场景底板缩放到画框尺寸（背景不承载身份，允许重采样；尺寸相同时不重采样）
    bg_canvas = bg_img if bg_img.size == (cw, ch) else bg_img.resize((cw, ch), Image.LANCZOS)
    bg_rgb = np.array(bg_canvas.convert("RGB"))

    # 人物层（预乘形式）：α==1 处 P 严格等于源图像素，故"硬保护区逐像素不变"与画框无关。
    #   edge_mode="alpha"：P = C（源图颜色原样搬）
    #   edge_mode="unmix"：P = C - (1-α)·B_orig（先剥掉源图原背景的贡献）
    # 之后统一为 out = P + (1-α)·B_new，两种模式共用同一条确定性公式。
    hero_rgb = np.array(hero_img.convert("RGB")).astype(np.float32)
    a01 = alpha_arr.astype(np.float32) / 255.0
    if edge_mode == "unmix":
        b_orig = _eval_bg_field(bg_est["coef"], src_w, src_h, bg_est["deg"])
        P = hero_rgb - (1.0 - a01)[..., None] * b_orig
    else:
        P = hero_rgb

    vis = _visible_region(ox, oy, cw, ch, src_w, src_h)
    canvas = bg_rgb.astype(np.float32)
    edge_stats = None
    if vis:
        sx0, sy0, sx1, sy1, cx0, cy0 = vis
        reg_alpha = a01[sy0:sy1, sx0:sx1]
        reg_bg = canvas[cy0:cy0 + (sy1 - sy0), cx0:cx0 + (sx1 - sx0)]
        canvas[cy0:cy0 + (sy1 - sy0), cx0:cx0 + (sx1 - sx0)] = (
            P[sy0:sy1, sx0:sx1] + (1.0 - reg_alpha)[..., None] * reg_bg)
        edge_stats = edge_residual(np.clip(canvas, 0, 255).round().astype(np.uint8)
                                   [cy0:cy0 + (sy1 - sy0), cx0:cx0 + (sx1 - sx0)],
                                   reg_bg.round().astype(np.uint8),
                                   alpha_arr[sy0:sy1, sx0:sx1])
    composed = Image.fromarray(np.clip(canvas, 0.0, 255.0).round().astype(np.uint8))

    ok, reasons = mask_is_usable(mask_asset_id, hero_asset_id)
    check = verify_preservation(_read(hero["storage_uri"]),
                                _encode_png(np.array(composed)), hard,
                                source_offset=(ox, oy))
    if not check["pixel_identical"]:
        raise ComposeRejected("COMPOSE_DRIFTED",
                              "硬保护区内像素被改动：合成不得改变身份特征区",
                              {"diffs": check["regions"]})

    out_bytes = _encode_png(np.array(composed))
    result = {
        "identity_id": identity_id,
        "hero_asset_id": hero_asset_id,
        "background_asset_id": background_asset_id,
        "mask_asset_id": mask_asset_id,
        "edge_mode": edge_mode,
        "frame": {
            "canvas_size": [cw, ch],
            "source_size": [src_w, src_h],
            "source_offset": [ox, oy],
            "person_scale_requested": (None if frame is not None else float(person_scale)),
            "person_scale_effective": eff_scale,
            "anchor": ([rx, ry] if frame is None else None),
            "explicit_frame": ([ox, oy, cw, ch] if frame is not None else None),
            "person_layer": "1:1 逐像素复制（缩放只改画框，不重采样人物）",
            "background_upscale": bg_upscale,
            "background_soft_warning": ("" if bg_upscale <= MAX_BG_UPSCALE else
                                        f"背景底板被放大 {bg_upscale}×，会发虚；"
                                        f"如需更小的画中人物，应改用更宽构图的主图或更高分辨率场景底板"),
            "preservation_strength": "bit_exact",
            "head_margin_px": gates["head_margin_px"],
            "revealed_cut": gates["reveal_visible"],
            "frame_reveal_acknowledged": bool(allow_frame_reveal and gates["reveal_visible"]),
            "note": "人物像素全程 1:1 复制；缩放与构图只改画框，故硬保护区断言强度不下降",
        },
        "edge_decontam": ({
            "method": bg_est["method"],
            "basis_terms": bg_est["basis_terms"],
            "samples": bg_est["samples"],
            "fallback": bg_est["fallback"],
            "bg_field_residual_p99": bg_est.get("bg_field_residual_p99"),
            "source_bg_corner_median_rgb255": bg_est["corner_median_rgb255"],
            "formula": "C' = C + (1-alpha)*(B_new - B_orig)",
        } if bg_est else {"method": "none", "note": "alpha 模式：未做边缘去污"}),
        "edge_residual": (edge_stats or {"band_pixels": 0, "mean_abs_diff": None,
                                         "p95_abs_diff": None}),
        "pixel_size": list(composed.size),
        "output_sha256": _sha(out_bytes),
        "output_bytes": len(out_bytes),
        "hard_protect_check": check,
        "hard_protect_enforced": not covered,
        "mask_protect_coverage": coverage,
        "mask_quality_warning": ("" if covered else
                                 "mask 未完整覆盖硬保护区，合成时已强制保留；"
                                 "说明该 mask 边缘偏紧，人工确认时应重点检查轮廓"),
        "identity_claim": "not_run",
        "identity_claim_note": "本模块不做身份识别，不声称产物即同一人物；"
                               "它只保证硬保护区内逐像素与源图相同。身份一致性仍需人工/审核结论。",
        "production_ready": bool(ok) and not (require_confirmed and reasons),
        "candidate": not ok,
        "blocking_reasons": reasons,
        "composed_png": out_bytes,
        "out_uri": out_uri,
        "created_at": now_iso(),
    }
    return result


def verify_preservation(source_bytes: bytes, composed_bytes: bytes,
                        hard_protect: Sequence[Sequence[int]],
                        source_offset: Sequence[int] = (0, 0)) -> Dict[str, Any]:
    """硬保护区逐像素断言：最大通道差必须严格为 0。

    这是"身份零漂移"的**可验证依据**，不是主观描述，也不是阈值近似。

    `source_offset` 支持画框式缩放：人物层 1:1 复制到画框的整数偏移处，
    因此源图坐标 (x, y) 对应产物坐标 (x+ox, y+oy) —— 断言按该映射比对，
    缩放前后**同样是"差 1 即失败"**，不因改变构图而放宽。
    """
    import numpy as np

    a = np.array(_load(source_bytes).convert("RGB")).astype(int)
    b = np.array(_load(composed_bytes).convert("RGB")).astype(int)
    ox, oy = (int(v) for v in source_offset)
    bh, bw = b.shape[:2]
    regions = []
    identical = True
    for box in hard_protect:
        x0, y0, x1, y1 = (int(v) for v in box)
        cx0, cy0, cx1, cy1 = x0 + ox, y0 + oy, x1 + ox, y1 + oy
        if not (0 <= cx0 < cx1 <= bw and 0 <= cy0 < cy1 <= bh):
            regions.append({"box": [cx0, cy0, cx1, cy1], "max_channel_diff": None,
                            "error": "该区域落在画框之外：不得裁掉身份特征区"})
            identical = False
            continue
        d = np.abs(a[y0:y1, x0:x1] - b[cy0:cy1, cx0:cx1])
        mx = int(d.max()) if d.size else 0
        regions.append({"box": [cx0, cy0, cx1, cy1], "source_box": [x0, y0, x1, y1],
                        "max_channel_diff": mx})
        if mx != 0:
            identical = False
    return {
        "pixel_identical": identical,
        "regions": regions,
        "source_offset": [ox, oy],
        "method": "逐像素最大通道差，必须严格为 0（非阈值判定）；按画框偏移映射比对",
    }


def _workspace_root() -> str:
    """工作区根目录（含 `AVATAR_STUDIO_WORKSPACE` 对测试临时目录的覆盖）。

    与 `person_image.workspace_root()` 共用同一约定，两处**必须解析到同一个根**：
    若一个把产物写到临时目录、另一个从真实工作区读，就会出现"文件明明落盘了却读不到"，
    而这种失败只在测试里出现，很容易被误判成产品缺陷。
    """
    from .person_image import workspace_root

    return workspace_root()


def _read(rel_uri: str) -> bytes:
    import os

    path = rel_uri if os.path.isabs(rel_uri) else os.path.join(_workspace_root(), rel_uri)
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ComposeRejected("ASSET_FILE_UNREADABLE",
                              f"资产文件不可读：{rel_uri}（{exc.strerror}）")


def read_asset_bytes(storage_uri: str) -> bytes:
    """`_read` 的对外别名：同包上层（如 variant_factory）需要按登记的 URI 取原图字节。

    单独开这个口子，是为了让上层不必依赖私有函数名；读取语义与错误类型完全一致。
    """
    return _read(storage_uri)


def _workspace_dir() -> str:
    """backend/app/avatar → 上溯四级 = 工作区根（assets/ 与 experiments/ 所在）。"""
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))


WORKSPACE_DIR = _workspace_dir()
# ↑ 默认根（由本文件位置推导）。实际读取走 `_workspace_root()`，以便测试把产物
#   导向临时目录；生产环境未设置 AVATAR_STUDIO_WORKSPACE，两者完全相同。
