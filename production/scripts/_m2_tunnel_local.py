#!/usr/bin/env python3
"""启动 M2 隧道，但**不读** `~/.ssh/known_hosts`。

为什么需要这个包装器：项目脚本 `m2_worker_tunnel.py` 的 `connect()` 会调用
`load_system_host_keys()` 并读取 `~/.ssh/known_hosts`，而本机沙箱拒绝该文件读取，
导致隧道起不来（`PermissionError: [Errno 13]`）。

安全语义不变：原脚本本来就设了 `AutoAddPolicy`（接受未知主机），
不读 known_hosts 只是少加载一份"已知主机"列表，不改变信任策略。

用法与项目脚本一致：`M2_SSH_PASSWORD=... python _m2_tunnel_local.py --local-port 18080`
一次性运维用，不属于产品代码。
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import m2_worker_tunnel as t  # noqa: E402
import paramiko  # noqa: E402


def connect_no_known_hosts(*, host: str, port: int, user: str, password: str):
    last = None
    for attempt in range(6):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # 刻意不调 load_system_host_keys()/load_host_keys()：沙箱拒绝读 ~/.ssh/known_hosts
        try:
            client.connect(host, port=port, username=user, password=password,
                           timeout=25, auth_timeout=25, banner_timeout=25,
                           look_for_keys=False, allow_agent=False)
            client.get_transport().set_keepalive(20)
            print(f"SSH 已连接 {user}@{host}:{port}", flush=True)
            return client
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"连接第 {attempt + 1} 次失败：{type(e).__name__}: {e}", flush=True)
            time.sleep(8)
    raise t.TunnelError(f"SSH 连接重试后仍失败：{type(last).__name__}: {last}")


t.connect = connect_no_known_hosts

if __name__ == "__main__":
    raise SystemExit(t.main())
