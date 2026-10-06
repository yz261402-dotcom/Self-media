"""Exercise the Windows entrypoint without SSH, task registration, or model calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


PRODUCTION = Path(__file__).resolve().parents[2]
ENTRYPOINT = PRODUCTION / "scripts/tunnel.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell entrypoint")


def quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def isolated(tmp_path):
    root = tmp_path / "带空格 project" / "production"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    if ENTRYPOINT.exists():
        shutil.copy2(ENTRYPOINT, scripts / "tunnel.ps1")
    venv = root / "backend/.venv"
    (venv / "Scripts").mkdir(parents=True)
    original = PRODUCTION / "backend/.venv"
    shutil.copy2(original / "pyvenv.cfg", venv / "pyvenv.cfg")
    for name in ("python.exe", "pythonw.exe"):
        shutil.copy2(original / "Scripts" / name, venv / "Scripts" / name)
    config = root / "runtime/tunnel/config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    calls = root / "calls.jsonl"
    (scripts / "managed_tunnel.py").write_text(
        "import json, pathlib, sys\n"
        f"log = pathlib.Path({str(calls)!r})\n"
        "with log.open('a', encoding='utf-8') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "mode = next((x for x in sys.argv if x in ('--status', '--check', '--stop')), 'run')\n"
        "config = json.loads(pathlib.Path(sys.argv[sys.argv.index('--config') + 1]).read_text())\n"
        "state = 'worker_unavailable' if config.get('offline') else 'ready'\n"
        "print(json.dumps({'state': state if mode == '--check' else 'stopped', 'mode': mode}))\n"
        "sys.exit(2 if mode == '--check' and state != 'ready' else 0)\n",
        encoding="utf-8",
    )
    return root, config, calls


def run_entrypoint(isolated, action, before="", after="", wait=0):
    root, config, _ = isolated
    assert ENTRYPOINT.exists(), "managed tunnel PowerShell entrypoint is missing"
    report = root / "task-report.json"
    harness = root / "invoke.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$global:events = [System.Collections.Generic.List[object]]::new()\n"
        "$global:task = $null\n"
        "function Get-ScheduledTask { [CmdletBinding()] param($TaskName) return $global:task }\n"
        "function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory) "
        "return [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory} }\n"
        "function New-ScheduledTaskTrigger { param([switch]$AtLogOn,$User) return @{AtLogOn=[bool]$AtLogOn;User=$User} }\n"
        "function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) return @{UserId=$UserId;LogonType=$LogonType;RunLevel=$RunLevel} }\n"
        "function New-ScheduledTaskSettingsSet { param($RestartCount,$RestartInterval,$MultipleInstances,$ExecutionTimeLimit,"
        "[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,[switch]$StartWhenAvailable) "
        "return @{RestartCount=$RestartCount;RestartSeconds=$RestartInterval.TotalSeconds;MultipleInstances=$MultipleInstances;"
        "ExecutionSeconds=$ExecutionTimeLimit.TotalSeconds;Battery=[bool]$AllowStartIfOnBatteries;KeepBattery=[bool]$DontStopIfGoingOnBatteries} }\n"
        "function Register-ScheduledTask { [CmdletBinding()] param($TaskName,$Action,$Trigger,$Principal,$Settings,$Description,[switch]$Force) "
        "$global:events.Add(@{kind='register';task=$TaskName;action=$Action;trigger=$Trigger;principal=$Principal;settings=$Settings}); "
        "$global:task=[pscustomobject]@{State='Ready';Actions=@($Action)}; return $global:task }\n"
        "function Start-ScheduledTask { [CmdletBinding()] param($TaskName) $global:events.Add(@{kind='start';task=$TaskName}) }\n"
        "function Enable-ScheduledTask { [CmdletBinding()] param($TaskName) $global:events.Add(@{kind='enable';task=$TaskName}) }\n"
        "function Disable-ScheduledTask { [CmdletBinding()] param($TaskName) $global:events.Add(@{kind='disable';task=$TaskName}) }\n"
        "function Stop-ScheduledTask { param($TaskName) throw 'must not forcibly stop tasks' }\n"
        "function Stop-Process { param($Id,$Name,[switch]$Force) throw 'must not kill unrelated processes' }\n"
        "function Start-Process { [CmdletBinding()] param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru) "
        "$global:events.Add(@{kind='process';file=$FilePath;arguments=$ArgumentList;window=$WindowStyle;cwd=$WorkingDirectory}); "
        "return [pscustomobject]@{Id=12345} }\n"
        + before + "\n"
        + f"& {quote(root / 'scripts/tunnel.ps1')} -Action {action} -ConfigPath {quote(config)} -WaitSeconds {wait}\n"
        + after + "\n"
        + f"ConvertTo-Json -InputObject @($global:events.ToArray()) -Depth 8 | Set-Content -LiteralPath {quote(report)} -Encoding UTF8\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(harness)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    events = json.loads(report.read_text(encoding="utf-8-sig"))
    return result, events


def test_entrypoints_parse_and_keep_existing_start_parameters():
    assert ENTRYPOINT.exists(), "managed tunnel PowerShell entrypoint is missing"
    command = (
        "$errors=$null;$tokens=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile({quote(ENTRYPOINT)},[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count){throw ($errors|Out-String)};"
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile({quote(PRODUCTION / 'start.ps1')},[ref]$tokens,[ref]$errors);"
        "if($errors.Count){throw ($errors|Out-String)};"
        "$ast.ParamBlock.Parameters.Name.VariablePath.UserPath|ConvertTo-Json"
    )
    result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert {"BackendPort", "FrontendPort", "NoBrowser", "SkipGpuTunnel"} <= set(json.loads(result.stdout))


def test_install_registers_durable_user_task_with_quoted_paths(isolated):
    result, events = run_entrypoint(isolated, "Install")
    event = next(event for event in events if event["kind"] == "register")
    assert event["task"] == "AvatarStudio-GpuTunnel"
    assert event["trigger"]["AtLogOn"] is True
    assert event["principal"]["LogonType"] == "Interactive"
    assert event["principal"]["RunLevel"] == "Limited"
    assert event["settings"] == {"RestartCount": 3, "RestartSeconds": 60.0, "MultipleInstances": "IgnoreNew", "ExecutionSeconds": 0.0, "Battery": True, "KeepBattery": True}
    assert f'"{isolated[0] / "scripts/managed_tunnel.py"}"' in event["action"]["Arguments"]
    assert f'--config "{isolated[1]}"' in event["action"]["Arguments"]
    assert [event["kind"] for event in events].count("start") == 1


def running_task(isolated):
    root, config, _ = isolated
    return (
        f"$global:task=[pscustomobject]@{{State='Running';Actions=@([pscustomobject]@{{Execute={quote(root / 'backend/.venv/Scripts/pythonw.exe')};"
        f"Arguments={quote(chr(34) + str(root / 'scripts/managed_tunnel.py') + chr(34) + ' --config ' + chr(34) + str(config) + chr(34))}}})}}"
    )


def test_start_reuses_running_task(isolated):
    _, events = run_entrypoint(isolated, "Start", before=running_task(isolated))
    assert not any(event["kind"] in {"start", "process"} for event in events)


def test_start_without_task_uses_hidden_process(isolated):
    _, events = run_entrypoint(isolated, "Start")
    event = next(event for event in events if event["kind"] == "process")
    assert event["window"] == "Hidden"
    assert Path(event["file"]).is_absolute()
    assert f'--config "{isolated[1]}"' in event["arguments"]


def test_status_reports_supervisor_and_fresh_health(isolated):
    result, events = run_entrypoint(isolated, "Status")
    body = json.loads(result.stdout)
    assert body["supervisor"]["mode"] == "--status"
    assert body["health"]["mode"] == "--check"
    assert not events
    calls = [json.loads(line) for line in isolated[2].read_text().splitlines()]
    assert [call[-1] for call in calls] == ["--status", "--check"]


@pytest.mark.parametrize("shell", list(dict.fromkeys(filter(None, (POWERSHELL, shutil.which("powershell.exe"))))))
def test_status_cli_returns_nonzero_when_real_supervisor_is_stopped(isolated, shell):
    """Exercise -File process exit semantics with the actual manager and no live tunnel."""
    root, config_path, _ = isolated
    shutil.copy2(PRODUCTION / "scripts/managed_tunnel.py", root / "scripts/managed_tunnel.py")
    # These placeholders only satisfy file existence checks. Status must never
    # start SSH or read authentication material when no supervisor is running.
    identity = root / "unused-identity"
    known_hosts = root / "unused-known-hosts"
    identity.write_text("unused test fixture", encoding="ascii")
    known_hosts.write_text("unused test fixture", encoding="ascii")
    config_path.write_text(json.dumps({
        "ssh_executable": shutil.which("ssh.exe"),
        "ssh_host": "invalid.example", "ssh_port": 22, "ssh_user": "test",
        "identity_file": str(identity), "known_hosts_file": str(known_hosts),
        "local_port": 18080, "remote_port": 8080,
        "runtime_dir": str(config_path.parent),
        "required_stages": ["align", "asr", "lipsync", "motion"],
    }), encoding="utf-8")
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(root / "scripts/tunnel.ps1"),
         "-Action", "Status", "-ConfigPath", str(config_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
    )
    body = json.loads(result.stdout)
    assert body["supervisor"]["state"] == "stopped"
    assert body["health"]["state"] == "stopped"
    assert result.returncode == 2, result.stdout + result.stderr


def test_stop_disables_task_before_manager_stop_without_process_kill(isolated):
    _, events = run_entrypoint(isolated, "Stop", before=running_task(isolated))
    assert events[0]["kind"] == "disable"
    calls = [json.loads(line) for line in isolated[2].read_text().splitlines()]
    assert calls[0][-1] == "--stop"
    assert all(call[-1] in {"--stop", "--status"} for call in calls)


def test_worker_unavailable_warns_without_stopping_supervisor(isolated):
    isolated[1].write_text('{"offline": true}', encoding="utf-8")
    result, events = run_entrypoint(isolated, "Start", wait=1)
    assert "worker_unavailable" in result.stdout
    assert [event["kind"] for event in events].count("process") == 1
    calls = [json.loads(line) for line in isolated[2].read_text().splitlines()]
    assert all(call[-1] == "--check" for call in calls)


def test_failed_task_registration_keeps_hidden_session_fallback(isolated):
    before = "function Register-ScheduledTask { [CmdletBinding()] param($TaskName,$Action,$Trigger,$Principal,$Settings,$Description,[switch]$Force) throw 'registration denied' }"
    result, events = run_entrypoint(isolated, "Install", before=before)
    assert "registration denied" in result.stdout
    assert [event["kind"] for event in events] == ["process"]


def test_start_switch_can_skip_gpu_tunnel_without_changing_local_startup(isolated):
    root, _, _ = isolated
    shutil.copy2(PRODUCTION / "start.ps1", root / "start.ps1")
    vite = root / "frontend/node_modules/vite/bin/vite.js"
    vite.parent.mkdir(parents=True)
    vite.write_text("", encoding="utf-8")
    marker = root / "tunnel-invoked"
    (root / "scripts/tunnel.ps1").write_text(f"'called' | Set-Content -LiteralPath {quote(marker)}", encoding="utf-8-sig")
    harness = root / "start-test.ps1"
    harness.write_text(
        "function Invoke-WebRequest { param($Uri,$TimeoutSec,[switch]$UseBasicParsing) return @{StatusCode=200;Content='{\"ok\":true}'} }\n"
        + f"& {quote(root / 'start.ps1')} -NoBrowser -SkipGpuTunnel\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run([POWERSHELL, "-NoProfile", "-File", str(harness)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
