#!/usr/bin/env python3
"""临时运维脚本：在算力云上执行任意一次性命令（用于核对/取证）。

密码从 `M2_SSH_PASSWORD` 读取。一次性运维用，不属于产品代码。
"""
from __future__ import annotations

import os
import sys
import time

import paramiko

HOST = os.environ.get("M2_SSH_HOST", "connect.weste.seetacloud.com")
PORT = int(os.environ.get("M2_SSH_PORT", "21170"))
USER = os.environ.get("M2_SSH_USER", "root")


def main() -> int:
    pw = os.environ.get("M2_SSH_PASSWORD", "").strip()
    if not pw:
        print("缺少 M2_SSH_PASSWORD", file=sys.stderr)
        return 2
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    last = None
    for attempt in range(6):
        try:
            c.connect(HOST, port=PORT, username=USER, password=pw,
                      timeout=25, auth_timeout=25, banner_timeout=25,
                      look_for_keys=False, allow_agent=False)
            last = None
            break
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"连接第 {attempt + 1} 次失败：{type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(8)
    if last is not None:
        print(f"SSH 连接失败：{type(last).__name__}: {last}", file=sys.stderr)
        return 2

    cmd = os.environ.get("M2_REMOTE_CMD", "echo '(未提供 M2_REMOTE_CMD)'")
    _in, out, err = c.exec_command(cmd, timeout=120)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    print(o.strip() or "(空)")
    if e.strip():
        print("stderr:", e.strip()[:800])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
