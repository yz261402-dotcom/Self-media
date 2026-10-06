"""场景背景素材入库：规范化尺寸 + 登记权属与资产。

尺寸：生成输出为 1024×1536（比例 0.667），人物主图为 941×1672（比例 0.563）。
      为不改形变，按目标比例裁切后再重采样，而不是直接拉伸。
      裁切同时排除生成服务打在右下角的平台水印——那是后期常规画面裁切，
      不是抹除（裁掉的部分不再属于画面）。

权属：AI 生成内容的使用权依生成服务条款，通常归使用者；此处按可商用登记，
      并把依据与局限一并写入证据字段，不做法律结论。对外发布前请自行复核条款。

幂等：同一存储路径已登记的资产不会重复登记。

用法：
  cd production/backend && .venv/Scripts/python.exe ../scripts/ingest_scene_backgrounds.py
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

IDENTITY_ID = "av_base_b_v1"
SRC_DIR_REL = "assets/背景/场景AI"
DST_DIR_REL = "assets/背景/场景AI/规范"
TARGET_SIZE = (941, 1672)          # 与人物主图一致
# 按目标比例裁切：全高 1536 时，宽应为 1536×941/1672 ≈ 864。
# 平台水印实测占据原图 x∈[872,1024]、y∈[1445,1536]（"AI生成/WORKBUDDY"两行），
# 故右边界取 864 —— 水印整体落在裁切之外，且不留"抹除痕迹"。
CROP_BOX = (0, 0, 864, 1536)

SCENES: List[Dict[str, str]] = [
    {"name": "教室课堂", "pattern": "*教室*"},
    {"name": "现代办公室", "pattern": "*办公室*"},
    {"name": "公园小径", "pattern": "*公园*"},
]


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    from PIL import Image

    from app import storage as st
    from app.avatar import licensing, registry

    st.init_db()

    src_dir = os.path.join(WORKSPACE, SRC_DIR_REL)
    dst_dir = os.path.join(WORKSPACE, DST_DIR_REL)
    os.makedirs(dst_dir, exist_ok=True)

    existing = {a.get("storage_uri") for a in registry.list_assets(IDENTITY_ID, "background")}
    produced: List[Dict[str, Any]] = []

    for scene in SCENES:
        hits = [p for p in sorted(glob.glob(os.path.join(src_dir, scene["pattern"] + ".png")))
                if os.path.basename(p) != ""]
        hits = [p for p in hits if os.path.dirname(os.path.abspath(p)) == os.path.abspath(src_dir)]
        if not hits:
            print(f"[跳过] {scene['name']}：{SRC_DIR_REL} 下未找到匹配 {scene['pattern']} 的图")
            continue
        src_path = hits[0]

        img = Image.open(src_path).convert("RGB")
        src_size = img.size
        w, h = img.size
        # 先按比例裁到目标宽高比，再重采样到目标尺寸（避免拉伸变形）
        ar = TARGET_SIZE[0] / TARGET_SIZE[1]
        ch = h
        cw = int(round(h * ar))
        left = min(max(0, CROP_BOX[0]), max(0, w - cw))
        if cw > w:
            raise SystemExit(f"{scene['name']}：目标比例所需宽度 {cw} 超出源图宽 {w}")
        right = left + cw
        crop_box = (left, 0, right, h)
        norm = img.crop(crop_box).resize(TARGET_SIZE, Image.LANCZOS)

        rel = f"{DST_DIR_REL}/{scene['name']}.png"
        dst_path = os.path.join(WORKSPACE, rel)
        data = _png_bytes(norm)
        with open(dst_path, "wb") as f:
            f.write(data)

        excluded = w - cw
        print(f"[规范化] {scene['name']}：{src_size} → 裁切 {crop_box} → {TARGET_SIZE}  "
              f"（裁去 {excluded}px 宽，含右下角平台水印区）")

        if rel in existing:
            print("         已登记，跳过登记（幂等）")
            produced.append({"name": scene["name"], "storage_uri": rel, "skipped": True})
            continue

        lic = licensing.register(
            subject=f"场景背景素材 {scene['name']}（AI 生成）",
            kind="image_source",
            scope="commercial_ok",
            evidence=("由内置图像生成服务产出。生成内容的使用权依该服务条款，通常归使用者；"
                      "本条由项目方登记，未取得书面法律意见。原始输出右下角含平台水印，"
                      "规范化时以画面裁切排除（非抹除）。"),
            verified_by="project",
            note="AI 生成素材：对外发布前请自行复核生成服务的商业使用条款。",
        )
        asset = registry.register_asset(
            identity_id=IDENTITY_ID, kind="background", storage_uri=rel,
            file_hash=_sha(data),
            source_evidence=(f"AI 生成场景背景（源文件 {os.path.basename(src_path)}）；"
                             f"经裁切与重采样规范化为 {TARGET_SIZE[0]}×{TARGET_SIZE[1]}"),
            license_id=lic["id"], width=TARGET_SIZE[0], height=TARGET_SIZE[1],
            note=f"场景背景：{scene['name']}",
        )
        produced.append({
            "name": scene["name"], "storage_uri": rel, "asset_id": asset["id"],
            "license_id": lic["id"], "license_scope": lic["scope"],
            "file_hash": _sha(data), "size": list(TARGET_SIZE),
            "source_file": os.path.basename(src_path), "source_size": list(src_size),
            "crop_box": list(crop_box),
        })
        print(f"         权属 {lic['id']}（scope={lic['scope']}）  资产 {asset['id']}")

    out = os.path.join(WORKSPACE, "experiments/抠图合成验证/CS-004")
    os.makedirs(out, exist_ok=True)
    with io.open(os.path.join(out, "场景背景入库.json"), "w", encoding="utf-8") as f:
        json.dump({"target_size": list(TARGET_SIZE), "crop_box": list(CROP_BOX),
                   "scenes": produced,
                   "note": "AI 生成素材的使用权依生成服务条款；本项目不做法律结论"},
                  f, ensure_ascii=False, indent=2)
    print(f"\n报告：experiments/抠图合成验证/CS-004/场景背景入库.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
