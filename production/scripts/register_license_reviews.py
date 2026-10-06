"""登记权属核验结论（AC-AV-04）。

依据必须可追溯：只记录官方条款原文与来源链接，由用户核验；
本脚本不做法律判断，也不因为"条款写得宽松"就抬高结论。

当前核验项：InsightFace 预训练权重。
官方原文（github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md）：
  "The InsightFace Python library code is released under the MIT License and can be
   used for academic and commercial purposes."
  "The pretrained models provided with this library are available for
   non-commercial research purposes only, including both automatically and
   manually downloaded models."
即：代码 MIT ≠ 权重可商用，权重受独立限制。

用法：
  .venv/Scripts/python.exe scripts/register_license_reviews.py
"""
from __future__ import annotations

import argparse
import os
import sys

PRODUCTION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(PRODUCTION, "backend")
sys.path.insert(0, BACKEND)

MODEL_ZOO_URL = ("https://github.com/deepinsight/insightface/blob/master/"
                 "python-package/docs/model_zoo.md")
OFFICIAL_SITE = "https://www.insightface.ai/"

INSIGHTFACE_WEIGHTS = {
    "subject": "InsightFace 预训练权重（buffalo_l / buffalo_m / buffalo_s / buffalo_sc / antelopev2 / raccoon_s / raccoon_l）",
    "kind": "model_weights",
    "scope": "blocked",
    "evidence": (
        f"官方条款 {MODEL_ZOO_URL} ：\"The pretrained models provided with this library "
        "are available for non-commercial research purposes only, including both "
        "automatically and manually downloaded models.\" "
        f"官方站点 {OFFICIAL_SITE} 亦载明预训练模型仅供非商业学术研究，"
        "模型商用需单独取得商业授权；自托管 Server 不授予模型商用权利。"
    ),
    "covers_weights": True,
    "note": (
        "结论：本项目生产路径不得使用这些公共权重做身份检测。"
        "注意区分——Python 库代码为 MIT（可商用），权重另有许可，二者不可混同。"
        "解禁条件（满足任一后重新登记并改判）：① 取得 InsightFace 商业授权；"
        "② 使用自训练/自有权重；③ 改用许可明确可商用的检测服务。"
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    if args.db:
        os.environ["AVATAR_STUDIO_DB"] = os.path.abspath(args.db)

    from app import storage as st
    from app.avatar import licensing

    st.init_db()

    existing = next((r for r in licensing.list_records()
                     if r.get("subject", "").startswith("InsightFace 预训练权重")), None)
    if existing:
        print(f"已登记，跳过：{existing['id']}（{existing['scope']}）")
    else:
        rec = licensing.register(
            subject=INSIGHTFACE_WEIGHTS["subject"],
            kind=INSIGHTFACE_WEIGHTS["kind"],
            scope=INSIGHTFACE_WEIGHTS["scope"],
            evidence=INSIGHTFACE_WEIGHTS["evidence"],
            verified_by="user（依据官方条款核验）",
            covers_weights=INSIGHTFACE_WEIGHTS["covers_weights"],
            note=INSIGHTFACE_WEIGHTS["note"],
        )
        print(f"已登记：{rec['id']}（{rec['scope']}）")

    # 启用检测 Provider 前会被这条阻塞
    try:
        licensing.require_weights("InsightFace 预训练权重（buffalo_l / buffalo_m / buffalo_s / buffalo_sc / antelopev2 / raccoon_s / raccoon_l）")
        print("检测 Provider 权重许可：通过（不应出现）")
    except licensing.LicenseBlocked as e:
        print(f"检测 Provider 启用检查：{e.code} — {e.message}")

    print("\n可替代方案（择一，选定后再登记）：")
    print("  · 商业人脸比对 API（云厂商，按量计费，许可明确）")
    print("  · 自训练 / 自有权重（许可自持）")
    print("  · 取得 InsightFace 商业授权（buffalo_l / antelopev2 等）")
    print("  · 暂不做自动检测：按 AC-AV-02 人工关键帧对照，检测项标 not_run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
