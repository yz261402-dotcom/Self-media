"""Install or check the remote GPU Worker through a restricted SSH command."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import uuid

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import setup_tunnel

PRODUCTION = SCRIPTS.parent
RUNTIME = PRODUCTION / "runtime/worker-service"
CONFIG = RUNTIME / "config.json"
REMOTE_PYTHON = "/root/autodl-tmp/avatar-motion/EXP-001/env-liveportrait/bin/python"


def worker_tokens(env_file, environ):
    values = {}
    for raw in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    values.update(environ)
    m2 = values.get("AVATAR_STUDIO_M2_WORKER_TOKEN", "").strip()
    asr = values.get("AVATAR_STUDIO_ASR_WORKER_TOKEN", "").strip() or m2
    if not m2:
        raise ValueError("backend worker token is missing")
    return {"m2": m2, "asr": asr}


def control_args(cfg, action):
    if action not in {"ensure", "status"}:
        raise ValueError("only ensure/status are allowed")
    args = setup_tunnel.bootstrap_args(cfg)
    # Remove interactive authentication preference, and require the one control key.
    pos = args.index("PreferredAuthentications=password,keyboard-interactive")
    del args[pos - 1:pos + 1]
    args[1:1] = ["-i", cfg["identity_file"], "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes"]
    return args + [action]


def install_request(cfg, bundle, installer_source):
    command = f"exec {shlex.quote(REMOTE_PYTHON)} -c {shlex.quote(installer_source)}"
    return setup_tunnel.bootstrap_args(cfg) + [command], json.dumps(bundle, ensure_ascii=False).encode("utf-8")


def redact(value, token):
    if isinstance(value, str):
        for form in (token, json.dumps(token)[1:-1], json.dumps(token, ensure_ascii=False)[1:-1]):
            value = value.replace(form, "[redacted]")
        return value
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, dict):
        return {redact(key, token): redact(item, token) for key, item in value.items()}
    return value


def install_response(returncode, stdout, token):
    if returncode != 0:
        return {"state": "configuration_error", "detail": "SSH installation did not finish successfully; check installation logs", "installed": False}
    try:
        status = json.loads(stdout.decode("utf-8"))
        if not isinstance(status, dict) or not isinstance(status.get("state"), str):
            raise ValueError("invalid response")
        return redact(status, token)
    except (ValueError, UnicodeError):
        return {"state": "configuration_error", "detail": "Invalid installation response", "installed": False}


def prepare_control_key():
    directory = Path.home() / ".ssh/avatar_studio"
    setup_tunnel.secure_directory(directory)
    key = directory / "id_ed25519_worker_control"
    if not key.exists():
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            raise ValueError("OpenSSH Client is missing")
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", "avatar-studio-worker-control", "-f", str(key)], check=True)
    setup_tunnel.secure_private_key(key)
    return key


def install_service():
    cfg = json.loads((PRODUCTION / "runtime/tunnel/config.json").read_text(encoding="utf-8-sig"))
    tokens = worker_tokens(PRODUCTION / "backend/.env", os.environ)
    if tokens["asr"] != tokens["m2"]:
        raise ValueError("ASR and M2 token differ, but this deployment shares one Worker")
    key = prepare_control_key()
    bundle = {"worker_token": tokens["m2"], "public_key": Path(str(key) + ".pub").read_text().strip(),
              "service_py": (PRODUCTION / "workers/m2_gpu/worker_service.py").read_text(encoding="utf-8"),
              "launcher_sh": (PRODUCTION / "workers/m2_gpu/启动完整服务.sh").read_text(encoding="utf-8")}
    installer = (SCRIPTS / "worker_service_install.py").read_text(encoding="utf-8")
    args, payload = install_request(cfg, bundle, installer)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    log = RUNTIME / ("install-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".ssh.log")
    args[1:1] = ["-E", str(log), "-o", "LogLevel=VERBOSE"]
    print("Enter the SSH password in this local window. It will not be saved.", file=sys.stderr, flush=True)
    # OpenSSH reads its password from the attached console; stdin carries only
    # the bundle over the encrypted connection, never as a process argument.
    result = subprocess.run(args, input=payload, capture_output=True)
    status = install_response(result.returncode, result.stdout, tokens["m2"])
    if result.returncode:
        # The installer writes only structured, non-secret errors to stdout.
        # Keep that explanation for diagnostics without claiming installation.
        diagnostic = redact(result.stdout.decode("utf-8", errors="replace"), tokens["m2"])
        (RUNTIME / "install.out.log").write_text(diagnostic[:65536], encoding="utf-8")
    if status.get("installed") is True:
        control_cfg = {field: cfg[field] for field in ("ssh_executable", "ssh_host", "ssh_port", "ssh_user", "known_hosts_file")}
        control_cfg["identity_file"] = str(key)
        CONFIG.write_text(json.dumps(control_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    # Defensive redaction, even if future remote diagnostics become verbose.
    safe = json.dumps(status, ensure_ascii=False)
    (RUNTIME / "install-result.json").write_text(safe, encoding="utf-8")
    if result.stderr:
        errors = redact(result.stderr.decode("utf-8", errors="replace"), tokens["m2"])
        (RUNTIME / "install.err.log").write_text(errors, encoding="utf-8")
    return json.loads(safe)


def control(action):
    if not CONFIG.exists():
        return {"state": "configuration_error", "detail": "Worker control is not installed"}
    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    try:
        result = subprocess.run(control_args(cfg, action), capture_output=True, timeout=28 if action == "ensure" else 12,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        status = json.loads(result.stdout.decode("utf-8"))
        if result.returncode != 0 and status.get("state") == "ready":
            return {"state": "worker_unavailable", "detail": "SSH control exited unsuccessfully"}
        return status
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return {"state": "worker_unavailable", "detail": "SSH control failed or timed out"}


def verified_local_port():
    import managed_tunnel
    cfg = managed_tunnel.Config.load(str(PRODUCTION / "runtime/tunnel/config.json"))
    if managed_tunnel.check_status(cfg).get("state") != "ready":
        raise ValueError("No trusted tunnel is ready")
    return cfg.local_port


def verify_auth():
    port = verified_local_port()
    tokens = worker_tokens(PRODUCTION / "backend/.env", os.environ)
    endpoint = "/v1/jobs/auth-probe-" + uuid.uuid4().hex
    results = {}
    for name, token in {"anonymous": None, **tokens}.items():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            headers = {"Authorization": "Bearer " + token} if token else {}
            connection.request("GET", endpoint, headers=headers)
            response = connection.getresponse()
            body = json.loads(response.read(65536))
            valid = (response.status == 401 if token is None else
                     response.status == 404 and body.get("error") == "job not found" and body.get("job_id") == endpoint.rsplit("/", 1)[1])
            results[name] = {"http_status": response.status, "passed": valid}
        finally:
            connection.close()
    return {"state": "ready" if all(item["passed"] for item in results.values()) else "configuration_error", "checks": results, "job_submitted": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "ensure", "status", "verify-auth"])
    args = parser.parse_args(argv)
    try:
        status = install_service() if args.action == "install" else verify_auth() if args.action == "verify-auth" else control(args.action)
    except (ValueError, OSError, subprocess.SubprocessError):
        status = {"state": "configuration_error", "detail": "Worker management failed; check deployment files, credentials and connectivity"}
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status.get("state") == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
