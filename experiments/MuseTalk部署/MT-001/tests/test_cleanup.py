"""Offline regression checks against real inference source; no model imports/GPU.

Run before patching: python tests/test_cleanup.py --original
Run after patching:  python tests/test_cleanup.py
Only source-path setup/cleanup statements are extracted. FFmpeg is the only
replaced side effect; actual mkdir/rmtree operations run in TemporaryDirectory.
"""
import ast
import glob
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "官方普通推理.py"
USE_ORIGINAL = "--original" in sys.argv
if USE_ORIGINAL:
    sys.argv.remove("--original")


def read_source():
    source = ORIGINAL.read_text(encoding="utf-8-sig")
    if not USE_ORIGINAL:
        patch_path = ROOT / "普通推理清理补丁.py"
        spec = importlib.util.spec_from_file_location("cleanup_patch", patch_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = module.apply_patch(source)
    compile(source, "inference.py", "exec")
    return source


def make_cleanup_runner(source):
    tree = ast.parse(source)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    loop = next(n for n in main.body if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and n.target.id == "task_id")
    task_try = next(n for n in loop.body if isinstance(n, ast.Try))
    setup = []
    for statement in task_try.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(n, ast.Name) and n.id == "whisper_input_features"
            for target in statement.targets for n in ast.walk(target)
        ):
            break
        setup.append(statement)
    cleanup = next(n for n in task_try.body if any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute) and call.func.attr == "rmtree"
        and call.args and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "save_dir_full"
        for call in ast.walk(n)
    ))
    task_try.body = setup + ast.parse("hook(task_id, locals().get('save_dir_full'))").body + [cleanup]
    runner = ast.parse("def run(args, inference_config, hook):\n    pass\n").body[0]
    runner.body = [loop]
    # Retain only actual standard-library imports, so missing patch imports fail.
    imports = [n for n in tree.body if isinstance(n, ast.Import) and all(a.name in {"os", "glob", "shutil", "tempfile"} for a in n.names)]
    unit = ast.fix_missing_locations(ast.Module(body=imports + [runner], type_ignores=[]))
    errors = []
    namespace = {
        "get_file_type": lambda p: "video" if str(p).endswith(".mp4") else "image" if str(p).endswith(".png") else "unsupported",
        "get_video_fps": lambda p: 25,
        "print": lambda *args: errors.append(" ".join(map(str, args))),
    }
    exec(compile(unit, "actual_inference_cleanup", "exec"), namespace)
    return namespace["run"], errors


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="musetalk-cleanup-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = SimpleNamespace(result_dir=str(self.root / "results"), version="v15", output_vid_name=None, fps=25)
        self.runner, self.errors = make_cleanup_runner(read_source())
        self.system_mock = patch("os.system", return_value=0)
        self.system_mock.start()
        self.addCleanup(self.system_mock.stop)

    def run_tasks(self, paths, hook=lambda task, owned: None):
        config = {str(i): {"video_path": str(p), "audio_path": str(self.root / "speech.wav")} for i, p in enumerate(paths)}
        self.runner(self.args, config, hook)

    def test_single_image_preserved_without_cleanup_error(self):
        source = self.root / "portrait.png"
        source.write_bytes(b"input image")
        self.run_tasks([source])
        self.assertEqual(source.read_bytes(), b"input image")
        self.assertEqual(self.errors, [])

    def test_input_frame_directory_preserved_without_cleanup_error(self):
        source = self.root / "frames"
        source.mkdir()
        frame = source / "00000000.png"
        frame.write_bytes(b"input frame")
        self.run_tasks([source])
        self.assertEqual(frame.read_bytes(), b"input frame")
        self.assertEqual(self.errors, [])

    def test_video_cleans_only_new_owned_directory(self):
        source = self.root / "clip.mp4"
        source.write_bytes(b"input video")
        existing = self.root / "results" / "v15" / "clip"
        existing.mkdir(parents=True)
        sentinel = existing / "do-not-delete.txt"
        sentinel.write_text("older evidence", encoding="utf-8")
        owned = []
        def observe(task, directory):
            owned.append(Path(directory))
            self.assertTrue(owned[-1].is_dir())
            (owned[-1] / "00000000.png").write_bytes(b"extracted frame")
        self.run_tasks([source], observe)
        self.assertEqual(self.errors, [])
        self.assertEqual(source.read_bytes(), b"input video")
        self.assertTrue(sentinel.is_file(), "preexisting same-name directory was deleted")
        self.assertNotEqual(owned[0], existing)
        self.assertFalse(owned[0].exists(), "task-owned frame directory was not cleaned")

    def test_video_then_image_then_frames_resets_directory_ownership(self):
        video = self.root / "video.mp4"
        video.write_bytes(b"video")
        image = self.root / "image.png"
        image.write_bytes(b"image")
        frames = self.root / "frames"
        frames.mkdir()
        (frames / "0.png").write_bytes(b"frame")
        observations = []
        def observe(task, directory):
            observations.append(directory)
            if task == "1":
                # Another task may reuse an old path after the video is cleaned.
                stale_path = Path(observations[0])
                stale_path.mkdir(exist_ok=True)
                (stale_path / "new-owner.txt").write_text("preserve", encoding="utf-8")
        self.run_tasks([video, image, frames], observe)
        self.assertEqual(observations[1:], [None, None], "non-video task inherited previous ownership")
        self.assertTrue((Path(observations[0]) / "new-owner.txt").is_file())
        self.assertTrue(image.is_file())
        self.assertTrue((frames / "0.png").is_file())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
