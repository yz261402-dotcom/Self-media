"""Local storyboard worker startup, without launching a real worker."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


PRODUCTION = Path(__file__).resolve().parents[2]
SHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(os.name != "nt" or not SHELL, reason="Windows PowerShell only")


def ps_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def worker_fixture(tmp_path):
    root = tmp_path / "path with spaces" / "production"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = PRODUCTION / "scripts/local-storyboard-worker.ps1"
    if source.exists():
        shutil.copy2(source, scripts / source.name)
    python = root / "backend/.venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (root / "runtime").mkdir()
    return root, python


def invoke(root: Path, python: Path, mode: str, module: str = 'app.short_video.worker') -> subprocess.CompletedProcess[str]:
    helper = root / "scripts/local-storyboard-worker.ps1"
    assert helper.exists(), "local-storyboard-worker.ps1 is missing"
    runtime = root / "runtime"
    backend = root / "backend"
    harness = root / "harness.ps1"
    harness.write_text(
        "$ErrorActionPreference='Stop'\n"
        + f"$mode={ps_quote(mode)}\n"
        + f"$exe={ps_quote(python)}\n"
        + f"$record={ps_quote(root / 'launch.txt')}\n"
        + "function Get-CimInstance {\n"
        + "  param($ClassName,$Filter,$OperationTimeoutSec)\n"
        + "  $match = [pscustomobject]@{ ProcessId=822; ExecutablePath=$exe; CommandLine=('\"'+$exe+'\" -m app.short_video.worker --loop') }\n"
        + "  $unrelated = [pscustomobject]@{ ProcessId=733; ExecutablePath=$exe; CommandLine=('\"'+$exe+'\" -m unrelated.module --loop') }\n"
        + "  if ($Filter -eq 'ProcessId = 733') { return $unrelated }\n"
        + "  if ($Filter -eq 'ProcessId = 822' -and $mode -eq 'pid') { return $match }\n"
        + "  if ($Filter -eq 'ProcessId = 411' -and $script:launched) { return [pscustomobject]@{ ProcessId=411; ExecutablePath=$exe; CommandLine=('\"'+$exe+'\" -m app.short_video.worker --loop') } }\n"
        + "  if ($Filter -like 'Name =*') { if ($mode -eq 'scan') { return @($unrelated,$match) }; return @($unrelated) }\n"
        + "  return $null\n"
        + "}\n"
        + "function Start-Process {\n"
        + "  param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru,$RedirectStandardOutput,$RedirectStandardError)\n"
        + "  $script:launched=$true\n"
        + "  @($FilePath,($ArgumentList -join ' '),$WorkingDirectory,$WindowStyle,$RedirectStandardOutput,$RedirectStandardError) | Set-Content -LiteralPath $record\n"
        + "  return [pscustomobject]@{ Id=411 }\n"
        + "}\n"
        + "function Start-Sleep { param($Milliseconds) }\n"
        + f"& {ps_quote(helper)} -PythonPath {ps_quote(python)} -BackendPath {ps_quote(backend)} -RuntimePath {ps_quote(runtime)}"
        + (f" -WorkerModule {ps_quote(module)}" if module != 'app.short_video.worker' else '') + "\n",
        encoding="utf-8-sig",
    )
    if module != 'app.short_video.worker':
        harness.write_text(harness.read_text(encoding='utf-8-sig').replace('app.short_video.worker --loop', module+' --loop'), encoding='utf-8-sig')
    return subprocess.run(
        [SHELL, "-NoProfile", "-NonInteractive", "-File", str(harness)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )


def test_verified_pid_reuses_worker(worker_fixture):
    root, python = worker_fixture
    (root / "runtime/storyboard-worker.pid").write_text("822\n", encoding="ascii")
    result = invoke(root, python, "pid")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / "launch.txt").exists()
    assert (root / "runtime/storyboard-worker.pid").read_text().strip() == "822"


def test_stale_pid_finds_matching_process_and_rewrites_pid(worker_fixture):
    root, python = worker_fixture
    (root / "runtime/storyboard-worker.pid").write_text("733\n", encoding="ascii")
    result = invoke(root, python, "scan")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / "launch.txt").exists()
    assert (root / "runtime/storyboard-worker.pid").read_text().strip() == "822"


def test_unrelated_pid_starts_hidden_worker_with_local_python_and_logs(worker_fixture):
    root, python = worker_fixture
    (root / "runtime/storyboard-worker.pid").write_text("733\n", encoding="ascii")
    result = invoke(root, python, "new")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = (root / "launch.txt").read_text().splitlines()
    assert lines == [
        str(python), "-m app.short_video.worker --loop", str(root / "backend"), "Hidden",
        str(root / "runtime/storyboard-worker.log"), str(root / "runtime/storyboard-worker.err.log"),
    ]
    assert (root / "runtime/storyboard-worker.pid").read_text().strip() == "411"


def test_workbench_calls_local_worker_entrypoint():
    script = (PRODUCTION / "start.ps1").read_text(encoding="utf-8-sig")
    assert "local-storyboard-worker.ps1" in script


def test_subtitle_worker_has_separate_pid_logs_and_command(worker_fixture):
    root, python = worker_fixture
    (root/'runtime/storyboard-worker.pid').write_text('822\n',encoding='ascii')
    result=invoke(root,python,'new','app.short_video.subtitle_jobs')
    assert result.returncode==0, result.stdout+result.stderr
    lines=(root/'launch.txt').read_text().splitlines()
    assert lines[1]=='-m app.short_video.subtitle_jobs --loop'
    assert lines[3]=='Hidden'
    assert lines[4:]==[str(root/'runtime/subtitle-worker.log'),str(root/'runtime/subtitle-worker.err.log')]
    assert (root/'runtime/subtitle-worker.pid').read_text().strip()=='411'
    assert (root/'runtime/storyboard-worker.pid').read_text().strip()=='822'


def test_existing_subtitle_worker_reuses_its_own_pid(worker_fixture):
    root,python=worker_fixture
    (root/'runtime/subtitle-worker.pid').write_text('822\n',encoding='ascii')
    result=invoke(root,python,'pid','app.short_video.subtitle_jobs')
    assert result.returncode==0,result.stdout+result.stderr
    assert not (root/'launch.txt').exists()
