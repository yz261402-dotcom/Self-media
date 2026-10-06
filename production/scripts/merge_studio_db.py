"""把一份 studio.db 里正牌库没有的行并入正牌库（只增不改，冲突以正牌库为准）。

为什么需要它：
    `storage.DB_PATH` 取 `AVATAR_STUDIO_DB`，缺省是 `<backend>/runtime/studio.db`。
    一旦启动后端时**在命令行显式传了另一个路径**，它就会拿一个空库跑：
    库里的历史（作品/变体/动作参考）看不见是小事，更麻烦的是
    `provider_evidence` 为空 → `config.check_provider().usable` 全假
    → `resolve_mode()` 从 production 掉回 **mock**
    → 而 `plates.py` / `submission.py` 的 `is_mock = resolve_mode()=="mock"`
    会让底片与口播提交**静默退化成 Mock**（违反"Mock 不得混入生产"这条红线）。
    所以合并数据之外，还要把后端启回缺省库。

用法：
    python scripts/merge_studio_db.py --from <次库> --into <正牌库>            # 预览
    python scripts/merge_studio_db.py --from <次库> --into <正牌库> --apply  # 落库
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

# 只并"以 id 为主键"的表；复合主键表（task_events: task_id+seq）另论，不在本次范围
TABLES = [
    "identities", "avatar_assets", "license_records", "source_records",
    "identity_reviews", "mask_specs", "works", "variants", "audios", "videos",
    "provider_evidence", "provider_runs", "motion_references", "alignment_records",
]


def table_names(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--into", dest="dst", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    src, dst = sqlite3.connect(args.src), sqlite3.connect(args.dst)
    have_src, have_dst = table_names(src), table_names(dst)
    print(f"次库   {args.src}")
    print(f"正牌库 {args.dst}")
    print("=" * 68)

    total = 0
    plan: list[tuple[str, int]] = []
    for t in TABLES:
        if t not in have_src or t not in have_dst:
            continue
        src_ids = {r[0] for r in src.execute(f"SELECT id FROM {t}")}
        dst_ids = {r[0] for r in dst.execute(f"SELECT id FROM {t}")}
        missing = src_ids - dst_ids
        if missing:
            plan.append((t, len(missing)))
            total += len(missing)
        only_conflict = src_ids & dst_ids
        if only_conflict:
            print(f"  {t:20s} 次库独有 {len(missing):3d} / 两库同名 {len(only_conflict):3d}（同名保持正牌库不动）")
        elif missing:
            print(f"  {t:20s} 次库独有 {len(missing):3d}")

    print("-" * 68)
    print(f"合计待并入 {total} 行")
    if not args.apply:
        print("预览模式：加 --apply 才落库")
        return 0

    # ATTACH 后用 INSERT OR IGNORE：同名主键保留正牌库版本，绝不覆盖既有数据
    dst.execute("ATTACH DATABASE ? AS src", [args.src])
    try:
        for t, n in plan:
            dst.execute(f"INSERT OR IGNORE INTO main.{t} SELECT * FROM src.{t}")
            print(f"  并入 {t}: {n} 行")
        dst.commit()
    finally:
        dst.execute("DETACH DATABASE src")
    print("已提交")
    return 0


if __name__ == "__main__":
    sys.exit(main())
