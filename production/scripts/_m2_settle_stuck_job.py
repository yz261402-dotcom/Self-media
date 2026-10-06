#!/usr/bin/env python3
"""临时运维脚本：把「提交后未取得可核对结果」的 M2 作业对账落定。

背景：`job_b40b6fba530f`（work_fffaa479d92e 的动作底片）在 2026-09-27 22:29 CST
提交时，算力云实例处于关机状态、本机 18080 无监听，`transport()` 2 秒内即抛错，
作业被标成 `unknown`（`create_plate` 的 except 分支）。此后每次重试都会命中
`PLATE_JOB_ALREADY_SUBMITTED`（409），因为系统拒绝在 GPU 上重复排队。

证据（已核对）：
- 提交时刻本机 18080 无监听（隧道进程已死）；
- 算力云 SSH 端口当时被拒（实例未开机）；
- 远端 `/root/autodl-tmp/avatar-m2worker/jobs/` 无 `b40b6fba530f` 记录，
  最近一条作业为 2026-09-27 16:33 CST，早于本作业 6 小时。

结论：请求从未送达 worker，未产生 GPU 作业与费用 → 如实记为 `cancelled`。

一次性运维用，不属于产品代码。默认只做 dry-run 检查，加 `--apply` 才写库。
"""
from __future__ import annotations

import argparse
import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

from app import jobs, storage as st  # noqa: E402

JOB_ID = "job_b40b6fba530f"

EVIDENCE = {
    "source": "运维核对（2026-09-28）：提交时刻本机 18080 无监听、算力云实例 SSH 端口被拒，"
              "请求未送达 worker；远端 jobs/ 目录无本作业记录"
              "（最近一条为 2026-09-27 16:33 CST，早于本作业创建时间 22:29 CST）。"
              "未产生 GPU 作业与费用。",
    "local_tunnel_at_submit": "127.0.0.1:18080 无监听",
    "remote_reachable_at_submit": False,
    "remote_jobs_dir_latest": "2026-09-27 16:33 CST",
    "remote_has_job_record": False,
    "checked_at": "2026-09-28T09:37:00+08:00",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库；默认只检查")
    ns = ap.parse_args()

    print(f"DB_PATH = {st.DB_PATH}")
    if not os.path.exists(st.DB_PATH):
        print("库文件不存在，中止", file=sys.stderr)
        return 2
    job = jobs.get_job(JOB_ID)
    if not job:
        print(f"作业不存在：{JOB_ID}")
        return 1
    print(f"当前状态   = {job.get('state')}")
    print(f"可计费     = {job.get('billable')} / 需结算 = {job.get('settlement_required')}")
    print(f"unknown 原因 = {job.get('unknown_reason')}")
    print(f"幂等键     = {job.get('idempotency_key')}")
    print(f"work_id    = {job.get('work_id')}")
    if job.get("state") != "unknown":
        print("不是待对账状态，无需处理")
        return 0
    if not ns.apply:
        print("\n[dry-run] 未写库。确认无误后加 --apply 执行。")
        return 0
    out = jobs.resolve_unknown(JOB_ID, "cancelled", EVIDENCE)
    print(f"\n已对账：state={out.get('state')} "
          f"settlement_required={out.get('settlement_required')}")
    print(f"reconciliation = {out.get('reconciliation')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
