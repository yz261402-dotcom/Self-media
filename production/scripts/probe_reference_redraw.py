"""参考图重绘探针：量化「图生图通道」的身份保真度与构图几何。

为什么需要它（CS-005 结论）：现有主图是 941×1672 的胸像特写，人物剪影贴着
左/右/下三条边。任何"把画框外扩让人物显得更小"的做法都会露出源图裁断的断面，
而算法补不出不存在的像素。正解是换一张**四周都有余量的主图**。

本脚本给出可复核的数字，不含主观判断：

  1. **人脸标定**：用肤色块定位头部、再用块内暗区（眉/眼）定出眼位，
     以瞳距（IPD）为尺度单位取固定比例窗口。这样窗口内容跨图可比，
     不会把脖子、衣物、背景算进比对区（这是纯肤色框取窗的致命缺陷）。
  2. **身份保真度**：把各重绘图与原图的人脸窗口归一化到同一画幅后逐像素比对，
     只统计窗口中央 78%（避开背景）。含两类对照：
       · 上界对照：原图人脸自身"降采样往返" —— 内容不变时该指标的最好值。
       · 交叉对照：两次独立重绘之间 —— 若彼此高度相似却都与原图差距明显，
         说明通道是在**重建一张近似脸**，而非忠实复写原图。
  3. **构图几何**：人物剪影是否贴边、可用缩放区间（决定能否缩小置身场景）。
  4. **通道局限存档**：请求透明底 → 通道把"透明"画成棋盘格纹理（输出 RGB，无 alpha）。

红线：本脚本**不做身份识别**，不声称"这是同一个人"。只输出像素/结构相似度与并排对照图，
身份结论由人工判定（项目无可用人脸识别权重，见 licensing 模块的核验结论）。
"""
from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

HERO_REL = "assets/固定主播/主播B/版本1/基础人物.png"
GEN_DIR_REL = "assets/固定主播/主播B/版本2候选/生成"
OUT_DIR_REL = "experiments/抠图合成验证/CS-006"
REGISTERED_HARD_PROTECT = (330, 200, 560, 500)   # 版本1 已登记的人脸核心（用于自洽性校验）

# 以瞳距为单位的人脸窗口（经典人像比例）：宽 2.35 IPD、高 3.30 IPD，眼位在窗口高 32% 处
WIN_W_IPD, WIN_H_IPD, EYE_AT = 2.35, 3.30, 0.32
NORM_W, NORM_H = 235, 330
CORE_RATIO = 0.78          # 指标只统计窗口中央 78%（避开背景与发梢）
MASK_PARAMS = {"deg": 4, "iters": 5, "thr_scale": 3.0, "alpha_source": "matting"}


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _font(size: int = 16):
    """取一个支持中文的字体；找不到就退回 PIL 默认字体（会画成方块）。

    证据图上的标注是中文（标签、读数）。PIL 默认位图字体不含 CJK 字形，
    之前所有对照图的标签其实都是**方块乱码**，等于证据图不可读。
    """
    from PIL import ImageFont

    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyh.ttf",
              r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _safe(tag: str) -> str:
    """把标签变成安全的文件名片段（去掉路径分隔符与标点）。"""
    out = []
    for ch in tag:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch in "·（）、/ ":
            out.append("_" if ch == " " else "")
        else:
            out.append("")
    s = "".join(out).strip("_")
    while "__" in s:
        s = s.replace("__", "_")
    return s or "item"


def _save_mask_evidence(rgb, ab, hp, main_box, tag, out_dir, np, cv2, Image, ImageDraw, g) -> None:
    """落盘两件证据：二值 mask 本体 + 叠加标注图。

    为什么必须出图：报告里的"贴边"数字完全取决于 mask 抠得对不对。CS-006 实测到
    中蓝底/中灰底候选的背景是**绘画性斑驳纹理**，四阶多项式曲面解释不了，残差超阈值
    → mask 侵蚀进背景、被算成"人物贴边"，把构图判死。只看数字会得出与肉眼相反的结论。
    """
    stem = _safe(tag)
    Image.fromarray((ab * 255).astype(np.uint8)).save(os.path.join(out_dir, f"诊断mask-{stem}.png"))

    vis = rgb.copy()
    cnts, _ = cv2.findContours(ab.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(vis, cnts, -1, (0, 220, 0), 2)          # 绿：mask 边界
    mx, my, mx1, my1 = main_box
    cv2.rectangle(vis, (mx, my), (mx1 - 1, my1 - 1), (255, 0, 220), 3)   # 品红：最大连通块
    cv2.rectangle(vis, (hp[0], hp[1]), (hp[2] - 1, hp[3] - 1), (0, 200, 255), 3)  # 青：人脸硬保护区
    im = Image.fromarray(vis)
    d = ImageDraw.Draw(im)
    fo = _font(20)
    d.text((10, 10), f"{tag}  前景占比 {g['foreground_ratio']}  "
                     f"最大块占比 {g['main_component_area_ratio']*100:.1f}%",
           fill=(255, 255, 0), font=fo)
    d.text((10, 36), f"绿=mask边界  品红=最大连通块  青=人脸硬保护区  "
                     f"全mask贴边={g['touching_edges'] or '无'}  "
                     f"本体贴边={g['main_component_touching_edges'] or '无'}",
           fill=(255, 255, 0), font=fo)
    im.save(os.path.join(out_dir, f"诊断mask叠加-{stem}.png"))


def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jsonable(obj, depth: int = 0):
    """递归压成纯数据，遇到不可序列化对象**标注后剔除**，而不是抛异常。

    为什么需要它：报告里混着 PIL.Image（用于渲染对照图）与 numpy 大数组（_rgb）。
    json.dump 是流式写盘——一旦中途抛 TypeError，文件里前半段字段已经落盘，
    看起来像个"正常但短"的报告，极易被当成完整产物（本项目就已踩过一次）。
    这里把不可序列化的值替换成占位字符串，保证报告要么完整、要么明显残缺。
    """
    if depth > 12:
        return "<深度超限>"
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v, depth + 1) for v in obj]
    if hasattr(obj, "shape") and hasattr(obj, "dtype"):      # numpy / cv2 数组
        return f"<ndarray {tuple(obj.shape)} {obj.dtype}>"
    return f"<{type(obj).__name__}>（不可序列化，已剔除）"


# ------------------------------------------------------------------ 人脸标定

def _skin_mask(rgb, cv2, np, upper_ratio: float = 0.55):
    """经典肤色规则。目标：框住「脸+脖子」，排除浅米色西装/西裤（|R-G|≈8<15）、
    浅蓝内搭（B>R）、黑发（低亮度）、地面（低饱和）。"""
    r = rgb[..., 0].astype(int)
    g = rgb[..., 1].astype(int)
    b = rgb[..., 2].astype(int)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    m = ((r > 95) & (g > 40) & (b > 20)
         & ((mx - mn) > 15) & (np.abs(r - g) > 15) & (r > g) & (r > b))
    m[int(m.shape[0] * upper_ratio):, :] = False   # 排除手部（全身图的手在画面下部）
    return m


def detect_eye_line(rgb, cv2, np) -> Optional[Dict[str, Any]]:
    """肤色块 → 块内暗区（眉/眼）→ 左右中位数定眼位，得瞳距 IPD。"""
    h, w = rgb.shape[:2]
    skin = _skin_mask(rgb, cv2, np).astype(np.uint8)
    skin = cv2.morphologyEx(skin, cv2.MORPH_CLOSE, np.ones((17, 17), np.uint8))
    skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    n, lab, stats, _c = cv2.connectedComponentsWithStats(skin)
    if n <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    i = 1 + int(np.argmax(areas))
    if int(stats[i, cv2.CC_STAT_AREA]) < h * w * 0.003:
        return None
    bx, by = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
    bw, bh = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])

    blob_local = (lab[by:by + bh, bx:bx + bw] == i).astype(np.uint8)

    # 暗区取自肤色块内部。**不做大半径腐蚀**：黑发天然不在肤色块内，无需靠腐蚀排除；
    # 而版本1 的肤色块上边缘恰好卡在眉线，腐蚀半径 32px 会把眼睛整个吃掉，
    # 只剩嘴部阴影被当成"眼"（实测把标记画到了嘴唇上）。只做 3px 腐蚀去除边界毛刺。
    inner = cv2.erode(blob_local, np.ones((3, 3), np.uint8))
    gray = cv2.cvtColor(rgb[by:by + bh, bx:bx + bw], cv2.COLOR_RGB2GRAY)
    dark = ((gray < 118) & (inner > 0)).astype(np.uint8)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    nd, _l2, st2, ce2 = cv2.connectedComponentsWithStats(dark)
    cands = []
    for j in range(1, nd):
        a = int(st2[j, cv2.CC_STAT_AREA])
        cxc, cyc = ce2[j]
        if a < max(15, bw * bh * 0.0008) or a > bw * bh * 0.06:
            continue                       # 太小=噪点；太大=整片阴影/头发
        if cyc > bh * 0.58:
            continue                       # 眼睛在脸的上半部
        cands.append((float(cxc), float(cyc), a))
    if len(cands) < 2:
        print(f"    [调试] 肤色块 {bw}×{bh}，块内暗结构仅 {len(cands)} 个 → 标定失败")
        return None

    # 眉/眼是脸内最靠上的一对暗结构。按「越靠上、越同高」打分取最优配对，
    # 从而自动排除鼻孔、唇线等更低的暗结构，无需人工指定。
    best = None
    for p in range(len(cands)):
        for q in range(p + 1, len(cands)):
            xa, ya, aa = cands[p]
            xb, yb, ab_ = cands[q]
            dx, dy = abs(xb - xa), abs(yb - ya)
            if not (0.18 * bw <= dx <= 0.85 * bw) or dy > 0.12 * bh:
                continue
            score = (ya + yb) / 2.0 + 0.5 * dy
            if best is None or score < best[0]:
                best = (score, xa, ya, xb, yb, aa + ab_)
    if best is None:
        print(f"    [调试] 暗结构 {len(cands)} 个但无合法配对（水平间距/等高不满足）→ 标定失败")
        return None
    _s, xa, ya, xb, yb, dark_area = best
    if xa > xb:
        xa, xb, ya, yb = xb, xa, yb, ya
    ipd = xb - xa
    if ipd <= 4:
        return None
    return {
        "skin_blob": [bx, by, bx + bw, by + bh],
        "skin_blob_size": [bw, bh],
        "dark_candidates": len(cands),
        "dark_area": int(dark_area),
        "eye_left": [round(bx + xa, 1), round(by + ya, 1)],
        "eye_right": [round(bx + xb, 1), round(by + yb, 1)],
        "ipd_px": round(ipd, 2),
        "eye_y": round(by + (ya + yb) / 2.0, 1),
        "eye_cx": round(bx + (xa + xb) / 2.0, 1),
    }


def face_core_from_eyes(face: Dict[str, Any]) -> List[int]:
    """由眼位与瞳距推人脸核心框（眉到下巴、含两颊、不含背景）。

    比例：宽 1.90×IPD、高 2.35×IPD，眼线在框高 32% 处。
    该比例对版本1 复现其已登记硬保护区 (330,200,560,500)，见探针报告的自洽性校验。
    """
    ipd, cx, ey = face["ipd_px"], face["eye_cx"], face["eye_y"]
    return [int(round(cx - 0.95 * ipd)), int(round(ey - 0.75 * ipd)),
            int(round(cx + 0.95 * ipd)), int(round(ey + 1.60 * ipd))]


def face_window(face: Dict[str, Any], shape) -> Dict[str, Any]:
    ipd, cx, ey = face["ipd_px"], face["eye_cx"], face["eye_y"]
    ww, wh = WIN_W_IPD * ipd, WIN_H_IPD * ipd
    left = cx - ww / 2.0
    top = ey - EYE_AT * wh
    h, w = shape[:2]
    clamped = []
    if left < 0 or top < 0 or left + ww > w or top + wh > h:
        clamped = [round(max(0.0, -left)), round(max(0.0, -top)),
                   round(max(0.0, left + ww - w)), round(max(0.0, top + wh - h))]
        left = min(max(left, 0.0), max(0.0, w - ww))
        top = min(max(top, 0.0), max(0.0, h - wh))
    return {"box": [int(round(left)), int(round(top)),
                    int(round(left + ww)), int(round(top + wh))],
            "size_px": [int(round(ww)), int(round(wh))], "clamped_px": clamped}


def normalize_face(rgb, win, np):
    from PIL import Image

    return Image.fromarray(rgb).crop(tuple(win["box"])).resize((NORM_W, NORM_H), Image.LANCZOS)


# ------------------------------------------------------------------ 指标

def _core_mask(np):
    y0 = int(NORM_H * (1 - CORE_RATIO) / 2)
    y1 = NORM_H - y0
    x0 = int(NORM_W * (1 - CORE_RATIO) / 2)
    x1 = NORM_W - x0
    m = np.zeros((NORM_H, NORM_W), bool)
    m[y0:y1, x0:x1] = True
    return m


def face_metrics(a, b, np, core) -> Dict[str, Any]:
    A = np.asarray(a).astype(np.float32)[core]
    B = np.asarray(b).astype(np.float32)[core]
    d = np.abs(A - B)
    ga, gb = A.mean(axis=1).ravel(), B.mean(axis=1).ravel()
    corr = float(np.corrcoef(ga, gb)[0, 1])
    return {"mean_abs_diff": round(float(d.mean()), 2),
            "p95_abs_diff": round(float(np.percentile(d, 95)), 2),
            "gray_corr": round(corr, 4),
            "relative_l1": round(float(d.mean() / 255.0), 5)}


def resample_floor(img, np, down: int = 4):
    from PIL import Image

    w, h = img.size
    return img.resize((max(1, w // down), max(1, h // down)), Image.LANCZOS).resize((w, h), Image.LANCZOS)


def diff_map(a, b, np):
    from PIL import Image

    d = np.abs(np.asarray(a).astype(np.float32) - np.asarray(b).astype(np.float32)).mean(axis=2)
    return Image.fromarray(np.clip(d * 4.0, 0, 255).astype(np.uint8)).convert("RGB")


# ------------------------------------------------------------------ 水印定位

def locate_watermark(rgb, cv2, np) -> Dict[str, Any]:
    """半透明白色文字水印：亮、低饱和、且由**细碎小连通块**组成。

    关键在最后一条——初版只按「高亮低饱和低频残差」判定，会把米白西装的
    亮部边缘整片误报（实测在版本1 主图上报出 397px 宽的假框）。
    加入「连通块须小而细」后，长边缘线与大片衣物都会被排除。
    """
    h, w = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    bg = cv2.medianBlur(gray.astype(np.uint8), 31).astype(np.float32)
    resid = gray - bg
    sat = (rgb.max(axis=2).astype(int) - rgb.min(axis=2).astype(int)).astype(np.float32)
    cand = ((resid > 10) & (sat < 32) & (gray > 195)).astype(np.uint8)
    roi = np.zeros_like(cand)
    # ROI 必须**贴着右下角**：水印固定打在角落（CS-004 实测 941×1672 版为
    # x∈[872,1024]、y∈[1445,1536]，即 x>0.85w、y>0.94h）。
    # 早期 ROI 放宽到 (0.78h, 0.55w) 时，背景的绘画性亮斑也被聚进来，
    # 报出的 bbox 比真实水印大好几倍（F 报 567~628×1321~1525，肉眼却在最右下角）。
    roi[int(h * 0.90):, int(w * 0.72):] = 1
    cand &= roi
    n, lab, stats, _c = cv2.connectedComponentsWithStats(cand)
    keep_x, keep_y, kept = [], [], 0
    for j in range(1, n):
        a = int(stats[j, cv2.CC_STAT_AREA])
        bw_, bh_ = int(stats[j, cv2.CC_STAT_WIDTH]), int(stats[j, cv2.CC_STAT_HEIGHT])
        if a < 4 or a > 500 or bw_ > 55 or bh_ > 55:
            continue                            # 文字笔画：小而细
        keep_x += [int(stats[j, cv2.CC_STAT_LEFT]), int(stats[j, cv2.CC_STAT_LEFT]) + bw_]
        keep_y += [int(stats[j, cv2.CC_STAT_TOP]), int(stats[j, cv2.CC_STAT_TOP]) + bh_]
        kept += 1
    if kept < 8 or not keep_x:
        return {"found": False, "components": kept}
    x0, x1 = min(keep_x), max(keep_x)
    y0, y1 = min(keep_y), max(keep_y)
    # 水印是平台打在角落的固定标：必须与右/下边相邻。
    # 这条判据是"角落 ROI + 细碎块"之外的第三重保险——避免背景亮斑偶发凑够 8 块。
    corner = (x1 >= w - 4) and (y1 >= h - 4)
    if not corner:
        return {"found": False, "components": kept,
                "note": "检出的细碎块不与右下角相邻，判为背景亮斑而非水印"}
    return {"found": True, "components": kept, "pixels": int(cand.sum()),
            "bbox": [x0, y0, x1, y1], "bbox_size": [x1 - x0, y1 - y0],
            "corner_anchored": True,
            "note": "由细小低饱和高亮连通块聚类定位，且要求与右下角相邻；"
                    "用于确定裁切边界（画面裁切，非抹除）"}


# ------------------------------------------------------------------ main

def load_items(cv2, np):
    from PIL import Image

    gen = sorted(glob.glob(os.path.join(WORKSPACE, GEN_DIR_REL, "*.png")))
    tag_of = {"47-36": "重绘B-请求透明底", "47-38": "重绘A-头到腰",
              "50-29": "重绘C-全身80%", "50-32": "重绘D-全身88%",
              "54-48": "重绘F-中蓝底·全身", "54-52": "重绘E-中灰底·全身"}
    paths: List[Tuple[str, str]] = [(HERO_REL, "参考图-版本1（基准）")]
    for p in gen:
        tag = next((v for k, v in tag_of.items() if k in os.path.basename(p)), os.path.basename(p))
        paths.append((os.path.relpath(p, WORKSPACE).replace("\\", "/"), tag))

    items = []
    for rel, tag in paths:
        path = os.path.join(WORKSPACE, rel)
        rgb = np.array(Image.open(path).convert("RGB"))
        face = detect_eye_line(rgb, cv2, np)
        win = face_window(face, rgb.shape) if face else None
        items.append({
            "tag": tag, "file": rel, "size": list(rgb.shape[1::-1]),
            "sha256": _sha(open(path, "rb").read()),
            "face": face, "window": win,
            "watermark": locate_watermark(rgb, cv2, np),
            "norm": normalize_face(rgb, win, np) if win else None,
            "_rgb": rgb,
        })
    return items


WORK_H = 112                # 模板工作高度（像素）
# 为什么是 112：64px 下脸区只有 ~60×64，五官被重采样糊成色块，人工目视判不了身份；
# 112px 能看清眼型/鼻梁/唇形，且模板匹配代价仍很低（候选模板 ~105×112，
# 搜索区约 246px 高、16 个尺度，整轮 < 2s）。
# 版本1 人脸核心（眉到下巴、含两颊、不含背景）：人工网格读数 + 视觉复核
# 见 experiments/抠图合成验证/CS-006/读数网格-版本1基准.png
REF_BOX = (315, 430, 620, 755)

# 对照图上的短标签：完整标签（"重绘B-请求透明底"）有 9 个汉字，比 105px 的图块还宽，
# 会互相压成一片，证据图等于不可读。这里只保留可辨认的最短形式。
SHORT_TAG = {
    "参考图-版本1（基准）": "基准v1",
    "重绘B-请求透明底": "B 透明底",
    "重绘A-头到腰": "A 头到腰",
    "重绘C-全身80%": "C 全身80",
    "重绘D-全身88%": "D 全身88",
    "重绘F-中蓝底·全身": "F 中蓝底",
    "重绘E-中灰底·全身": "E 中灰底",
}


def _short(tag: str) -> str:
    return SHORT_TAG.get(tag, tag)


def align_face(ref_rgb, cand_rgb, region_box, np, cv2):
    """把候选图的人脸**按最优几何对齐**匹配到参考人脸模板上。

    为什么不靠五官检测：项目没有可商用的人脸关键点权重（InsightFace 权重仅限非商业研究），
    而自写的肤色/暗区启发式在实测中把发际线的分缝当成了"眼"（见 CS-006 的核验图），
    在版本1 上还会因肤色块上边缘卡在眉线而把眼睛整块腐蚀掉。不可靠的标定会让
    后续所有数字失真。

    改为：以版本1 的人脸核心（人工读数）为模板，在候选图的候选区域内做
    多尺度归一化相关搜索，取相关性最大的位置。报告的是**最优对齐下的残差**——
    这是真实差异的下界（对齐误差已被扣除，只会让差异显得更小，不会更大）。
    顺带产出的对齐裁剪图，正是人工判断身份所需的那张图。
    """
    from PIL import Image

    x0, y0, x1, y1 = REF_BOX
    tpl_img = Image.fromarray(ref_rgb).crop((x0, y0, x1, y1))
    th = WORK_H
    tw = max(1, int(round(tpl_img.width * th / tpl_img.height)))
    tpl_rgb = tpl_img.resize((tw, th), Image.LANCZOS)
    tpl_gray = np.asarray(tpl_rgb.convert("L")).astype(np.float32)

    h, w = cand_rgb.shape[:2]
    rx0, ry0, rx1, ry1 = (max(0, int(region_box[0])), max(0, int(region_box[1])),
                          min(w, int(region_box[2])), min(h, int(region_box[3])))
    if rx1 - rx0 < 20 or ry1 - ry0 < 20:
        return None
    reg_img = Image.fromarray(cand_rgb).crop((rx0, ry0, rx1, ry1))

    # 基准缩放：让搜索区高度约等于 2.2 倍模板高（人脸约占搜索区 1/2.2）
    f0 = 2.2 * th / float(ry1 - ry0)
    best = None
    for f in f0 * np.linspace(0.70, 1.42, 16):
        nw, nh = max(1, int(round(reg_img.width * f))), max(1, int(round(reg_img.height * f)))
        if nh < th or nw < tw or nh > th * 8:
            continue
        gray = np.asarray(reg_img.resize((nw, nh), Image.LANCZOS).convert("L")).astype(np.float32)
        res = cv2.matchTemplate(gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _ml, loc = cv2.minMaxLoc(res)
        if best is None or mx > best["corr"]:
            best = {"corr": float(mx), "loc": loc, "scale": float(f),
                    "scaled_size": [nw, nh]}
    if best is None:
        return None

    nw, nh = best["scaled_size"]
    lx, ly = best["loc"]
    reg_rgb = reg_img.resize((nw, nh), Image.LANCZOS)
    aligned = reg_rgb.crop((lx, ly, lx + tw, ly + th))
    A = np.asarray(aligned).astype(np.float32)
    B = np.asarray(tpl_rgb).astype(np.float32)
    d = np.abs(A - B)
    ga, gb = A.mean(axis=2).ravel(), B.mean(axis=2).ravel()
    face_h_px = th / best["scale"]          # 候选图中人脸核心的实际高度（原图像素）
    # 匹配位置换算回候选图原始坐标 → 该图的人脸核心框。
    # 它就是合成时该用的人脸硬保护区：由**人工核验过的基准模板**推出，不依赖五官检测。
    core_box = [int(round(rx0 + lx / best["scale"])), int(round(ry0 + ly / best["scale"])),
                int(round(rx0 + (lx + tw) / best["scale"])),
                int(round(ry0 + (ly + th) / best["scale"]))]
    return {
        "aligned": aligned, "template": tpl_rgb,
        "corr_peak": round(best["corr"], 4),
        "scale_used": round(best["scale"], 4),
        "region_box": [rx0, ry0, rx1, ry1],
        "match_xy_in_region_scaled": [int(lx), int(ly)],
        "face_core_h_px_in_candidate": round(face_h_px, 1),
        "face_core_box_in_candidate": core_box,
        "metrics": {"mean_abs_diff": round(float(d.mean()), 2),
                    "p95_abs_diff": round(float(np.percentile(d, 95)), 2),
                    "max_abs_diff": int(d.max()),
                    "gray_corr": round(float(np.corrcoef(ga, gb)[0, 1]), 4)},
    }


def main() -> int:
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    from app.avatar import compose

    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)

    items = load_items(cv2, np)

    print("=" * 80)
    print("一、人脸标定与水印定位")
    print("=" * 80)
    for it in items:
        f = it["face"]
        print(f"\n[{it['tag']}]  {it['size'][0]}×{it['size'][1]}")
        if f:
            print(f"  肤色块(脸+脖) {f['skin_blob_size'][0]}×{f['skin_blob_size'][1]}")
            print(f"  瞳孔间距 IPD = {f['ipd_px']}px   眼位 y={f['eye_y']}")
            print(f"  人脸窗口 {it['window']['size_px'][0]}×{it['window']['size_px'][1]}"
                  f" @ {it['window']['box']}"
                  + (f"   ⚠越界裁切 {it['window']['clamped_px']}" if it["window"]["clamped_px"] else ""))
        else:
            print("  人脸标定：失败")
        wm = it["watermark"]
        print(f"  水印：{('检出 ' + str(wm['bbox']) + '  小块数=' + str(wm['components'])) if wm['found'] else '未检出'}")

    valid = [it for it in items if it["norm"] is not None]
    if len(valid) < 2:
        print("\n可标定的人脸不足两张，无法比对。")
        return 4

    # 自洽性校验：用眼位比例推版本1 的人脸核心框，应与它**已登记**的硬保护区接近。
    # 若偏差过大，说明要么标定错了、要么比例系数不适用于该图，两种情况都必须先查清再用数字。
    hero = valid[0]
    if hero["tag"].startswith("参考图"):
        derived = face_core_from_eyes(hero["face"])
        print(f"\n[参考] 自写的眼位启发式推出 {derived}，与已登记硬保护区 "
              f"{list(REGISTERED_HARD_PROTECT)} 不一致（见 CS-006 核验图：该启发式把发际线分缝"
              f"当成了眼）。因此下面**不使用**它，也不使用已登记框——"
              f"基准人脸框由人工网格读数确定：{list(REF_BOX)}。")
        print(f"[参考] 已登记硬保护区 {list(REGISTERED_HARD_PROTECT)} 实际只覆盖额到眼；"
              f"人脸核心应为 {list(REF_BOX)}。这一偏差已记入报告待办。")

    print()
    print("=" * 80)
    print("二、人脸最优对齐比对（以版本1 人脸核心为模板，多尺度归一化相关搜索）")
    print("=" * 80)
    ref = valid[0]
    th = WORK_H
    tw = max(1, int(round((REF_BOX[2] - REF_BOX[0]) * th / (REF_BOX[3] - REF_BOX[1]))))
    tpl_work = Image.fromarray(ref["_rgb"]).crop(REF_BOX).resize((tw, th), Image.LANCZOS)

    # 上界对照：模板自身降采样往返（内容不变，纯重采样代价）
    down = tpl_work.resize((max(1, tw // 4), max(1, th // 4)), Image.LANCZOS).resize(
        (tw, th), Image.LANCZOS)
    fa = np.asarray(tpl_work).astype(np.float32)
    fb = np.asarray(down).astype(np.float32)
    dd = np.abs(fa - fb)
    floor = {"mean_abs_diff": round(float(dd.mean()), 2),
             "p95_abs_diff": round(float(np.percentile(dd, 95)), 2),
             "gray_corr": round(float(np.corrcoef(fa.mean(axis=2).ravel(),
                                                  fb.mean(axis=2).ravel())[0, 1]), 4)}
    print(f"\n上界对照 · {ref['tag']} 人脸自身降采样往返（内容不变，纯重采样代价）：")
    print(f"   平均绝对差={floor['mean_abs_diff']}  p95={floor['p95_abs_diff']}  "
          f"灰度相关={floor['gray_corr']}")

    pairs: List[Dict[str, Any]] = []
    aligned_tiles: List[Tuple[str, Any]] = []
    aligned_boxes: Dict[str, List[int]] = {}
    for other in valid[1:]:
        f = other["face"]
        if f:
            bx, by, bx1, by1 = f["skin_blob"]
            wb, hb = bx1 - bx, by1 - by
            region = (bx - 0.28 * wb, by - 0.18 * hb, bx1 + 0.28 * wb, by + 0.80 * hb)
        else:
            h, w = other["_rgb"].shape[:2]
            region = (w * 0.22, h * 0.02, w * 0.78, h * 0.45)
        res = align_face(ref["_rgb"], other["_rgb"], region, np, cv2)
        if res is None:
            print(f"\n{other['tag']}：对齐失败（搜索区无效）")
            continue
        mt = res["metrics"]
        ratio = mt["mean_abs_diff"] / max(floor["mean_abs_diff"], 1e-6)
        pairs.append({"a": ref["tag"], "b": other["tag"], **mt,
                      "corr_peak": res["corr_peak"], "scale_used": res["scale_used"],
                      "face_core_h_px_in_candidate": res["face_core_h_px_in_candidate"],
                      "face_core_box_in_candidate": res["face_core_box_in_candidate"],
                      "倍数于上界": round(ratio, 2)})
        print(f"\n{ref['tag']}  vs  {other['tag']}  （最优几何对齐后）：")
        print(f"   平均绝对差={mt['mean_abs_diff']}（上界的 {ratio:.1f} 倍）  "
              f"灰度相关={mt['gray_corr']}  匹配峰值相关={res['corr_peak']}")
        print(f"   缩放={res['scale_used']}  → 该图中人脸核心实际高 ≈ "
              f"{res['face_core_h_px_in_candidate']}px")
        aligned_tiles.append((other["tag"], res["aligned"]))
        aligned_boxes[other["tag"]] = res["face_core_box_in_candidate"]
        print(f"   人脸核心框（本图坐标）={res['face_core_box_in_candidate']}")
    print("\n   解读：残差是在**最优对齐**下测的，对齐误差已被扣除，故它只是真实差异的下界。")

    # 鉴别力自检：若"重绘 vs 基准"的残差不高于"基准自身重采样"的残差，
    # 说明该指标在同尺度下的数值被重采样模糊主导，无法区分"同一张脸"与"另一张脸"。
    weaker = [p for p in pairs if p["mean_abs_diff"] <= floor["mean_abs_diff"]]
    diagnostic = len(weaker) == 0
    print(f"\n   鉴别力自检：上界对照（基准自身重采样）={floor['mean_abs_diff']}，"
          f"{len(pairs)} 个重绘样本中有 {len(weaker)} 个残差**不高于**该上界。")
    if diagnostic:
        print("   → 指标能区分'重绘'与'重采样'，可作为弱证据。")
    else:
        print("   → **该指标无鉴别力**：把同一张脸重采样一遍的差异，竟不小于重绘另一张脸的差异。"
              "\n     说明这里的残差主要由缩放模糊量决定，而非脸部内容。"
              "\n     结论：**不引用这些数字作为身份依据**。身份是否成立只能靠人工目视对照图判定。")
    print("   警告：这是像素/结构相似度，**不是身份识别**。项目无可用人脸识别权重。")

    # ---- 人脸对照图（真正对齐过的）----
    gap, lab = 16, 26
    tiles: List[Tuple[str, Any]] = [(f"基准v1", tpl_work)] + [(_short(t), im) for t, im in aligned_tiles]
    if aligned_tiles:
        tiles.append((f"差异×4 v/{_short(aligned_tiles[0][0]).split()[0]}",
                      diff_map(tpl_work, aligned_tiles[0][1], np)))
    fo = _font(15)
    c = Image.new("RGB", (len(tiles) * (tw + gap) + gap, th + lab + gap), (250, 250, 248))
    for i, (label, im) in enumerate(tiles):
        x = gap + i * (tw + gap)
        c.paste(im, (x, lab))
        ImageDraw.Draw(c).text((x, 6), label, fill=(20, 20, 20), font=fo)
    c.save(os.path.join(out_dir, "对照-人脸对齐后.png"))
    # 纯像素放大 3×（NEAREST，无插值）：只是方便在屏幕上目视，不引入任何平滑/美化。
    # 用 LANCZOS 放大反而会把不同脸都磨得更像，污染"像不像"的判断。
    c.resize((c.width * 3, c.height * 3), Image.NEAREST).save(
        os.path.join(out_dir, "对照-人脸对齐后-放大3倍.png"))

    # ---- 标定调试图（放大脸部窗口区，画窗口框与眼位）----
    zoom = []
    for it in valid:
        rgb, win = it["_rgb"], it["window"]
        x0, y0, x1, y1 = win["box"]
        m = int(round((x1 - x0) * 0.45))
        h, w = rgb.shape[:2]
        bx0, by0 = max(0, x0 - m), max(0, y0 - m)
        bx1, by1 = min(w, x1 + m), min(h, y1 + m)
        im = Image.fromarray(rgb).crop((bx0, by0, bx1, by1))
        d = ImageDraw.Draw(im)
        d.rectangle([x0 - bx0, y0 - by0, x1 - bx0, y1 - by0], outline=(0, 200, 0), width=3)
        for key in ("eye_left", "eye_right"):
            ex, ey = it["face"][key]
            d.line([ex - bx0 - 9, ey - by0, ex - bx0 + 9, ey - by0], fill=(255, 0, 0), width=3)
            d.line([ex - bx0, ey - by0 - 9, ex - bx0, ey - by0 + 9], fill=(255, 0, 0), width=3)
        d.line([it["face"]["eye_left"][0] - bx0, it["face"]["eye_y"] - by0,
                it["face"]["eye_right"][0] - bx0, it["face"]["eye_y"] - by0],
               fill=(255, 140, 0), width=2)
        if it["watermark"]["found"]:
            wb = it["watermark"]["bbox"]
            if wb[2] > bx0 and wb[0] < bx1 and wb[3] > by0 and wb[1] < by1:
                d.rectangle([wb[0] - bx0, wb[1] - by0, wb[2] - bx0, wb[3] - by0],
                            outline=(0, 90, 255), width=3)
        zoom.append((_short(it["tag"]) + f"  IPD={it['face']['ipd_px']:.0f}px", im))
    Hn = 330
    scaled = [(t, im.resize((int(round(im.width * Hn / im.height)), Hn), Image.LANCZOS))
              for t, im in zoom]
    Wt = sum(x[1].width for x in scaled) + gap * (len(scaled) + 1)
    c2 = Image.new("RGB", (Wt, Hn + lab + gap), (250, 250, 248))
    x = gap
    fo2 = _font(16)
    for t, im in scaled:
        c2.paste(im, (x, lab))
        ImageDraw.Draw(c2).text((x, 5), t, fill=(20, 20, 20), font=fo2)
        x += im.width + gap
    c2.save(os.path.join(out_dir, "对照-脸部标定.png"))

    # ---- 构图几何 ----
    print()
    print("=" * 80)
    print("三、构图几何（能否缩小、置于场景中）")
    print("=" * 80)
    geo: Dict[str, Any] = {}
    for it in valid[1:]:
        rgb = it["_rgb"]
        hp = aligned_boxes.get(it["tag"])
        if not hp:
            print(f"\n[{it['tag']}] 无对齐出的人脸核心框 → 跳过几何测量")
            continue
        x0, y0, x1, y1 = hp
        if not (0 <= x0 < x1 <= rgb.shape[1] and 0 <= y0 < y1 <= rgb.shape[0]):
            print(f"\n[{it['tag']}] 人脸核心框越界 {hp} → 跳过几何测量")
            continue
        est = compose.estimate_mask(_png_bytes(Image.fromarray(rgb)), method="bgmodel",
                                    hard_protect=[hp], **MASK_PARAMS)
        alpha = np.array(Image.open(io.BytesIO(est["mask_alpha_png"])).convert("L"))
        ab = alpha > compose.MASK_BINARY_MAX
        h, w = ab.shape
        edges = {"left": int(ab[:, 0].sum()), "right": int(ab[:, -1].sum()),
                 "top": int(ab[0, :].sum()), "bottom": int(ab[-1, :].sum())}
        rows, cols = np.where(ab.any(axis=1))[0], np.where(ab.any(axis=0))[0]
        rng = compose.allowed_scale_range(ab, [hp])
        mm = est["params"].get("matting") or {}
        # 最大连通块 = 最可能是"人物本体"；它的 bbox 才是"人物是否贴边"的可信读数。
        # 全 mask 的 bbox 会被误入的背景斑块撑满整幅，把构图判死（见 CS-006 诊断图）。
        n_lab, lab, stats, _c = cv2.connectedComponentsWithStats(ab.astype(np.uint8))
        if n_lab > 1:
            main = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            mx, my = int(stats[main, cv2.CC_STAT_LEFT]), int(stats[main, cv2.CC_STAT_TOP])
            mw, mh = int(stats[main, cv2.CC_STAT_WIDTH]), int(stats[main, cv2.CC_STAT_HEIGHT])
            main_box = [mx, my, mx + mw, my + mh]
            main_ratio = round(float(stats[main, cv2.CC_STAT_AREA]) / (w * h), 4)
        else:
            main_box, main_ratio = [0, 0, w, h], 1.0
        main_edges = {"left": mx == 0, "right": mx + mw >= w, "top": my == 0, "bottom": my + mh >= h}
        geo[it["tag"]] = {
            "hard_protect": hp, "pixel_size": [w, h],
            "silhouette_bbox": [int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())],
            "person_height_ratio": round((int(rows.max()) - int(rows.min()) + 1) / h, 4),
            "edge_pixels": edges, "touching_edges": [k for k, v in edges.items() if v > 0],
            "main_component_box": main_box,
            "main_component_area_ratio": main_ratio,
            "main_component_touching_edges": [k for k, v in main_edges.items() if v],
            "allowed_scale_range": rng,
            "matting": {k: mm.get(k) for k in ("usable", "seed_dist_used",
                                               "uncertain_pixels", "degenerate_pixels")},
            "foreground_ratio": est["metrics"]["foreground_ratio"],
            "mask_sha256": _sha(est["mask_alpha_png"]),
        }
        _save_mask_evidence(rgb, ab, hp, main_box, it["tag"], out_dir, np, cv2, Image, ImageDraw,
                            geo[it["tag"]])
        print(f"\n[{it['tag']}]  {w}×{h}   前景占比 {geo[it['tag']]['foreground_ratio']}"
              f"   最大连通块占比 {main_ratio*100:.1f}%")
        print(f"  全 mask 剪影 bbox {geo[it['tag']]['silhouette_bbox']}"
              f"   人物占图高 {geo[it['tag']]['person_height_ratio']*100:.1f}%")
        print(f"  → 全 mask 贴边像素 {edges} → {geo[it['tag']]['touching_edges'] or '无'}")
        print(f"  → 最大连通块 bbox {main_box} 贴边 {geo[it['tag']]['main_component_touching_edges'] or '无'}")
        print(f"  可用缩放区间 [{rng['min']}, {rng['max']}]  含1.0={rng['contains_no_zoom']}")
        if not geo[it["tag"]]["touching_edges"]:
            print("  ✓ 全 mask 四周留有余量 → 可以缩小人物，真正置身场景中")
        elif not geo[it["tag"]]["main_component_touching_edges"]:
            print("  △ 人物本体四周有余量，但 mask 里有**误入的背景斑块**撑到了画框边"
                  "→ 必须先修 mask，否则会把原背景一块搬进新场景")
        else:
            print("  ✗ 人物本体自身贴住画框 → 该方向上的缩小会露断面（换图或改构图）")
        print(f"  证据图：{OUT_DIR_REL}/诊断mask叠加-{_safe(it['tag'])}.png")

    report = {
        "purpose": "验证「用原图作参考图重绘宽构图主图」的身份保真度与构图几何",
        "watermark_method": "高亮 + 低饱和 + 由**细碎小连通块**聚类（area 4–500 且 宽高≤55）。"
                            "初版只按「高亮/低饱和/低频残差」判定，会把米白西装亮部整片误报；"
                            "加入「须小而细」后长边缘线与大片衣物都被排除。",
        "watermark_hits": {it["tag"]: (it["watermark"].get("bbox") if it["watermark"]["found"] else None)
                           for it in items},
        "通道局限": [
            "请求 background=transparent：通道把「透明」画成棋盘格纹理，输出 RGB 无 alpha。",
            "输出固定 1024×1536（请求 941×1672 未生效），须裁切/重采样规范化。",
            "输出右下角带平台水印，须以画面裁切排除。",
        ],
        "items": [_jsonable({k: v for k, v in it.items()
                             if k not in ("norm", "_rgb")}) for it in items],
        "face_comparison": {
            "calibration": {"window_ipd": [WIN_W_IPD, WIN_H_IPD], "eye_at": EYE_AT,
                            "frame": [NORM_W, NORM_H], "core_ratio": CORE_RATIO,
                            "note": "以瞳距为尺度单位取窗，只统计中央区域，避开背景与发梢"},
            "resample_floor": {"pair": f"{ref['tag']} 自身降采样往返", **floor},
            "pairs": pairs,
            "method_note": "仅像素/结构相似度，不是身份识别；项目无可用人脸识别权重",
            "judgment_artifacts": {
                "对照-人脸对齐后.png": "各图人脸按基准模板做多尺度最优对齐后的同尺度并排；"
                                       "最后一格是与第一个候选重绘的差异放大 4 倍",
                "对照-人脸对齐后-放大3倍.png": "同上，NEAREST 纯像素放大 3 倍（无插值），便于目视",
                "对照-脸部标定.png": "标注肤色块/眼位的调试图，用于核验标定是否可靠",
                "short_labels": SHORT_TAG,
                "note": "身份是否成立只能由人工看这几张图判定；报告中的残差数字无鉴别力，"
                        "不得引用为身份证据",
            },
            "diff_tile_pair": (f"{ref['tag']} vs {aligned_tiles[0][0]}" if aligned_tiles else None),
        },
        "geometry": geo,
    }
    with io.open(os.path.join(out_dir, "重绘探针报告.json"), "w", encoding="utf-8") as f:
        json.dump(_jsonable(report), f, ensure_ascii=False, indent=2)
    print(f"\n人脸对照图（已对齐）：{OUT_DIR_REL}/对照-人脸对齐后.png")
    print(f"  （放大 3× 版，NEAREST 无插值）：{OUT_DIR_REL}/对照-人脸对齐后-放大3倍.png")
    print(f"脸部标定调试图：{OUT_DIR_REL}/对照-脸部标定.png")
    print(f"mask 证据图：{OUT_DIR_REL}/诊断mask叠加-*.png")
    print(f"报告：{OUT_DIR_REL}/重绘探针报告.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
