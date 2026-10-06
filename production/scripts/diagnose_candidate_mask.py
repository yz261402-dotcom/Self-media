"""渲染候选图 mask 的"配置对比图"：一眼看清贴边的到底是人物还是背景斑块。

背景（CS-006）：参数扫描显示没有任何 (deg, thr_scale) 能做到四边不贴边，
但某些配置下**最大连通块的长宽比稳定在 0.28~0.30**——这正是正面站姿人物的比例。
这暗示人物本体已被正确分离，贴边的是零散背景斑块。本脚本把这一判断变成可看的图：

对每个配置渲染三种 mask 叠加：
  ① 全 mask（当前 estimate_mask 的输出：保留所有面积达标的前景块）
  ② 仅"含人脸硬保护区的那个连通块"（人形种子块）
  ③ 仅最大连通块
并打印每种的四边贴边像素数。

判据：若 ① 贴边而 ② 不贴边，说明问题在"多留了背景块"，而非人物本身被画框裁断
——这正是"人物能否缩小置身场景"的决定性区别。

不做自动结论：脚本只给数字与图，是否采用由人工看证据图决定。
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

# 配置来自 候选mask扫描.json 里各自最优的几组（贴边最少 / 主块最像人形）
CONFIGS: List[Tuple[str, str, int, float]] = [
    ("重绘C-全身80%", "02-50-29", 4, 8.0),
    ("重绘C-全身80%", "02-50-29", 6, 8.0),
    ("重绘D-全身88%", "02-50-32", 4, 8.0),
    ("重绘D-全身88%", "02-50-32", 6, 8.0),
    ("重绘E-中灰底·全身", "02-54-52", 6, 8.0),
    ("重绘E-中灰底·全身", "02-54-52", 6, 4.0),
    ("重绘F-中蓝底·全身", "02-54-48", 8, 8.0),
]


def _font(size: int = 20):
    from PIL import ImageFont
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _core() -> Dict[str, List[int]]:
    p = os.path.join(WORKSPACE, OUT_DIR_REL, "重绘探针报告.json")
    d = json.load(open(p, encoding="utf-8"))
    return {k: list(v["hard_protect"]) for k, v in d["geometry"].items()}


def _find(ts: str) -> str:
    hits = glob.glob(os.path.join(WORKSPACE, GEN_DIR_REL, f"*{ts}.png"))
    if not hits:
        raise SystemExit(f"找不到候选图：{ts}")
    return hits[0]


def _leak(ab, np) -> Dict[str, int]:
    return {"left": int(ab[:, 0].sum()), "right": int(ab[:, -1].sum()),
            "top": int(ab[0, :].sum()), "bottom": int(ab[-1, :].sum())}


def _safe(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum() or ch in "-_")


def main() -> int:
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    from app.avatar import compose

    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)
    cores = _core()
    fo = _font(17)
    fo_s = _font(15)
    summary: List[Dict[str, Any]] = []

    for tag, ts, deg, thr in CONFIGS:
        path = _find(ts)
        hp = cores[tag]
        rgb = np.array(Image.open(path).convert("RGB"))
        h, w = rgb.shape[:2]
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="PNG")
        est = compose.estimate_mask(buf.getvalue(), method="bgmodel", hard_protect=[hp],
                                    deg=deg, thr_scale=thr, iters=5, alpha_source="matting")
        ab = np.array(Image.open(io.BytesIO(est["mask_alpha_png"])).convert("L")) > compose.MASK_BINARY_MAX
        n_lab, lab, stats, _c = cv2.connectedComponentsWithStats(ab.astype(np.uint8))
        seed_lab = int(lab[hp[1] + (hp[3] - hp[1]) // 2, hp[0] + (hp[2] - hp[0]) // 2])
        variants: Dict[str, Any] = {
            "① 全mask（现管线）": ab,
            "② 含人脸种子的连通块": (lab == seed_lab),
        }
        if n_lab > 1:
            main = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            variants["③ 最大连通块"] = (lab == main)
        info = {"tag": tag, "deg": deg, "thr_scale": thr, "face_core": hp,
                "seed_label_area": int((lab == seed_lab).sum()),
                "n_components": n_lab - 1, "variants": {}}
        panels = []
        for name, m in variants.items():
            leak = _leak(m, np)
            rows, cols = np.where(m.any(axis=1))[0], np.where(m.any(axis=0))[0]
            box = [int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())]
            bh, bw = box[3] - box[1] + 1, box[2] - box[0] + 1
            info["variants"][name] = {"border_leak": leak, "border_leak_total": sum(leak.values()),
                                      "bbox": box, "area_ratio": round(float(m.mean()), 4),
                                      "aspect": round(bw / max(1, bh), 3)}
            vis = rgb.copy()
            cnts, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(vis, cnts, -1, (0, 220, 0), 2)
            cv2.rectangle(vis, (box[0], box[1]), (box[2], box[3]), (255, 0, 220), 3)
            cv2.rectangle(vis, (hp[0], hp[1]), (hp[2] - 1, hp[3] - 1), (0, 200, 255), 3)
            im = Image.fromarray(vis).resize((300, 450))
            d = ImageDraw.Draw(im)
            d.text((6, 6), name, fill=(255, 255, 0), font=fo)
            d.text((6, 28), f"贴边={sum(leak.values())}  面积={m.mean()*100:.1f}%  "
                            f"长宽比={bw / max(1, bh):.3f}", fill=(255, 255, 0), font=fo_s)
            panels.append(im)
        sheet = Image.new("RGB", (len(panels) * 310 + 10, 450 + 26), (250, 250, 248))
        ImageDraw.Draw(sheet).text((8, 4), f"{tag}   deg={deg} thr_scale={thr}  "
                                         f"候选块数={n_lab - 1}", fill=(20, 20, 20), font=fo_s)
        for i, im in enumerate(panels):
            sheet.paste(im, (10 + i * 310, 24))
        sheet.save(os.path.join(out_dir, f"诊断连通块-{_safe(tag)}-d{deg}t{thr:g}.png"))
        summary.append(info)
        print(f"\n[{tag}] deg={deg} thr={thr}  块数={n_lab - 1}")
        for name, v in info["variants"].items():
            print(f"   {name:<22} 贴边合计={v['border_leak_total']:>6}  "
                  f"面积={v['area_ratio']*100:5.2f}%  长宽比={v['aspect']:.3f}  bbox={v['bbox']}")

    with io.open(os.path.join(out_dir, "诊断连通块报告.json"), "w", encoding="utf-8") as f:
        json.dump({"purpose": "区分'贴边的是人物'还是'贴边的是背景斑块'",
                   "note": "②是'含人脸硬保护区的连通块'——人脸框经人工核验在人物内部，"
                           "故该块必属人物；但它可能通过投影与背景连通，需看证据图",
                   "runs": summary}, f, ensure_ascii=False, indent=2)
    print(f"\n报告：{OUT_DIR_REL}/诊断连通块报告.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
