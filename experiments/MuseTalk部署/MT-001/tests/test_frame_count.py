"""Run the official process_frames AST with a real queue and NumPy pixels.

python tests/test_frame_count.py --original  # expected red: N - 1 frames
python tests/test_frame_count.py             # patched: all N frames
No model imports, GPU, or network. Resize/blending/PNG encoding are isolated
because this regression concerns the consumer's frame count and ordering.
"""
import ast
import copy
import importlib.util
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace
import unittest

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
USE_ORIGINAL = "--original" in sys.argv
if USE_ORIGINAL:
    sys.argv.remove("--original")


def load_process_frames(writes):
    source = (ROOT / "官方实时推理.py").read_text(encoding="utf-8-sig")
    if not USE_ORIGINAL:
        spec = importlib.util.spec_from_file_location("frame_patch", ROOT / "实时帧数补丁.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = module.apply_patch(source)
    tree = ast.parse(source)
    avatar = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Avatar")
    method = next(n for n in avatar.body if isinstance(n, ast.FunctionDef) and n.name == "process_frames")
    unit = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))

    def write_image(path, pixels):
        writes.append((path, pixels.copy()))
        return True

    namespace = {
        "queue": queue, "copy": copy, "time": time, "np": np,
        "print": lambda *args: None,
        "cv2": SimpleNamespace(resize=lambda pixels, shape: pixels.copy(), imwrite=write_image),
        "get_image_blending": lambda original, generated, bbox, mask, coords: generated.copy(),
    }
    exec(compile(unit, "official_process_frames", "exec"), namespace)
    return namespace["process_frames"]


class FrameCountTests(unittest.TestCase):
    def assert_all_frames_saved(self, count):
        writes = []
        consume = load_process_frames(writes)
        frames = queue.Queue()
        for index in range(count):
            frames.put(np.full((2, 2, 3), index, dtype=np.uint8))
        avatar = SimpleNamespace(
            idx=0, avatar_path="isolated-avatar",
            coord_list_cycle=[(0, 0, 2, 2)],
            frame_list_cycle=[np.zeros((2, 2, 3), dtype=np.uint8)],
            mask_list_cycle=[np.ones((2, 2), dtype=np.uint8)],
            mask_coords_list_cycle=[(0, 0, 2, 2)],
        )
        exceptions = []
        def run():
            try:
                consume(avatar, frames, count, False)
            except BaseException as error:
                exceptions.append(error)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive(), "consumer waited for nonexistent extra frames")
        self.assertEqual(exceptions, [])
        self.assertEqual(len(writes), count, "consumer dropped an expected output frame")
        self.assertEqual(avatar.idx, count)
        self.assertTrue(frames.empty(), "unconsumed frames remain in the queue")
        self.assertEqual([path for path, _ in writes], [f"isolated-avatar/tmp/{i:08d}.png" for i in range(count)])
        self.assertEqual([int(pixels[0, 0, 0]) for _, pixels in writes], list(range(count)))

    def test_video_len_1(self):
        self.assert_all_frames_saved(1)

    def test_video_len_2(self):
        self.assert_all_frames_saved(2)

    def test_video_len_234(self):
        self.assert_all_frames_saved(234)


if __name__ == "__main__":
    unittest.main(verbosity=2)
