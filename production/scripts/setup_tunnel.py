"""一次性配置 OpenSSH 专用隧道；密码只由 ssh.exe 在本机控制台读取。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys


PRODUCTION = Path(__file__).resolve().parents[1]
CONFIG = PRODUCTION / "runtime/tunnel/config.json"


def validate_target(host, user, port):
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("Invalid SSH host")
    if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", user):
        raise ValueError("Invalid SSH user")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid SSH port")


def trusted_host_lines(keygen, host, port, known_hosts, *, run=subprocess.run):
    lookup = host if port == 22 else f"[{host}]:{port}"
    result = run([keygen, "-F", lookup, "-f", str(known_hosts)], capture_output=True, text=True, check=False)
    lines = [line for line in result.stdout.splitlines() if line.strip() and not line.startswith("#")]
    if result.returncode or not lines:
        raise ValueError("No trusted host in known_hosts; verify the server fingerprint before setup")
    return "\n".join(lines) + "\n"


def install_command(public_key, remote_port):
    validate_target("localhost", "root", remote_port)
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9_.@-]+)?", public_key.strip()):
        raise ValueError("Invalid Ed25519 public key")
    record = f'restrict,port-forwarding,permitopen="127.0.0.1:{remote_port}",command="/bin/false" {public_key.strip()}'
    # Append only. Existing keys and active GPU jobs are never changed.
    return "\n".join([
        "set -eu", "umask 077", 'mkdir -p "$HOME/.ssh"', 'chmod 700 "$HOME/.ssh"',
        'auth_file="$HOME/.ssh/authorized_keys"', 'touch "$auth_file"', 'chmod 600 "$auth_file"',
        f"key_record={shlex.quote(record)}",
        'if ! grep -Fqx -- "$key_record" "$auth_file"; then printf "\\n%s\\n" "$key_record" >> "$auth_file"; fi',
        "printf 'AVATAR_TUNNEL_KEY_INSTALLED\\n'",
        "printf 'WORKER_HEALTH: '",
        f"curl --noproxy '*' -fsS --max-time 5 http://127.0.0.1:{remote_port}/v1/healthz || true",
        "printf '\\n'",
    ])


def bootstrap_args(cfg):
    validate_target(cfg["ssh_host"], cfg["ssh_user"], cfg["ssh_port"])
    known_hosts = Path(cfg["known_hosts_file"]).as_posix()
    if any(ord(char) < 32 for char in known_hosts) or '"' in known_hosts or '%' in known_hosts:
        raise ValueError("known_hosts path must be literal")
    return [cfg["ssh_executable"], "-F", "none", "-T", "-p", str(cfg["ssh_port"]),
            "-o", "StrictHostKeyChecking=yes", "-o", f'UserKnownHostsFile="{known_hosts}"',
            "-o", "GlobalKnownHostsFile=none", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
            "-o", "ConnectTimeout=15", "-o", "ConnectionAttempts=1", "-o", "NumberOfPasswordPrompts=3",
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            f'{cfg["ssh_user"]}@{cfg["ssh_host"]}']


def secure_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Dedicated directory only; do not alter the rest of the user's .ssh.
        who = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
        sid = re.search(r"S-1-5-[0-9-]+", who).group(0)
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], check=True, capture_output=True)
    else:
        path.chmod(0o700)


def secure_private_key(path):
    if os.name == "nt":
        # ssh-keygen installs an explicit Administrators ACE. Inherit the
        # dedicated directory's narrower ACL, then freeze those permissions.
        subprocess.run(["icacls", str(path), "/reset"], check=True, capture_output=True)
        subprocess.run(["icacls", str(path), "/inheritance:d"], check=True, capture_output=True)
    else:
        path.chmod(0o600)


def prepare(host, port, user):
    validate_target(host, user, port)
    ssh = shutil.which("ssh")
    keygen = shutil.which("ssh-keygen")
    if not ssh or not keygen:
        raise ValueError("Windows OpenSSH Client is required")
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        if (cfg["ssh_host"], cfg["ssh_port"], cfg["ssh_user"]) != (host, port, user):
            raise ValueError("Existing target differs; stop the old tunnel and review config.json before migration")
        return cfg
    ssh_dir = Path.home() / ".ssh"
    pinned = trusted_host_lines(keygen, host, port, ssh_dir / "known_hosts")
    private_dir = ssh_dir / "avatar_studio"
    secure_directory(private_dir)
    key = private_dir / "id_ed25519"
    if not key.exists():
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", "avatar-studio-tunnel", "-f", str(key)], check=True)
    if not key.with_suffix(".pub").exists():
        raise ValueError("Private key exists without public key; refusing to overwrite it")
    secure_private_key(key)
    known = private_dir / "known_hosts"
    known.write_text(pinned, encoding="utf-8")
    cfg = dict(ssh_executable=ssh, ssh_host=host, ssh_port=port, ssh_user=user,
               identity_file=str(key), known_hosts_file=str(known), local_port=18080,
               remote_port=8080, runtime_dir=str(CONFIG.parent),
               required_stages=["align", "asr", "lipsync", "motion"])
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default="connect.weste.seetacloud.com")
    parser.add_argument("--ssh-port", type=int, default=21170)
    parser.add_argument("--ssh-user", default="root")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare-only", action="store_true")
    group.add_argument("--install-key", action="store_true")
    args = parser.parse_args(argv)
    cfg = prepare(args.ssh_host, args.ssh_port, args.ssh_user)
    print(f"Configuration: {CONFIG}", flush=True)
    if args.install_key:
        public_key = Path(cfg["identity_file"] + ".pub").read_text(encoding="utf-8").strip()
        started_at = datetime.now(timezone.utc)
        ssh_log = CONFIG.parent / ("key-install-" + started_at.strftime("%Y%m%dT%H%M%S%f") + ".ssh.log")
        command = bootstrap_args(cfg)
        command[1:1] = ["-E", str(ssh_log), "-o", "LogLevel=VERBOSE"]
        print("Enter SSH password only in this local window. It is not saved.\n", flush=True)
        result = subprocess.run(command + [install_command(public_key, cfg["remote_port"])])
        receipt = {"installed": result.returncode == 0, "exit_code": result.returncode,
                   "started_at": started_at.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
                   "ssh_log": str(ssh_log)}
        (CONFIG.parent / "key-install-result.json").write_text(json.dumps(receipt), encoding="utf-8")
        if result.returncode:
            print("Key installation failed; existing services were not restarted.", file=sys.stderr)
            return result.returncode
        (CONFIG.parent / "key-installed.json").write_text(json.dumps({"installed": True, "ssh_host": cfg["ssh_host"]}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Tunnel setup failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
