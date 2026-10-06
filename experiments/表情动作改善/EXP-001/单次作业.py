"""一次执行、保留失败与输入摘要的离线实验作业包装器。"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def save_new(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def execute(spec):
    directory = Path(spec["job_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    save_new(directory / "started.json", {"started_at": now(), "spec": spec, "automatic_retry": False})
    started = time.monotonic()
    result = {"state": "FAILED", "reason": None, "pid": None, "returncode": None,
              "gpu_peak_total_mib_sampled": None, "outputs": [], "quality": "NOT_REVIEWED"}
    process = None
    samples = []
    try:
        for file, expected in spec.get("inputs", {}).items():
            if digest(file) != expected:
                raise ValueError("input_digest_mismatch: " + file)
        if any(Path(file).exists() for file in spec["outputs"]):
            raise FileExistsError("output_already_exists")
        environment = os.environ.copy()
        environment.update(spec.get("environment", {}))
        with (directory / "process.log").open("xb") as log:
            process = subprocess.Popen(spec["command"], cwd=spec["cwd"], env=environment,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=(os.name != "nt"))
            result["pid"] = process.pid
            while process.poll() is None:
                if time.monotonic() - started > spec["timeout_seconds"]:
                    result["reason"] = "timeout"
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    break
                if spec.get("sample_gpu", True):
                    measure = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
                    if measure.returncode == 0:
                        used = int(measure.stdout.splitlines()[0].strip())
                        samples.append({"elapsed_seconds": round(time.monotonic() - started, 3), "gpu_total_used_mib": used})
                time.sleep(0.25)
            result["returncode"] = process.returncode
        if result["reason"] is None:
            if process.returncode != 0:
                result["reason"] = "process_exit_nonzero"
            elif not all(Path(file).is_file() and Path(file).stat().st_size > 0 for file in spec["outputs"]):
                result["reason"] = "missing_outputs"
            else:
                result["state"] = "PROCESS_SUCCEEDED"
        for file in spec["outputs"]:
            if Path(file).is_file():
                result["outputs"].append({"path": file, "bytes": Path(file).stat().st_size, "sha256": digest(file)})
    except Exception as error:
        result["reason"] = type(error).__name__ + ": " + str(error)
        if process is not None and process.poll() is None:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    finally:
        result["finished_at"] = now()
        result["wall_seconds"] = round(time.monotonic() - started, 3)
        result["estimated_job_gpu_cny"] = round(result["wall_seconds"] / 3600 * spec.get("hourly_cny", 1.58), 6)
        result["cost_scope"] = "job_wall_clock_only; excludes installation, idle time and all previous costs"
        if samples:
            result["gpu_peak_total_mib_sampled"] = max(s["gpu_total_used_mib"] for s in samples)
        save_new(directory / "gpu_samples.json", samples)
        save_new(directory / "result.json", result)
    return result


if __name__ == "__main__":
    result = execute(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["state"] == "PROCESS_SUCCEEDED" else 1)
