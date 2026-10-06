"""每个 LivePortrait 作业保留自己的结果，后续生成不能覆盖旧片。"""
import hashlib
import importlib
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess


def test_two_jobs_keep_separate_outputs_and_first_hash(tmp_path, monkeypatch):
    tmp_path = Path(tmp_path)
    runner_path = Path(__file__).resolve().parents[3] / "workers" / "m2_gpu"
    monkeypatch.syspath_prepend(str(runner_path))
    runner = importlib.import_module("runner_liveportrait")
    root = tmp_path / "model-installation"
    monkeypatch.setenv("M2_LP_ROOT", str(root))
    monkeypatch.setenv("M2_LP_PYTHON", sys.executable)
    source = tmp_path / "portrait.png"
    motion = tmp_path / "motion.mp4"
    source.write_bytes(b"portrait fixture; not a model invocation")
    motion.write_bytes(b"motion fixture; subprocess is mocked")
    args = {"source_image_url": source.as_uri(), "motion_ref_url": motion.as_uri(),
            "source_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
            "motion_hash": hashlib.sha256(motion.read_bytes()).hexdigest(),
            "output_seconds": 9.0}
    inference_calls = []

    def subprocess_boundary(argv, *, cwd, timeout, **kwargs):
        if argv[0] == "ffmpeg":
            Path(argv[-1]).write_bytes(b"normalized driving fixture")
        else:
            assert argv[1] == "inference.py"
            inference_calls.append(argv)
            output_dir = Path(argv[argv.index("-o") + 1])
            # 模拟真实固定 basename，不在 mock 里替 runner 做隔离。
            (output_dir / "source--driving_cfr25.mp4").write_bytes(
                f"result for job {len(inference_calls)}".encode())
        return CompletedProcess(argv, 0, stdout=b"fixture boundary")

    monkeypatch.setattr(runner.rc, "run_capture", subprocess_boundary)
    monkeypatch.setattr(runner.rc, "probe_duration_ms", lambda _: 9000)
    results = []
    for name in ("job-one", "job-two"):
        work_dir = tmp_path / "jobs" / name
        work_dir.mkdir(parents=True)
        job_path, result_path = work_dir / "job.json", work_dir / "result.json"
        job_path.write_text(json.dumps({"job_id": name, "args": args}), encoding="utf-8")
        runner.run(str(job_path), str(result_path))
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["state"] == "succeeded", result
        results.append(result)

    first, second = results
    assert first["output_path"] != second["output_path"]
    assert Path(first["output_path"]).is_relative_to(tmp_path / "jobs" / "job-one")
    assert Path(second["output_path"]).is_relative_to(tmp_path / "jobs" / "job-two")
    assert hashlib.sha256(Path(first["output_path"]).read_bytes()).hexdigest() == first["sha256"]
    assert len(inference_calls) == 2
    assert not (root / "outputs").exists()
