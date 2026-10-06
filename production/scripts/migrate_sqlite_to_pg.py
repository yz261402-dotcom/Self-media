"""一次性迁移：把 SQLite 业务库（runtime/studio.db）灌入 PostgreSQL。

- 先用 sqlite3.backup 取一致性快照（WAL 库不能直接拷文件），再按 rowid 顺序逐表复制，
  使 PG 的 ins_seq 与原插入顺序一致。
- insert-only + ON CONFLICT DO NOTHING：可重复运行，绝不覆盖 PG 里已有的同主键行。
- PG 连接串取自 backend/.env 的 AVATAR_STUDIO_DATABASE_URL（不在命令行传密码）。

用法：backend/.venv/Scripts/python.exe production/scripts/migrate_sqlite_to_pg.py [--src PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402,F401  # 加载 backend/.env
from app import storage as st  # noqa: E402


def snapshot(src: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="studio-snapshot-")
    os.close(fd)
    s, d = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(path)
    s.backup(d)
    s.close()
    d.close()
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(BACKEND / "runtime" / "studio.db"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not os.path.isfile(args.src):
        print(f"源库不存在：{args.src}")
        return 2

    snap = snapshot(args.src)
    src = sqlite3.connect(snap)
    src.row_factory = sqlite3.Row
    st.create_schema()
    pg = st.conn()
    pg_tables = {r["table_name"] for r in pg.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()").fetchall()}

    total = 0
    for (table,) in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if table not in pg_tables:
            print(f"[跳过] {table}：PG 中无此表")
            continue
        cols = [r["name"] for r in src.execute(f"PRAGMA table_info({table})")]
        rows = src.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY rowid").fetchall()
        if args.dry_run:
            print(f"[dry-run] {table}: {len(rows)} 行")
            continue
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) ON CONFLICT DO NOTHING"
        with st.transaction() as c:
            before = c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            c.executemany(sql, [tuple(r) for r in rows])
            after = c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        print(f"{table}: 源 {len(rows)} 行，新增 {after - before}，PG 现有 {after}")
        total += after - before
    src.close()
    os.remove(snap)
    print(f"完成，共新增 {total} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
