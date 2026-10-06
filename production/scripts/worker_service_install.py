"""Remote bootstrap; reads its secret bundle only from SSH stdin."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path("/root/autodl-tmp/avatar-m2worker")
PYTHON = Path("/root/autodl-tmp/avatar-motion/EXP-001/env-liveportrait/bin/python")
REQUIRED = [PYTHON, Path("/root/autodl-tmp/avatar-musetalk/MT-001/env/bin/python"),
            Path("/root/autodl-tmp/avatar-subtitles/ASR-001/env/bin/python"),
            Path("/root/autodl-tmp/avatar-subtitles/ASR-001/qwen_inference.py"),
            Path("/root/autodl-tmp/avatar-subtitles/ASR-001/qwen_audio_chunks.py")]
REQUIRED += [ROOT / name for name in ("worker_server.py", "runner_common.py", "runner_liveportrait.py",
                                    "runner_musetalk.py", "runner_qwen_asr.py", "runner_qwen_align.py")]


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or set(bundle) != {"worker_token", "public_key", "service_py", "launcher_sh"}:
        raise ValueError("invalid deployment bundle fields")
    if any(not isinstance(value, str) or not value for value in bundle.values()):
        raise ValueError("empty or invalid deployment field")
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+ avatar-studio-worker-control", bundle["public_key"]):
        raise ValueError("invalid control public key")
    if len(bundle["worker_token"]) > 4096 or any(ord(c) < 32 for c in bundle["worker_token"]):
        raise ValueError("invalid worker token format")


def write_private(path, content, mode):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(bundle, *, root=ROOT, user_home=None, required_files=None):
    validate_bundle(bundle)
    missing = [str(path) for path in (REQUIRED if required_files is None else required_files) if not Path(path).is_file()]
    if missing:
        raise ValueError("missing dependency: " + ", ".join(missing))
    if not os.access(PYTHON, os.X_OK):
        raise ValueError("worker Python dependency is not executable")
    root = Path(root)
    service = root / "service"
    service.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(service, 0o700)
    import fcntl
    with (service / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env_file = service / "env.json"
        if env_file.exists() and json.loads(env_file.read_text())["M2_WORKER_TOKEN"] != bundle["worker_token"]:
            raise ValueError("existing service token differs; explicit maintenance is required")
        sources = {"worker_service.py": bundle["service_py"], "start-all-stages.sh": bundle["launcher_sh"]}
        for name, source in sources.items():
            target = service / name
            if target.exists() and target.read_text(encoding="utf-8") != source:
                raise ValueError("existing service code differs; explicit maintenance is required")
        write_private(env_file, json.dumps({"M2_WORKER_TOKEN": bundle["worker_token"]}), 0o600)
        for name, source in sources.items():
            write_private(service / name, source, 0o700)
        ssh_dir = (Path.home() if user_home is None else Path(user_home)) / ".ssh"
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ssh_dir, 0o700)
        authorized = ssh_dir / "authorized_keys"
        record = f'restrict,command="{PYTHON} {service}/worker_service.py forced" {bundle["public_key"]}'
        with authorized.open("a+", encoding="utf-8") as handle:
            handle.seek(0)
            if record not in handle.read().splitlines():
                handle.write("\n" + record + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        os.chmod(authorized, 0o600)
    try:
        result = subprocess.run([str(PYTHON), str(service / "worker_service.py"), "ensure"],
                                capture_output=True, timeout=25, text=True)
        status = json.loads(result.stdout)
        if not isinstance(status, dict):
            raise ValueError("invalid ensure response")
        if result.returncode != 0 and status.get("state") == "ready":
            status = {"state": "worker_unavailable", "detail": "ensure did not finish successfully; query status"}
    except (ValueError, subprocess.TimeoutExpired, OSError):
        status = {"state": "starting", "detail": "installed; query the restricted control endpoint for current status"}
    return {**status, "installed": True}


if __name__ == "__main__":
    try:
        package = json.load(sys.stdin)
        outcome = install(package)
    except Exception as error:
        # No bundle or token values are ever included in diagnostics.
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(json.dumps({"state": "configuration_error", "detail": detail, "installed": False}))
        raise SystemExit(2)
    print(json.dumps(outcome))
