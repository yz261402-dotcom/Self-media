"""把 AI 生成的主图补挂回身份。

背景：`generate_candidate_hero` 早期版本只登记了 hero 资产，没有写回
`identities.hero_asset_id` / `identities.license_id`，导致这些资产成为孤儿——
`production_ready` 报"缺少定妆原片资产"，冻结时也要靠人手动补 id。
根因已在 `person_image.generate_candidate_hero` 修掉，本脚本只修存量数据。

用法：
    AVATAR_STUDIO_DB=<studio.db> python scripts/repair_generated_hero_links.py [--apply]

不带 --apply 时只打印将要改动的内容，不落库。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app import storage as st  # noqa: E402
from app.avatar import registry  # noqa: E402


def find_orphans() -> list:
    """找出「身份没有主图、但名下确实躺着 hero 资产」的记录。"""
    orphans = []
    for ident in st.all_rows("identities"):
        if ident.get("hero_asset_id"):
            continue
        heroes = [a for a in registry.list_assets(ident["id"], "hero")
                  if a.get("storage_uri") and a.get("file_hash")]
        if not heroes:
            continue
        newest = sorted(heroes, key=lambda a: a.get("created_at", ""))[-1]
        orphans.append((ident, newest, len(heroes)))
    return orphans


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写入，默认只预览")
    args = ap.parse_args()

    orphans = find_orphans()
    if not orphans:
        print("没有需要补挂的身份，数据已是最新形态。")
        return 0

    for ident, hero, total in orphans:
        print(f"{ident['id']}  {ident.get('name')}")
        print(f"   hero_asset_id  : (空) -> {hero['id']}")
        print(f"   license_id     : {ident.get('license_id')!r} -> {hero.get('license_id')!r}")
        if total > 1:
            print(f"   注意：名下共 {total} 张 hero 资产，取最新的一张")
        if args.apply:
            ident["hero_asset_id"] = hero["id"]
            ident["license_id"] = hero.get("license_id")
            ident["note"] = (
                "补齐主图挂载（修复脚本：此前生成的主图未挂回身份）；"
                "主图为 AI 生成，仍待人工身份审核后定版"
            )
            st.put("identities", ident["id"], ident)
    print()
    print(f"共 {len(orphans)} 条" + ("已修复。" if args.apply else "待修复（加 --apply 落库）。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
