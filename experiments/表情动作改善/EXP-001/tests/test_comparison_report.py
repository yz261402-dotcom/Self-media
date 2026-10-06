"""Offline report contract checks; fixtures stay in temporary directories."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


GENERATOR = Path(__file__).resolve().parents[1] / "生成对比报告.py"


class ComparisonReportContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, data=None):
        data_path = self.root / "对比结果.json"
        if data is not None:
            data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        output = self.root / "report.html"
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(GENERATOR), "--data", str(data_path), "--output", str(output)],
            capture_output=True, text=True, encoding="utf-8",
        )
        return result, output

    def fixture(self):
        # Text-only test fixtures are never persisted as experiment results.
        return {
            "updated_at": "test fixture", "summary": "<script>alert(1)</script>",
            "inputs": {"avatar_path": None, "audio_path": None, "identity": "人物 B", "voice": "音色 3"},
            "clips": [
                {"id": "M1", "label": "单图 MuseTalk", "model": "MuseTalk", "status": "SUCCEEDED", "video_path": "原片 one.mp4", "poster_path": None, "width": 512, "height": 768, "fps": 25, "video_seconds": 15.24, "audio_seconds": 15.218844, "wall_seconds": 0, "gpu_peak_mib": None, "estimated_gpu_cny": None, "observations": [], "limitations": [], "evidence_path": None},
                {"id": "M2", "label": "LivePortrait + MuseTalk", "model": "LivePortrait + MuseTalk", "status": "FAILED", "video_path": "should-not-play.mp4", "observations": [], "limitations": ["执行失败"], "evidence_path": None},
                {"id": "M3", "label": "SadTalker", "model": "SadTalker", "status": "NOT_RUN", "video_path": None, "observations": [], "limitations": [], "evidence_path": None},
            ],
            "cost_note": "仅活跃推理时间", "notes": [],
        }

    def test_missing_data_has_clear_error_and_writes_no_report(self):
        result, output = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺少结果数据", result.stderr)
        self.assertFalse(output.exists())

    def test_success_only_player_and_escaped_content_and_unmeasured_values(self):
        data = self.fixture()
        (self.root / "原片 one.mp4").write_bytes(b"fixture only; not real media")
        result, output = self.invoke(data)
        self.assertEqual(result.returncode, 0, result.stderr)
        html = output.read_text("utf-8")
        self.assertEqual(html.count("<video "), 1)
        self.assertNotIn("should-not-play.mp4", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("未测", html)
        self.assertIn("0 秒", html)
        self.assertIn("执行失败", html)
        self.assertIn("未运行", html)
        self.assertIn("object-fit: contain", html)
        self.assertIn("%E5%8E%9F%E7%89%87%20one.mp4", html)

    def test_success_without_existing_video_fails_instead_of_broken_player(self):
        result, output = self.invoke(self.fixture())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("成功路线的视频不存在", result.stderr)
        self.assertFalse(output.exists())

    def test_path_cannot_escape_experiment_directory(self):
        data = self.fixture()
        data["clips"][0]["video_path"] = "../private.mp4"
        result, output = self.invoke(data)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("相对路径", result.stderr)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
