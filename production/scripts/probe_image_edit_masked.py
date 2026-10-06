"""探针（mask 版）：只改背景，人物区域逐像素保留（T03 身份漂移的改进验证）。

上一次全图重绘的探针（IE-001）出现了身份漂移。本探针改用局部编辑：
mask 的透明区为可编辑区（浅灰背景），人物区域不透明（保留）。
验证指标是**可量化的像素差**，不靠主观描述：
  · 人脸核心区差异应接近 0（若 mask 生效）
  · 背景区差异应明显（说明确实发生了编辑）

用法：
  set AVATAR_STUDIO_SECRETS_FILE=C:\\Users\\22346\\Desktop\\密钥\\api_keys.md
  .venv/Scripts/python.exe scripts/probe_image_edit_masked.py
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

HERO_REL = "assets/固定主播/主播B/版本1/基础人物.png"
OUT_DIR_REL = "experiments/图像编辑验证/IE-002"

PROMPT = ("Replace only the background with a plain light-gray studio wall, soft even lighting. "
          "Keep the person exactly as-is: same face, same hair, same clothing, same pose.")

# 人物保留区（941×1672 坐标系下的粗略包围）：头部椭圆 + 躯干矩形
HEAD_BOX = (215, 20, 645, 700)
TORSO_BOX = (30, 560, 830, 1672)
# 人脸核心区：仅用于量化比对
FACE_BOX = (300, 180, 570, 520)


def _diff_stats(a: "Any", b: "Any", box: Tuple[int, int, int, int]) -> Dict[str, float]:
    from PIL import ImageChops, ImageStat

    ca, cb = a.crop(box), b.crop(box)
    diff = ImageChops.difference(ca.convert("RGB"), cb.convert("RGB"))
    stat = ImageStat.Stat(diff)
    mean = sum(stat.mean) / len(stat.mean)
    extrema = diff.getextrema()
    max_diff = max(e[1] for e in extrema)
    return {"mean_abs_diff": round(mean, 3), "max_channel_diff": int(max_diff)}


def main() -> int:
    ap = argparse.ArgumentParser()
    # 关键：默认与参考图同尺寸。若请求的宽高比与参考图不同，模型会重构构图，
    # 人脸必然变形——那不是身份漂移，而是尺寸不匹配造成的假信号。
    ap.add_argument("--size", default="")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out-name", default="人物B-仅换背景-mask-01.png")
    args = ap.parse_args()

    from PIL import Image, ImageDraw

    from app import config

    injected = []
    if not os.environ.get("AVATAR_STUDIO_GPTIMAGE_API_KEY"):
        injected = config.bootstrap_secrets()
    api_key = os.environ.get("AVATAR_STUDIO_GPTIMAGE_API_KEY", "")
    base_url = os.environ.get("AVATAR_STUDIO_GPTIMAGE_BASE_URL", "").rstrip("/")
    model = os.environ.get("AVATAR_STUDIO_GPTIMAGE_MODEL", "gpt-image-2.5-sunburst")
    if injected:
        print(f"[探针] 已注入 {len(injected)} 个变量（内容不打印）")
    if not api_key or not base_url:
        print("缺少密钥或 base_url：请设置 AVATAR_STUDIO_SECRETS_FILE")
        return 3

    hero_path = os.path.join(WORKSPACE, HERO_REL)
    hero = Image.open(hero_path).convert("RGBA")
    w, h = hero.size
    hero_bytes = open(hero_path, "rb").read()
    hero_hash = hashlib.sha256(hero_bytes).hexdigest()

    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)
    req_size = args.size or f"{w}x{h}"
    ref_ratio = w / h
    rw, rh = (int(x) for x in req_size.split("x"))
    ratio_matched = abs((rw / rh) - ref_ratio) < 0.01
    print(f"[探针] 参考图 {w}×{h}（宽高比 {ref_ratio:.4f}），请求 {req_size}"
          f"{'（等比）' if ratio_matched else '（宽高比不一致，人脸可能因重构而变形）'}")

    # mask：透明=可编辑（背景），不透明=保留（人物）
    mask = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(mask)
    d.ellipse(HEAD_BOX, fill=(0, 0, 0, 255))
    d.rectangle(TORSO_BOX, fill=(0, 0, 0, 255))
    mask_path = os.path.join(out_dir, "mask-人物保留区.png")
    mask.save(mask_path)

    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    mask_bytes = buf.getvalue()

    payload = {
        "provider": "openai_compatible_relay",
        "endpoint": "/v1/images/edits",
        "model": model,
        "prompt": PROMPT,
        "reference": HERO_REL,
        "reference_sha256": hero_hash,
        "mask": f"{OUT_DIR_REL}/mask-人物保留区.png",
        "mask_sha256": hashlib.sha256(mask_bytes).hexdigest(),
        "size": req_size,
        "size_ratio_matched": ratio_matched,
        "mode": "masked_edit",
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True)
                            .encode("utf-8")).hexdigest()
    print(f"[探针] mask 已生成 {mask.size}，请求摘要={digest[:16]}…")

    import httpx

    files = {
        "image": ("hero.png", hero_bytes, "image/png"),
        "mask": ("mask.png", mask_bytes, "image/png"),
    }
    data = {"model": model, "prompt": PROMPT, "size": args.size, "n": "1"}
    headers = {"Authorization": f"Bearer {api_key}"}

    result: Dict[str, Any] = {"request": payload, "request_digest": digest}
    try:
        with httpx.Client(timeout=args.timeout) as client:
            resp = client.post(f"{base_url}/v1/images/edits", headers=headers,
                               files=files, data=data)
    except Exception as exc:
        result.update({"outcome": "network_error", "detail": type(exc).__name__})
        _write(out_dir, result)
        print(f"网络故障 / 超时：{type(exc).__name__}（不重试）")
        return 4

    result["http_status"] = resp.status_code
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code != 200:
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        msg = err.get("message") or body.get("message") or resp.text[:200]
        result.update({"outcome": "failed", "error_message": msg})
        _write(out_dir, result)
        print(f"调用失败 HTTP {resp.status_code}：{msg[:200]}")
        if "mask" in str(msg).lower():
            print("结论：该中转不支持 mask 参数，局部编辑路线不可用")
        return 5

    items = body.get("data") or []
    item = items[0] if items else {}
    img_bytes: Optional[bytes] = None
    if item.get("b64_json"):
        img_bytes = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        try:
            with httpx.Client(timeout=args.timeout) as client:
                r2 = client.get(item["url"])
            if r2.status_code == 200:
                img_bytes = r2.content
        except Exception as exc:
            result["download_error"] = type(exc).__name__
    if not img_bytes:
        result.update({"outcome": "failed", "error_message": "未能取得图片字节"})
        _write(out_dir, result)
        print("未能取得图片字节")
        return 5

    out_path = os.path.join(out_dir, args.out_name)
    with open(out_path, "wb") as f:
        f.write(img_bytes)

    produced_rgb = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    pw, ph = produced_rgb.size
    size_mismatch = (pw, ph) != (w, h)

    if size_mismatch:
        # 统一到产出尺寸比较：缩放参考图而非缩放产出，避免把重采样差算进指标
        sx, sy = pw / w, ph / h
        base = hero.convert("RGB").resize((pw, ph), Image.LANCZOS)
        face_box = tuple(int(v * (sx if i % 2 == 0 else sy)) for i, v in enumerate(FACE_BOX))
        bg_box = (0, 0, int(120 * sx), int(120 * sy))
    else:
        base = hero.convert("RGB")
        face_box = FACE_BOX
        bg_box = (0, 0, 120, 120)

    face_stats = _diff_stats(base, produced_rgb, face_box)
    bg_stats = _diff_stats(base, produced_rgb, bg_box)

    mask_applied = (not size_mismatch) and face_stats["mean_abs_diff"] <= 2.0
    if size_mismatch:
        verdict = "尺寸不一致：产出被重构，指标不可用于判断身份保持，需等比尺寸重测"
    elif mask_applied:
        verdict = "mask 生效：人脸区域逐像素保留"
    else:
        verdict = "仍重绘人脸：mask 未被采用或该实现忽略 mask"
    result.update({
        "outcome": "succeeded",
        "output_file": f"{OUT_DIR_REL}/{args.out_name}",
        "output_sha256": hashlib.sha256(img_bytes).hexdigest(),
        "output_bytes": len(img_bytes),
        "output_pixel_size": [pw, ph],
        "size_mismatch": size_mismatch,
        "face_region_diff": face_stats,
        "background_region_diff": bg_stats,
        "mask_effective": mask_applied,
        "verdict": verdict,
        "threshold_note": "仅在产出尺寸与参考图一致时，人脸核心区平均绝对像素差 ≤2 才判为 mask 生效",
        "usage": body.get("usage") or {},
    })
    _write(out_dir, result)

    print(f"产出：{OUT_DIR_REL}/{args.out_name}（{pw}×{ph}，{len(img_bytes)} 字节）")
    print(f"人脸核心区差异：{face_stats}")
    print(f"背景区差异：{bg_stats}")
    print(f"判定：{verdict}")
    return 0


def _write(out_dir: str, result: Dict[str, Any]) -> None:
    with io.open(os.path.join(out_dir, "执行结果.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
