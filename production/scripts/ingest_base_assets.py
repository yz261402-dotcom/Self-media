"""把工作区已有的真实素材受控登记进资产库（T03）。

数据来源：
  assets/固定主播/主播B/版本1/角色档案.json     身份、来源、审批范围
  assets/固定主播/主播B/版本1/校验摘要.json     原图与档案的 SHA-256

原则：
  - 只登记真实存在且哈希可核验的文件，不生成、不补造素材；
  - 身份审核结论按档案实际范围登记（当前仅 identity_choice_only，故为 review 而非 pass）；
  - 权属按档案的 license_review_state 登记，pending 即 unverified，生产准入会被阻塞；
  - 重复执行安全：已冻结的身份不重复登记。

用法：
  .venv/Scripts/python.exe scripts/ingest_base_assets.py
  .venv/Scripts/python.exe scripts/ingest_base_assets.py --db runtime/studio.db --dry-run
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

PROFILE_REL = "assets/固定主播/主播B/版本1/角色档案.json"
DIGEST_REL = "assets/固定主播/主播B/版本1/校验摘要.json"
HERO_REL = "assets/固定主播/主播B/版本1/基础人物.png"
VOICE_REL = "assets/固定主播/主播B/版本1/音色3-温暖闺蜜试听.wav"


def load_json(rel: str) -> dict:
    with io.open(os.path.join(WORKSPACE, rel), encoding="utf-8") as f:
        return json.load(f)


def sha256_of(rel: str) -> str:
    p = os.path.join(WORKSPACE, rel)
    if not os.path.exists(p):
        return ""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


TODO_LINES = [
    "参考图 ≥4 张：需经 gpt-image-2 参考图编辑（同参考图）生成并逐张审核",
    "6 组服装/背景变体：需真实产出 + 权利清单",
    "动作母版：experiments/MuseTalk部署/MT-001 动作母版首片.mp4 待权利核验后登记",
    "权属核验：gpt-image-2 中转服务条款、检测模型权重许可（InsightFace 权重）",
]


def print_todo() -> None:
    print("\n待补（不伪造，缺一项就阻塞一项）：")
    for line in TODO_LINES:
        print(f"  · {line}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="SQLite 路径，默认 runtime/studio.db")
    ap.add_argument("--dry-run", action="store_true", help="只核验与打印，不写库")
    args = ap.parse_args()

    if args.db:
        os.environ["AVATAR_STUDIO_DB"] = os.path.abspath(args.db)

    profile = load_json(PROFILE_REL)
    digests = load_json(DIGEST_REL)["files"]

    identity_id = profile["avatar_identity"]["id"]
    identity_digest = profile["avatar_identity"]["identity_digest"]
    print(f"[入链] 人物 {identity_id} / {profile['avatar_identity']['name']}")

    problems = []
    hero_hash = sha256_of(HERO_REL)
    if not hero_hash:
        problems.append(f"原图不存在：{HERO_REL}")
    elif hero_hash != digests.get(HERO_REL):
        problems.append(f"原图哈希与校验摘要不符：{hero_hash} != {digests.get(HERO_REL)}")
    else:
        print(f"  ✓ 原图哈希一致 {hero_hash[:16]}…")

    if hero_hash and hero_hash != identity_digest:
        print(f"  注：档案身份基准 {identity_digest[:16]}… 与文件哈希不同，以文件哈希为准登记")

    voice_hash = sha256_of(VOICE_REL)
    if voice_hash and voice_hash != digests.get(VOICE_REL):
        problems.append("音色试听哈希与校验摘要不符")
    elif voice_hash:
        print(f"  ✓ 音色试听哈希一致 {voice_hash[:16]}…（音色资产在 T04 登记）")

    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        print("核验未通过，已中止，不写入任何数据。")
        return 2

    if args.dry_run:
        print("dry-run：核验通过，未写库。")
        return 0

    from app import storage as st
    from app.avatar import licensing, registry

    st.init_db()

    existing = registry.get_identity(identity_id)
    if existing and existing.get("frozen"):
        ok, reasons = registry.production_ready(identity_id)
        print(f"  身份已冻结，跳过登记。生产准入：{'通过' if ok else '未通过'}")
        for r in reasons:
            print(f"    - {r}")
        print_todo()
        return 0

    src = next((s for s in profile.get("source_records", []) if s.get("id") == "source_av001_image_b"), {})

    # 幂等：已有权属/资产/审核记录时复用，不重复登记
    prior_lic = next((r for r in licensing.list_records()
                      if r.get("subject", "").startswith("人物B 主参考图来源")), None)
    if prior_lic:
        image_lic = prior_lic
        print(f"  权属记录复用：{image_lic['id']}（{image_lic['scope']}）")
    else:
        image_lic = licensing.register(
            subject="人物B 主参考图来源（本项目经中转生成）",
            kind="image_source",
            scope="unverified",  # 档案 license_review_state=pending，不抬高结论
            evidence=f"provider={src.get('provider')} model={src.get('model')} "
                     f"run={src.get('provider_run_id')} 执行证据={src.get('execution_evidence')}",
            verified_by="",
            note="生成服务条款与商用范围待核验；未核验前不进入生产",
        )
        print(f"  权属记录：{image_lic['id']}（{image_lic['scope']}）")

    if not existing:
        # 档案里的 id 是既定身份标识（av_base_b_v1），保持与契约、前端一致
        st.put("identities", identity_id, {
            "id": identity_id,
            "name": profile["avatar_identity"]["name"],
            "persona": profile["avatar_identity"].get("identity_prompt", ""),
            "hero_asset_id": "",
            "identity_digest": "",
            "default_voice_asset_id": profile.get("voice_asset", {}).get("id"),
            "approval_state": "pending",
            "is_preset": False,
            "note": "由工作区既有素材登记",
            "identity_version": 1,
            "frozen": False,
            "reference_asset_ids": [],
            "license_id": None,
            "base_approved_at": None,
        })
        print(f"  已创建身份记录 {identity_id}（待登记资产与审核）")

    hero = next((a for a in registry.list_assets(identity_id, "hero")
                 if a.get("file_hash") == "sha256:" + hero_hash), None)
    if hero:
        print(f"  hero 资产复用：{hero['id']}（v{hero['version']}）")
    else:
        hero = registry.register_asset(
            identity_id=identity_id,
            kind="hero",
            storage_uri=HERO_REL,
            file_hash="sha256:" + hero_hash,
            source_evidence=f"{src.get('id')} | {src.get('execution_evidence')} | "
                            f"请求摘要 {src.get('request_digest')}",
            license_id=image_lic["id"],
            width=941,
            height=1672,
            note="941×1672 主参考图；版本1",
        )
        print(f"  hero 资产：{hero['id']}（v{hero['version']}）")

    prior_reviews = st.all_rows("identity_reviews", "asset_id=?", [hero["id"]])
    if prior_reviews:
        review = prior_reviews[-1]
        print(f"  审核记录复用：{review['id']}（{review['outcome']}）")
    else:
        review = registry.record_review(
            asset_id=hero["id"],
            reviewer="user",
            outcome="review",  # 档案 approval_scope=identity_choice_only，仅为选择审批
            identity_digest="sha256:" + hero_hash,
            method="登记核验（哈希比对）",
            note="档案 approval_scope=identity_choice_only：仅身份选择审批，"
                 "非身份一致性检测结论，生产前仍需按 AC-AV-02 做对照验收",
        )
        print(f"  审核记录：{review['id']}（{review['outcome']}）")

    ident = registry.freeze_identity(
        identity_id=identity_id,
        hero_asset_id=hero["id"],
        identity_digest="sha256:" + hero_hash,
        reviewer="user",
        license_id=image_lic["id"],
    )
    print(f"  身份已冻结：v{ident['identity_version']}")

    ok, reasons = registry.production_ready(identity_id)
    print(f"\n生产准入：{'通过' if ok else '未通过'}")
    for r in reasons:
        print(f"  - {r}")
    print_todo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
