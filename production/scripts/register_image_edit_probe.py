"""把参考图编辑探针的产出登记为候选资产并记录核验结论（T03）。

链路：探针产出 → 候选资产（带哈希与来源）→ 身份核验结论。
结论不外推：接口可用 ≠ 身份保持达标；本次核验按 AC-AV-02 判 fail，
因此 reference_edit_verified 仍不得置 true。

用法：
  .venv/Scripts/python.exe scripts/register_image_edit_probe.py
  .venv/Scripts/python.exe scripts/register_image_edit_probe.py --outcome review
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
WORKSPACE = os.path.dirname(PRODUCTION)
sys.path.insert(0, BACKEND)

IDENTITY_ID = "av_base_b_v1"
PROBE_DIR_REL = "experiments/图像编辑验证/IE-001"
RESULT_REL = f"{PROBE_DIR_REL}/执行结果.json"

# 人工对照结论：可定位到具体特征，不做笼统判断
FINDINGS = [
    {"feature": "脸型/下颌线", "reference": "偏圆润，下颌柔和", "probe": "明显收窄、下巴更尖", "verdict": "漂移"},
    {"feature": "眼部", "reference": "双眼皮较窄、眼型偏平", "probe": "双眼皮明显放大、眼妆偏浓", "verdict": "漂移"},
    {"feature": "唇形", "reference": "自然、唇色偏淡", "probe": "更饱满、唇色明显加深", "verdict": "漂移"},
    {"feature": "发型", "reference": "黑色长直发，中分偏左", "probe": "一致", "verdict": "保持"},
    {"feature": "服装", "reference": "米白西装 + 浅蓝圆领上衣", "probe": "一致", "verdict": "保持"},
    {"feature": "背景", "reference": "浅灰摄影棚", "probe": "一致", "verdict": "保持"},
]


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outcome", default="fail", choices=["fail", "review", "pass"])
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    if args.db:
        os.environ["AVATAR_STUDIO_DB"] = os.path.abspath(args.db)

    result_path = os.path.join(WORKSPACE, RESULT_REL)
    if not os.path.exists(result_path):
        print(f"未找到探针结果：{RESULT_REL}（先运行 probe_image_edit.py）")
        return 3
    with io.open(result_path, encoding="utf-8") as f:
        report = json.load(f)

    out_rel = report.get("output_file", "")
    out_path = os.path.join(WORKSPACE, out_rel)
    if not out_rel or not os.path.exists(out_path):
        print("探针未产出图片，无可登记内容")
        return 3

    actual_hash = sha256_of(out_path)
    if report.get("output_sha256") and actual_hash != report["output_sha256"]:
        print("产出文件哈希与记录不符，拒绝登记")
        return 4

    drifted = [f for f in FINDINGS if f["verdict"] == "漂移"]
    note = ("探针核验（人工对照，无自动检测：检测权重许可未核验）："
            + "；".join(f"{f['feature']} {f['reference']} → {f['probe']}" for f in drifted))

    from app import storage as st
    from app.avatar import licensing, registry

    st.init_db()

    lic = next((r for r in licensing.list_records()
                if r.get("subject", "").startswith("人物B 主参考图来源")), None)
    lic_id = lic["id"] if lic else None

    prior = next((a for a in registry.list_assets(IDENTITY_ID, "reference")
                  if a.get("file_hash") == "sha256:" + actual_hash), None)
    if prior:
        asset = prior
        print(f"资产已登记，复用：{asset['id']}")
    else:
        asset = registry.register_asset(
            identity_id=IDENTITY_ID,
            kind="reference",
            storage_uri=out_rel,
            file_hash="sha256:" + actual_hash,
            source_evidence=f"探针 IE-001 | 请求摘要 {report.get('request_digest')} | "
                            f"model={report.get('request', {}).get('model')} | "
                            f"endpoint={report.get('request', {}).get('endpoint')}",
            license_id=lic_id,
            note="换背景探针产出（同参考图编辑），非最终参考集成员",
        )
        print(f"资产已登记：{asset['id']}（v{asset['version']}）")

    review = registry.record_review(
        asset_id=asset["id"],
        reviewer="user（人工对照）",
        outcome=args.outcome,
        identity_digest=report.get("request", {}).get("reference_sha256", "") and
                        "sha256:" + report["request"]["reference_sha256"],
        method="人工对照（自动检测不可用：InsightFace 权重许可未核验）",
        note=note,
    )
    print(f"审核已记录：{review['id']}（{review['outcome']}）")

    # 结论写回探针目录，作为可追溯证据
    report["identity_verification"] = {
        "method": "人工对照",
        "findings": FINDINGS,
        "outcome": args.outcome,
        "asset_id": asset["id"],
        "review_id": review["id"],
        "reference_edit_verified": False,
        "conclusion": "接口可用（可返回同参考图编辑结果），但本次产出的脸型/眼部/唇形发生漂移，"
                      "按 AC-AV-02 判定身份不一致，故 reference_edit_verified 不得置 true；"
                      "需改进身份保持手段后重测。",
    }
    with io.open(result_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    ok, reasons = registry.production_ready(IDENTITY_ID)
    print(f"\n生产准入：{'通过' if ok else '未通过'}")
    for r in reasons:
        print(f"  - {r}")
    print(f"\nreference_edit_verified 保持 false（结论已写入 {RESULT_REL}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
