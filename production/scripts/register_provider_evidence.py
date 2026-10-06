"""登记供应商实测证据（连接检查只认真实证据，不认"配置项存在"）。

证据来源：
  · AV-001 人物 B 主图 → gpt-image-2 / text_to_image 可用
  · IE-001、IE-002 探针 → gpt-image-2 / image_edit 可用（含 mask 不被采用的实测结论）

用法：
  .venv/Scripts/python.exe scripts/register_provider_evidence.py
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

PROFILE_REL = "assets/固定主播/主播B/版本1/角色档案.json"
IE001_REL = "experiments/图像编辑验证/IE-001/执行结果.json"
IE002_REL = "experiments/图像编辑验证/IE-002/执行结果.json"


def load(rel: str) -> dict:
    p = os.path.join(WORKSPACE, rel)
    if not os.path.exists(p):
        return {}
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    if args.db:
        os.environ["AVATAR_STUDIO_DB"] = os.path.abspath(args.db)

    from app import config, storage as st

    st.init_db()

    profile = load(PROFILE_REL)
    src = next((s for s in profile.get("source_records", [])
                if s.get("model") == "gpt-image-2"), {})

    existing = {(r.get("provider"), r.get("capability"), r.get("outcome"))
                for r in config.provider_evidence("gpt-image-2")}

    records = []

    if src and ("gpt-image-2", "text_to_image", "succeeded") not in existing:
        records.append(config.record_provider_evidence(
            provider="gpt-image-2", capability="text_to_image", outcome="succeeded",
            evidence=f"AV-001 人物B主图 {src.get('source_file')}",
            detail=f"provider={src.get('provider')} run={src.get('provider_run_id')} "
                   f"请求摘要={str(src.get('request_digest'))[:16]}…",
        ))

    ie1 = load(IE001_REL)
    if ie1.get("outcome") == "succeeded" and ("gpt-image-2", "image_edit", "succeeded") not in existing:
        records.append(config.record_provider_evidence(
            provider="gpt-image-2", capability="image_edit", outcome="succeeded",
            evidence=f"IE-001 {ie1.get('output_file')}（同参考图编辑，返回图片字节）",
            detail="接口可用且支持自定义尺寸；但产出经人工对照判定身份漂移，"
                   "产出可用性由资产审核门禁负责，能力可用不等于产出可用",
        ))

    ie2 = load(IE002_REL)
    if ie2.get("outcome") == "succeeded":
        records.append(config.record_provider_evidence(
            provider="gpt-image-2", capability="image_edit_local",
            outcome="unsupported",
            evidence=f"IE-002 {ie2.get('output_file')}",
            detail="实测该中转忽略 mask 参数：同尺寸下人脸区仍有明显改动"
                   f"（平均差 {ie2.get('face_region_diff', {}).get('mean_abs_diff')}，"
                   f"峰值 {ie2.get('face_region_diff', {}).get('max_channel_diff')}），"
                   "不支持「只改背景、人脸逐像素保留」的局部编辑",
        ))

    for r in records:
        print(f"已登记：{r['provider']}/{r['capability']} → {r['outcome']}")
    if not records:
        print("无新增证据（已登记过）")

    print("\n当前供应商状态：")
    for s in config.provider_states():
        print(f"  {s['provider']:12s} {s['state']:24s} key_present={s['api_key_present']}  {s['detail'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
