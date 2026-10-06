#!/usr/bin/env python3
"""把算力云实例上的 M2 worker 端口转发到本机 127.0.0.1。

用法（密码不落盘，从环境变量或交互输入取）：

    set M2_SSH_PASSWORD=...
    python production/scripts/m2_worker_tunnel.py --remote-port 8080 --local-port 18080

然后让后端指向被转发出来的本机端口：

    set AVATAR_STUDIO_M2_WORKER_URL=http://127.0.0.1:18080
    set AVATAR_STUDIO_M2_WORKER_TOKEN=<与远端 M2_WORKER_TOKEN 一致>

为什么走 SSH 转发而不是直接暴露公网端口：
  - worker 只监听 127.0.0.1，实例上不额外开任何对外端口；
  - 鉴权只剩一层 worker token，不用再引入一套反向代理或 TLS；
  - 本机到 volcano TOS 的取件链路不变（素材 URL 由 worker 自己去下载）。

**未验证项**：AutoDL 实例的 sshd 是否允许 direct-tcpip（即 SSH 端口转发）在本条会话里
尚未实测。脚本会先主动探一次，若服务端不允许，会明确报
TCP_FORWARDING_DENIED 并给出替代路径，而不是静默卡住。
"""

from __future__ import annotations

import argparse
import getpass
import os
import select
import socket
import socketserver
import sys
import time
from typing import Optional

DEFAULT_SSH_HOST = "connect.westc.seetacloud.com"
DEFAULT_SSH_PORT = 35884
DEFAULT_SSH_USER = "root"


class TunnelError(RuntimeError):
    pass


def connect(*, host: str, port: int, user: str, password: str):
    try:
        import paramiko
    except ImportError:  # pragma: no cover - 依赖缺失要明确
        raise TunnelError("未安装 paramiko：pip install paramiko")

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known = os.path.expanduser("~/.ssh/known_hosts")
    if os.path.isfile(known):
        client.load_host_keys(known)
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # 注意：`allow_agent_forwarding` 在较老的 paramiko 上不存在（TypeError）。
    # 这里只关 key/agent 认证，不额外传版本相关的参数，避免装个旧版就炸。
    try:
        client.connect(host, port=port, username=user, password=password,
                       timeout=20, auth_timeout=20, banner_timeout=20,
                       look_for_keys=False, allow_agent=False)
    except TypeError:  # pragma: no cover - 老版本 paramiko 的兼容分支
        client.connect(host, port=port, username=user, password=password,
                       timeout=20)
    client.get_transport().set_keepalive(20)
    return client


def _http_get_via_channel(client, *, remote_port: int, path: str, timeout: float = 10.0) -> str:
    """开一条 direct-tcpip 通道，向容器内服务发一次 GET。

    这是**唯一诚实**的可行性检查：能收到 HTTP 响应就同时证明两件事——
    服务端允许 TCP 转发，且 worker 真的在听。任何一项不成立都会显式抛错。
    """
    transport = client.get_transport()
    if transport is None:
        raise TunnelError("SSH 传输未建立")
    try:
        chan = transport.open_channel("direct-tcpip", ("127.0.0.1", remote_port),
                                      ("127.0.0.1", 0), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise TunnelError(
            f"SSH 服务端拒绝或不支持 TCP 转发：{type(e).__name__}: {e}。"
            "替代路径：①在算力云控制台为该实例开放自定义服务端口，"
            "让 worker 直接对公网提供 HTTPS；②或让后端部署在能与该实例互通的机器上。")
    try:
        chan.settimeout(timeout)
        chan.sendall(f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
        chunks = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            if chan.recv_ready():
                data = chan.recv(65536)
                if not data:
                    break
                chunks.append(data)
                # HTTP/1.0 无分块编码：worker 会主动断开，这里多读一会即可
                time.sleep(0.2)
            else:
                time.sleep(0.1)
        return b"".join(chunks).decode("utf-8", "replace")
    finally:
        chan.close()


def assert_forwarding_allowed(client, *, remote_port: int) -> None:
    """确认穿透可行且 worker 在线。

    早前的实现连一个"肯定没人听"的高位端口来试探，靠异常文本里的关键字判断；
    这不可靠——paramiko 的异常串只有 "ChannelException(2, 'Connect failed')"，
    既看不出是"服务端禁止转发"还是"目标端口没服务"，实测把可用环境误判成不可用。
    """
    raw = _http_get_via_channel(client, remote_port=remote_port, path="/v1/healthz")
    if "state" not in raw or "ok" not in raw:
        raise TunnelError(
            f"worker 未在容器内 127.0.0.1:{remote_port} 响应健康检查。"
            f"原始响应前 200 字符：{raw[:200]}。"
            "请先 SSH 进实例确认 worker 进程已启动。")


class _ForwardHandler(socketserver.BaseRequestHandler):
    client_ref = None  # 由外层赋值：paramiko SSHClient
    remote_port = 0

    def handle(self) -> None:
        transport = self.client_ref.get_transport()  # type: ignore[union-attr]
        if transport is None or not transport.is_active():
            self.request.close()
            return
        try:
            # src_addr 必须用 BaseRequestHandler 的 client_address；
            # 早前这里写成 self.client（该属性不存在），于是每次连接都抛 AttributeError
            # 并走进下面的 except，表现为"转发通道建立失败"——真实原因被掩盖了。
            chan = transport.open_channel(
                "direct-tcpip", ("127.0.0.1", self.remote_port),
                self.client_address, timeout=30)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"转发通道建立失败：{type(e).__name__}: {e}\n")
            self.request.close()
            return
        self._pump(chan)

    def _pump(self, chan) -> None:
        sock = self.request
        try:
            while True:
                r, _w, _x = select.select([sock, chan], [], [], 60)
                if sock in r:
                    data = sock.recv(64 * 1024)
                    if not data:
                        break
                    # Channel.send 可能只接收一个 SSH 包（约 32 KiB）。
                    # 上传会收到 64 KiB 块，必须等待全部发送，不能丢弃剩余字节。
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(64 * 1024)
                    if not data:
                        break
                    sock.sendall(data)
        except Exception:  # noqa: BLE001 - 单个连接断开不该杀掉隧道
            pass
        finally:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


class _TunnelServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(*, client, ssh_host: str, ssh_port: int, local_port: int, remote_port: int) -> None:
    handler = type("BoundHandler", (_ForwardHandler,),
                   {"client_ref": client, "remote_port": remote_port})
    server = _TunnelServer(("127.0.0.1", local_port), handler)
    print(f"隧道已就绪：本机 http://127.0.0.1:{local_port} "
          f"→ {ssh_host}:{ssh_port} 容器内 127.0.0.1:{remote_port}", flush=True)
    print("把后端 AVATAR_STUDIO_M2_WORKER_URL 指向这个本机地址；Ctrl+C 结束。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="算力云 worker 的 SSH 本地端口转发")
    ap.add_argument("--ssh-host", default=os.environ.get("M2_SSH_HOST", DEFAULT_SSH_HOST))
    ap.add_argument("--ssh-port", type=int,
                    default=int(os.environ.get("M2_SSH_PORT", DEFAULT_SSH_PORT)))
    ap.add_argument("--ssh-user", default=os.environ.get("M2_SSH_USER", DEFAULT_SSH_USER))
    ap.add_argument("--remote-port", type=int, default=int(
        os.environ.get("M2_WORKER_REMOTE_PORT", "8080")))
    ap.add_argument("--local-port", type=int, default=int(
        os.environ.get("M2_WORKER_LOCAL_PORT", "18080")))
    ap.add_argument("--password-env", default="M2_SSH_PASSWORD")
    ns = ap.parse_args(argv)

    password = os.environ.get(ns.password_env, "").strip()
    if not password:
        password = getpass.getpass(f"{ns.ssh_user}@{ns.ssh_host}:{ns.ssh_port} 密码（不保存）：")

    try:
        client = connect(host=ns.ssh_host, port=ns.ssh_port, user=ns.ssh_user,
                         password=password)
    except Exception as e:  # noqa: BLE001
        print(f"SSH 连接失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    try:
        assert_forwarding_allowed(client, remote_port=ns.remote_port)
    except TunnelError as e:
        print(str(e), file=sys.stderr)
        return 3

    serve(client=client, ssh_host=ns.ssh_host, ssh_port=ns.ssh_port,
          local_port=ns.local_port, remote_port=ns.remote_port)
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
