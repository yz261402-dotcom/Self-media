"""生成"人物大小"交互预览页：放大档可拖动对比 + 缩小两条路线的对照。

为什么用固定显示框：画框越小 → 同一像素内容被放大得越多 → 人物在屏幕上显得越大。
所以把各档都塞进同一个显示框，拖动滑块就能看到"人物放大"的连续效果，
而人物的**像素数量全程不变**（1:1 逐像素复制，不重采样）。

用法：
  cd production/backend && .venv/Scripts/python.exe ../scripts/build_zoom_preview.py
"""
from __future__ import annotations

import html
import json
import os
import sys
from typing import Any, Dict, List

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

IDENTITY_ID = "av_base_b_v1"
HERO_ASSET_ID = "img_745806f0e7eb"
MASK_ALPHA_REL = "assets/固定主播/主播B/版本1/遮罩/mask-matting-draft-alpha.png"
OUT_DIR_REL = "experiments/抠图合成验证/CS-005"
PAGE_REL = "experiments/抠图合成验证/人物大小预览.html"
HARD_PROTECT = [(330, 200, 560, 500)]
SCENE_KEY = "教室课堂"
ZOOM_STEPS = [1.00, 1.08, 1.16, 1.24, 1.32, 1.40]

CSS = """
:root{--bg:#f6f6f4;--card:#fff;--fg:#1c1c1a;--mut:#6b6b66;--line:#e2e0da;
      --ok:#0f6e56;--warn:#854f0b;--bad:#a32c2c;--blue:#2f6fd0}
*{box-sizing:border-box}
body{margin:0;padding:32px 28px 60px;background:var(--bg);color:var(--fg);
     font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
     font-size:14px;line-height:1.65}
h1{font-size:20px;font-weight:500;margin:0 0 6px}
.lead{color:var(--mut);margin:0 0 26px;max-width:820px}
h2{font-size:15px;font-weight:500;margin:34px 0 6px}
.sub{color:var(--mut);font-size:13px;margin:0 0 16px;max-width:820px}
.wrap{display:flex;gap:26px;align-items:flex-start;flex-wrap:wrap}
.shot{width:300px;height:533px;background:var(--card);border:1px solid var(--line);
      border-radius:10px;overflow:hidden;flex:0 0 auto}
.shot img{display:block;width:100%;height:100%;object-fit:cover}
.panel{flex:1 1 320px;min-width:300px;background:var(--card);border:1px solid var(--line);
       border-radius:12px;padding:18px 20px}
input[type=range]{width:100%;accent-color:var(--blue);margin:6px 0 2px}
.row{display:flex;justify-content:space-between;gap:12px;padding:5px 0;
     border-bottom:1px dashed var(--line);font-size:13px}
.row:last-child{border-bottom:0}
.row b{font-weight:500}
.k{color:var(--mut)}
.hint{margin-top:14px;padding:10px 12px;border-left:3px solid var(--blue);
      background:#f2f6fc;color:#33465f;font-size:12.5px;border-radius:0 6px 6px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:16px;margin-top:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.card img{display:block;width:100%;aspect-ratio:9/16;object-fit:cover;background:var(--bg)}
.meta{padding:11px 13px}
.name{font-weight:500;margin:0 0 4px;font-size:13.5px}
.kv{color:var(--mut);font-size:12px;margin:0}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;
     border:1px solid currentColor;margin-left:6px;vertical-align:1px}
.tag.ok{color:var(--ok)}.tag.warn{color:var(--warn)}.tag.bad{color:var(--bad)}
.foot{margin-top:38px;padding-top:16px;border-top:1px solid var(--line);
      color:var(--mut);font-size:13px;max-width:860px}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;
     background:#efeeea;padding:1px 5px;border-radius:4px}
"""


def main() -> int:
    from PIL import Image

    from app import storage as st
    from app.avatar import compose, registry

    st.init_db()
    hero = registry.get_asset(HERO_ASSET_ID)
    if not hero:
        print(f"源资产不存在：{HERO_ASSET_ID}")
        return 3

    spec = None
    for row in st.all_rows("mask_specs"):
        p = row.get("params") or {}
        if (row.get("source_asset_id") == hero["id"] and p.get("alpha_source") == "matting"
                and (p.get("matting") or {}).get("usable")):
            spec = row
    if spec is None:
        print("未找到 matting mask；请先运行 scripts/compose_scene_variants.py")
        return 3

    bgs = [a for a in registry.list_assets(IDENTITY_ID, "background")
           if SCENE_KEY in str(a.get("storage_uri", ""))]
    if not bgs:
        print("未找到教室场景背景")
        return 3
    bg = sorted(bgs, key=lambda x: x.get("created_at") or "")[-1]

    mask_path = os.path.join(WORKSPACE, MASK_ALPHA_REL)
    import numpy as np
    ab = np.array(Image.open(mask_path).convert("L")) > compose.MASK_BINARY_MAX
    rng = compose.allowed_scale_range(ab, HARD_PROTECT)

    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)

    steps: List[Dict[str, Any]] = []
    for s in ZOOM_STEPS:
        out = compose.compose(IDENTITY_ID, HERO_ASSET_ID, spec["mask_asset_id"], bg["id"],
                              out_uri=f"{OUT_DIR_REL}/缩放-{s:.2f}.png",
                              edge_mode="unmix", person_scale=s)
        fname = f"缩放-{s:.2f}.png"
        with open(os.path.join(out_dir, fname), "wb") as f:
            f.write(out["composed_png"])
        fr = out["frame"]
        steps.append({
            "file": f"CS-005/{fname}",
            "scale": s,
            "canvas": f"{fr['canvas_size'][0]}×{fr['canvas_size'][1]}",
            "head": fr["head_margin_px"],
            "diff": [r["max_channel_diff"] for r in out["hard_protect_check"]["regions"]],
        })
        print(f"[缩放 {s:.2f}] 画框 {fr['canvas_size']}  头顶余量 {fr['head_margin_px']}px  "
              f"硬保护区最大通道差 {steps[-1]['diff']}")

    r_last = compose.fit_smaller_frame(open(mask_path, "rb").read(),
                                       HARD_PROTECT, ab.shape[0])["clean_last_row"]

    cards = [
        ("人物B-零断面缩小-同尺寸画框.png", "零断面缩小 · 同尺寸画框",
         "断面正好压在画框下沿 → 看不到；人物占画面高 47.5%", "ok", "无断面"),
        ("人物B-零断面缩小-更大画框.png", "零断面缩小 · 更大画框",
         "画框更高，人物占比进一步下降；背景放大 1.18×", "ok", "无断面"),
        ("人物B-缩小-0.70-露断面-标注断面.png", "整体外扩 0.70× · 露断面",
         "红框处露出源图裁断的断面：人物像纸片贴在场景里", "bad", "断面 3 处"),
    ]
    card_html = ""
    for fn, name, desc, cls, tag in cards:
        if not os.path.exists(os.path.join(out_dir, fn)):
            continue
        card_html += (
            f'<div class="card"><img src="CS-005/{fn}" alt="{html.escape(name)}">'
            f'<div class="meta"><p class="name">{html.escape(name)}'
            f'<span class="tag {cls}">{tag}</span></p>'
            f'<p class="kv">{html.escape(desc)}</p></div></div>')

    page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>人物大小 · 画框式缩放实测</title><style>{CSS}</style></head><body>
<h1>人物大小 · 画框式缩放实测</h1>
<p class="lead">缩放的本质是"人物在画面里占多大"，不是"人物有多少像素"。所以本管线把两者解耦：
人物层<b>永远 1:1 逐像素复制</b>（不重采样、不生成），缩放只改<b>画框</b>——
放大 = 画框内收（裁切），缩小 = 画框外扩（补场景）。
因此硬保护区的断言在缩放前后<b>同样是"差 1 即失败"</b>，身份零漂移的可验证性不因改构图而下降。</p>

<h2>一、放大（可用区间 {rng['min']}× – {rng['max']}×）</h2>
<p class="sub">拖动滑块。画框越小，同一像素内容被放得越大；<b>人物的像素数量全程不变</b>。
上限由素材头顶余量决定——本素材头顶之上只有 127px，所以到 {rng['max']}× 就到顶，再大就要切头（会被硬拒绝）。</p>
<div class="wrap">
  <div class="shot"><img id="shot" src="{steps[0]['file']}" alt="缩放预览"></div>
  <div class="panel">
    <input type="range" id="slider" min="0" max="{len(steps) - 1}" step="1" value="0">
    <div class="row"><span class="k">人物缩放</span><b id="v-scale"></b></div>
    <div class="row"><span class="k">画框（输出分辨率）</span><b id="v-canvas"></b></div>
    <div class="row"><span class="k">头顶到画框上沿</span><b id="v-head"></b></div>
    <div class="row"><span class="k">硬保护区最大通道差</span><b id="v-diff"></b></div>
    <div class="hint">人脸/头发所在区域逐像素取自源图，通道差恒为 0——这是"身份零漂移"的
    可验证依据，与缩放档位无关。缩放只是把画框收窄，人没被重画。</div>
  </div>
</div>

<h2>二、缩小：两条路线的代价完全不同</h2>
<p class="sub">源图是"胸像特写"：人物躯干在左、右、下三条边都被源图裁断了（实测左边界自 y=951 起、
右边界自 y=921 起、下边界整行 941 像素贴着人物）。所以<b>整体外扩画框必然露出断面</b>，
默认被硬拒绝。右下这条是显式覆盖后的对照实验，只为看清成因，不作为生产路径。</p>
<div class="grid">{card_html}</div>
<div class="hint" style="max-width:820px;margin-top:16px">零断面构图的做法：把画框<b>下移</b>，
让人物在画框下沿被裁断（断面正好落在画框边缘 → 不可见），上半部分交给场景。
人物像素仍是 1:1 逐像素复制，硬保护区通道差同样恒为 0。
代价如实记录：人物被挤到画面下半部，人脸位置比常规口播低；想让人物既小又居中，
正解是换一张更宽构图的主图（人物四周要有真实像素），而不是把特写往外放。</div>

<h2>三、边界（由素材几何自动求得，非拍脑袋）</h2>
<div class="panel" style="max-width:820px">
  <div class="row"><span class="k">可用缩放区间（含 1.0 的连续段）</span><b>{rng['min']}× – {rng['max']}×</b></div>
  <div class="row"><span class="k">放大上限的成因</span><b>头顶之上仅 127px（需留 {compose.MIN_HEAD_MARGIN}px 余量）</b></div>
  <div class="row"><span class="k">缩小下限的成因</span><b>躯干贴住左/右/下三边，外扩即露断面</b></div>
  <div class="row"><span class="k">零断面构图可用的最长可见行</span><b>源图第 {r_last} 行</b></div>
  <div class="row"><span class="k">人物像素是否被重采样</span><b>否（1:1 复制，preservation_strength=bit_exact）</b></div>
</div>

<div class="foot">
生成脚本：<code>production/scripts/build_zoom_preview.py</code> ·
实测数据：<code>production/scripts/compose_zoom_variants.py</code> →
<code>experiments/抠图合成验证/CS-005/人物大小实测报告.json</code><br>
所有产物均为<b>候选</b>：mask 为自动草稿，未经人工目视确认（<code>compose.confirm_mask</code>）前不得定版进入生产。
</div>
<script>
const STEPS = {json.dumps(steps, ensure_ascii=False)};
const img = document.getElementById('shot'), sl = document.getElementById('slider');
function render() {{
  const s = STEPS[+sl.value];
  img.src = s.file;
  document.getElementById('v-scale').textContent = s.scale.toFixed(2) + '×';
  document.getElementById('v-canvas').textContent = s.canvas + ' px';
  document.getElementById('v-head').textContent = s.head + ' px';
  document.getElementById('v-diff').textContent = JSON.stringify(s.diff);
}}
sl.addEventListener('input', render); render();
</script>
</body></html>"""

    page_path = os.path.join(WORKSPACE, PAGE_REL)
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"\n预览页：{PAGE_REL}")
    print(f"缩放档位图 {len(steps)} 张 → {OUT_DIR_REL}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
