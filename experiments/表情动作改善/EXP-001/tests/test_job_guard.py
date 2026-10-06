import importlib.util
import json
from pathlib import Path
import sys
import pytest

path = Path(__file__).resolve().parents[1] / "单次作业.py"
spec = importlib.util.spec_from_file_location("motion_job", path)
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)


def request(tmp_path, code, timeout=10):
    return {"job_dir": str(tmp_path / "attempt1"), "cwd": str(tmp_path),
            "command": [sys.executable, "-c", code], "timeout_seconds": timeout,
            "outputs": [str(tmp_path / "clip.txt")], "inputs": {},
            "sample_gpu": False, "hourly_cny": 1.58}


def test_no_duplicate_after_success(tmp_path):
    req = request(tmp_path, "from pathlib import Path; Path('clip.txt').write_text('first')")
    first = job.execute(req)
    assert first["state"] == "PROCESS_SUCCEEDED"
    saved = (tmp_path / "attempt1/started.json").read_bytes()
    with pytest.raises(FileExistsError):
        job.execute(req)
    assert (tmp_path / "attempt1/started.json").read_bytes() == saved
    assert (tmp_path / "clip.txt").read_text() == "first"


def test_timeout_preserves_failure_and_blocks_duplicate(tmp_path):
    req = request(tmp_path, "import time; time.sleep(5)", timeout=0.1)
    result = job.execute(req)
    assert result["state"] == "FAILED" and result["reason"] == "timeout"
    assert (tmp_path / "attempt1/started.json").is_file()
    assert json.loads((tmp_path / "attempt1/result.json").read_text())["state"] == "FAILED"
    with pytest.raises(FileExistsError):
        job.execute(req)


def test_zero_exit_without_output_is_failure(tmp_path):
    result = job.execute(request(tmp_path, "pass"))
    assert result["state"] == "FAILED" and result["reason"] == "missing_outputs"
