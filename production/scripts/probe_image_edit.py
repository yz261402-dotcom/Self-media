"""探针：验证 gpt-image-2 中转是否支持"同参考图编辑"（T03）。

背景：人物 B 主图本就是该中转生成的（已验证文生图），但档案里
`reference_edit_verified: false`——图生图编辑尚未验证。
本探针只做 1 次最小调用，先确认接口与身份保持能力，再决定是否放量。

产物落在 experiments/图像编辑验证/IE-001/，含请求摘要与执行证据；
密钥只从环境变量或密钥文件读取，不写日志、不写产物。

用法：
  set AVATAR_STUDIO_SECRETS_FILE=C:\\Users\\22346\\Desktop\\密钥\\api_keys.md
  .venv/Scripts/python.exe scripts/probe_image_edit.py [--prompt "..."] [--out-name x.png]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
from typing import Any, Dict, Optional

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

HERO_REL = "assets/固定主播/主播B/版本1/基础人物.png"
OUT_DIR_REL = "experiments/图像编辑验证/IE-001"

DEFAULT_PROMPT = (
    "保持该人物的身份特征完全不变：脸型、五官、发型、肤色、年龄感、性别、身体比例。"
    "仅将背景替换为浅灰色摄影棚背景（干净的纯色影棚墙面，无杂物）。"
    "写实摄影风格，半身构图，正面视角，柔和均匀的棚拍光线，颈部与发丝边缘自然。"
)
NEGATIVE_NOTE = "不改变人物长相、不改变服装主色、不添加文字与水印"


def request_digest(payload: Dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--out-name", default="人物B-换背景-浅灰影棚-01.png")
    ap.add_argument("--size", default="1024x1536")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    from app import config

    injected = []
    if not os.environ.get("AVATAR_STUDIO_GPTIMAGE_API_KEY"):
        injected = config.bootstrap_secrets()
    api_key = os.environ.get("AVATAR_STUDIO_GPTIMAGE_API_KEY", "")
    base_url = os.environ.get("AVATAR_STUDIO_GPTIMAGE_BASE_URL", "").rstrip("/")
    model = os.environ.get("AVATAR_STUDIO_GPTIMAGE_MODEL", "gpt-image-2.5-sunburst")

    if injected:
        print(f"[探针] 已从密钥文件注入 {len(injected)} 个变量（内容不打印）")
    if not api_key:
        print("未找到图像编辑密钥：请设置 AVATAR_STUDIO_SECRETS_FILE 或 AVATAR_STUDIO_GPTIMAGE_API_KEY")
        return 3
    if not base_url:
        print("未找到中转 base_url：请在密钥文件中登记，或设置 AVATAR_STUDIO_GPTIMAGE_BASE_URL")
        return 3

    hero_path = os.path.join(WORKSPACE, HERO_REL)
    if not os.path.exists(hero_path):
        print(f"参考图不存在：{HERO_REL}")
        return 3
    hero_bytes = open(hero_path, "rb").read()
    hero_hash = hashlib.sha256(hero_bytes).hexdigest()

    out_dir = os.path.join(WORKSPACE, OUT_DIR_REL)
    os.makedirs(out_dir, exist_ok=True)

    payload = {
        "provider": "openai_compatible_relay",
        "endpoint": "/v1/images/edits",
        "model": model,
        "prompt": args.prompt,
        "negative_note": NEGATIVE_NOTE,
        "reference": HERO_REL,
        "reference_sha256": hero_hash,
        "size": args.size,
        "n": 1,
        "base_url_host": base_url.split("//")[-1].split("/")[0],
    }
    digest = request_digest(payload)
    print(f"[探针] 参考图 sha256={hero_hash[:16]}… 请求摘要={digest[:16]}…")

    import httpx

    url = f"{base_url}/v1/images/edits"
    files = {"image": ("hero.png", hero_bytes, "image/png")}
    data = {"model": model, "prompt": f"{args.prompt}。{NEGATIVE_NOTE}", "size": args.size, "n": "1"}
    headers = {"Authorization": f"Bearer {api_key}"}

    result: Dict[str, Any] = {"request": payload, "request_digest": digest}
    try:
        with httpx.Client(timeout=args.timeout) as client:
            resp = client.post(url, headers=headers, files=files, data=data)
    except Exception as exc:  # 网络故障与超时归为同一类，不重试计费动作
        result.update({"outcome": "network_error", "detail": type(exc).__name__})
        _write(out_dir, result, None)
        print(f"网络故障 / 超时：{type(exc).__name__}（未获结论，不重试）")
        return 4

    result["http_status"] = resp.status_code
    body: Dict[str, Any] = {}
    try:
        body = resp.json()
    except Exception:
        body = {"raw_head": resp.text[:300]}

    if resp.status_code != 200:
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        result.update({
            "outcome": "failed",
            "error_code": err.get("code") or body.get("code") or "",
            "error_message": (err.get("message") or body.get("message") or resp.text[:200]),
        })
        _write(out_dir, result, None)
        print(f"调用失败 HTTP {resp.status_code}：{result['error_message'][:200]}")
        print("结论：该中转未验证支持同参考图编辑，保持 reference_edit_verified=false")
        return 5

    items = body.get("data") or []
    if not items:
        result.update({"outcome": "failed", "error_message": "响应无 data 数组"})
        _write(out_dir, result, None)
        print("响应无图片数据")
        return 5

    item = items[0]
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
        _write(out_dir, result, None)
        print("未能取得图片字节")
        return 5

    out_path = os.path.join(out_dir, args.out_name)
    with open(out_path, "wb") as f:
        f.write(img_bytes)
    out_hash = hashlib.sha256(img_bytes).hexdigest()

    result.update({
        "outcome": "succeeded",
        "output_file": f"{OUT_DIR_REL}/{args.out_name}",
        "output_sha256": out_hash,
        "output_bytes": len(img_bytes),
        "usage": body.get("usage") or {},
        "response_id": body.get("id") or body.get("created"),
        "identity_preserved": "待人工对照：需与参考图逐项比对脸型/五官/发型/肤色/年龄感",
        "reference_edit_verified": "接口可用；身份一致性尚未检测，需人工或标定检测确认后才置 true",
    })
    _write(out_dir, result, img_bytes)
    print(f"成功：{OUT_DIR_REL}/{args.out_name}（{len(img_bytes)} 字节，sha256={out_hash[:16]}…）")
    print("下一步：人工对照参考图核验身份是否保持；确认后再批量生成其余参考图与变体。")
    return 0


def _write(out_dir: str, result: Dict[str, Any], img: Optional[bytes]) -> None:
    with io.open(os.path.join(out_dir, "执行结果.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
