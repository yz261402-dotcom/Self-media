"""Windows Worker entrypoint checks with no cloud access or credential prompts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


PRODUCTION = Path(__file__).resolve().parents[2]
ENTRYPOINT = PRODUCTION / "scripts/worker-service.ps1"
SHELLS = list(dict.fromkeys(filter(None, (shutil.which("pwsh"), shutil.which("powershell.exe")))))
pytestmark = pytest.mark.skipif(os.name != "nt" or not SHELLS, reason="Windows PowerShell entrypoint")


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def isolated_worker(tmp_path):
    root = tmp_path / "云端控制 project" / "production"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    if ENTRYPOINT.exists():
        shutil.copy2(ENTRYPOINT, scripts / ENTRYPOINT.name)
    shutil.copy2(PRODUCTION / "start.ps1", root / "start.ps1")
    venv = root / "backend/.venv"
    (venv / "Scripts").mkdir(parents=True)
    shutil.copy2(PRODUCTION / "backend/.venv/pyvenv.cfg", venv / "pyvenv.cfg")
    shutil.copy2(PRODUCTION / "backend/.venv/Scripts/python.exe", venv / "Scripts/python.exe")
    config = root / "runtime/worker-service/config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    (root / "fixture-state.txt").write_text("blocked", encoding="ascii")
    (scripts / "manage_worker.py").write_text(
        "import json,pathlib,sys\n"
        "root = pathlib.Path(__file__).resolve().parent.parent\n"
        "mode = sys.argv[1]\n"
        "with (root / 'calls.txt').open('a',encoding='utf-8') as out: out.write(mode+'\\n')\n"
        "state = (root / 'fixture-state.txt').read_text()\n"
        "body = {'state':state, 'mode':mode}\n"
        "if mode == 'install':\n"
        "    body.update(installed=True,interactive_input_received=bool(sys.stdin.readline().strip()))\n"
        "print(json.dumps(body))\n"
        "raise SystemExit(0 if state == 'ready' else 2)\n",
        encoding="utf-8",
    )
    return root


def invoke(root, action, shell, input_text=None):
    assert ENTRYPOINT.exists(), "worker-service.ps1 is missing"
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(root / "scripts/worker-service.ps1"), "-Action", action],
        input=input_text, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
    )


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("state,code", [("blocked", 2), ("ready", 0)])
def test_status_preserves_json_and_process_exit_code(isolated_worker, shell, state, code):
    (isolated_worker / "fixture-state.txt").write_text(state, encoding="ascii")
    result = invoke(isolated_worker, "Status", shell)
    assert json.loads(result.stdout) == {"state": state, "mode": "status"}
    assert result.returncode == code, result.stdout + result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_install_preserves_interactive_input_and_install_result(isolated_worker, shell):
    result = invoke(isolated_worker, "Install", shell, input_text="fixture input\n")
    assert result.returncode == 2, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"state": "blocked", "mode": "install", "installed": True, "interactive_input_received": True}


@pytest.mark.parametrize("shell", SHELLS)
def test_start_failure_warns_and_returns_to_calling_workbench(isolated_worker, shell):
    assert ENTRYPOINT.exists(), "worker-service.ps1 is missing"
    harness = isolated_worker / "invoke-start.ps1"
    harness.write_text(
        "$ErrorActionPreference='Stop'\n"
        + f"& {quote(isolated_worker / 'scripts/worker-service.ps1')} -Action Start\n"
        + "'WORKBENCH_CONTINUES'\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run([shell, "-NoProfile", "-File", str(harness)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "blocked" in result.stdout
    assert "WORKBENCH_CONTINUES" in result.stdout
    assert (isolated_worker / "calls.txt").read_text().splitlines() == ["ensure"]


@pytest.mark.parametrize("skip", [False, True])
def test_workbench_ensures_worker_before_tunnel_and_skip_disables_both(isolated_worker, skip):
    root = isolated_worker
    vite = root / "frontend/node_modules/vite/bin/vite.js"
    vite.parent.mkdir(parents=True)
    vite.write_text("", encoding="utf-8")
    tunnel_config = root / "runtime/tunnel/config.json"
    tunnel_config.parent.mkdir(parents=True)
    tunnel_config.write_text("{}", encoding="utf-8")
    # Exercise start.ps1 orchestration independently of either implementation.
    for name, call in (("worker-service.ps1", "worker"), ("tunnel.ps1", "tunnel")):
        (root / "scripts" / name).write_text(
            f"'{call}' | Add-Content -LiteralPath {quote(root / 'order.txt')}\n",
            encoding="utf-8-sig",
        )
    harness = root / "workbench.ps1"
    harness.write_text(
        "function Invoke-WebRequest { param($Uri,$TimeoutSec,[switch]$UseBasicParsing) return @{StatusCode=200;Content='{\"ok\":true}'} }\n"
        + f"& {quote(root / 'start.ps1')} -NoBrowser {'-SkipGpuTunnel' if skip else ''}\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run([SHELLS[0], "-NoProfile", "-File", str(harness)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    order = root / "order.txt"
    assert (order.read_text(encoding="utf-8-sig").splitlines() if order.exists() else []) == ([] if skip else ["worker", "tunnel"])
