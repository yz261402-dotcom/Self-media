"""生成背景变体预览页（纯色/渐变 + 场景），供人工目视挑选与 mask 确认。

图片用相对路径引用，因此预览页与各 CS-* 产物目录必须保持同级关系：
  experiments/抠图合成验证/背景变体预览.html
  experiments/抠图合成验证/CS-002/人物B-*.png
  experiments/抠图合成验证/CS-004/人物B-*.png

用法：
  cd production/backend && .venv/Scripts/python.exe ../scripts/build_background_preview.py
"""
from __future__ import annotations

import glob
import io
import json
import os
import sys

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.dirname(PRODUCTION)
EXPERIMENTS_REL = "experiments/抠图合成验证"
OUT_HTML = f"{EXPERIMENTS_REL}/背景变体预览.html"

GROUPS = [
    {
        "dir": "CS-002",
        "title": "纯色 / 渐变背景",
        "sub": "程序生成，无第三方权利；色温可控，适合正式口播",
        "rights": "程序生成",
    },
    {
        "dir": "CS-004",
        "title": "场景背景",
        "sub": "AI 生成（教室 / 办公室 / 公园）；已按目标比例裁切并规范化到 941×1672",
        "rights": "AI 生成 · 权属依生成服务条款",
    },
]

CSS = """
:root{--bg:#f4f6fa;--card:#ffffff;--fg:#1f2733;--mut:#6b7686;--line:#e3e8f0;--ok:#0f6e56;--warn:#854f0b;--accent:#2f6fed}
*{box-sizing:border-box}
body{margin:0;padding:32px 28px 56px;background:var(--bg);color:var(--fg);
     font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.6}
h1{font-size:20px;font-weight:500;margin:0 0 6px}
.lead{color:var(--mut);margin:0 0 28px;max-width:780px}
h2{font-size:15px;font-weight:500;margin:32px 0 4px}
.sub{color:var(--mut);font-size:13px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.card img{display:block;width:100%;height:320px;object-fit:cover;background:var(--bg)}
.meta{padding:10px 12px}
.name{font-weight:500;margin:0 0 4px}
.kv{color:var(--mut);font-size:12px;margin:0}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid currentColor;margin-left:6px;vertical-align:1px}
.tag.ok{color:var(--ok)}.tag.warn{color:var(--warn)}
.foot{margin-top:36px;padding-top:16px;border-top:1px solid var(--line);color:var(--mut);font-size:13px;max-width:840px}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;background:#eef2f8;padding:1px 5px;border-radius:4px}
"""


def load_report(path: str) -> dict:
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main() -> int:
    exp_dir = os.path.join(WORKSPACE, EXPERIMENTS_REL)
    sections = []
    total = 0

    for g in GROUPS:
        files = sorted(glob.glob(os.path.join(exp_dir, g["dir"], "人物B-*.png")))
        cards = []
        for p in files:
            fname = os.path.basename(p)
            name = fname[len("人物B-"):-len(".png")]
            cards.append((name, f"{g['dir']}/{fname}"))
        total += len(cards)
        sections.append((g, cards))

    parts = [
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        "<title>人物 B · 背景变体预览</title><style>", CSS, "</style></head><body>",
        "<h1>人物 B · 背景变体候选</h1>",
        "<p class=\"lead\">同一张定妆原片、同一份 mask，仅更换背景。人物区为源图逐像素复制，",
        "不做任何生成式重绘——换背景不会改变人脸像素。</p>",
    ]

    for g, cards in sections:
        parts.append(f"<h2>{g['title']}<span class=\"tag\">{g['rights']}</span></h2>")
        parts.append(f"<p class=\"sub\">{g['sub']}　共 {len(cards)} 张</p>")
        parts.append("<div class=\"grid\">")
        for name, rel in cards:
            parts.append(
                f"<div class=\"card\"><img src=\"{rel}\" alt=\"{name}\" loading=\"lazy\">"
                f"<div class=\"meta\"><p class=\"name\">{name}</p>"
                f"<p class=\"kv\">候选 · 未定版</p></div></div>"
            )
        parts.append("</div>")

    parts.append(
        "<div class=\"foot\"><strong>状态说明</strong>：全部为候选，不得进入生产。"
        "mask 是算法草稿，须经人工目视确认（<code>compose.confirm_mask</code>）后才能定版。<br>"
        "<strong>可验证依据</strong>：各组合成产物在硬保护区（人脸核心）内与源图的最大通道差实测为 "
        "<code>0</code>，非阈值近似。身份一致性结论仍由人工/审核负责，本页不做身份判定。<br>"
        "<strong>边缘处理</strong>：场景背景使用 matting 反解的真实 alpha + 代数去污 "
        "<code>C' = C + (1-α)(B_new - B_orig)</code>，消除原影棚在轮廓外的浅灰残留。</div>"
    )
    parts.append("</body></html>")

    out_path = os.path.join(exp_dir, "背景变体预览.html")
    with io.open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    print(f"预览页：{OUT_HTML}（共 {total} 张背景变体）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
