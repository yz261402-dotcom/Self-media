"""Apply four narrow, checked substitutions to pinned MuseTalk normal inference.

Never edits the input file. Example:
  python 普通推理清理补丁.py 官方普通推理.py --output inference.patched.py
Without --output, prints a unified diff only.
"""
import argparse
import difflib
from pathlib import Path


REPLACEMENTS = (
    (
        "import shutil\n",
        "import shutil\nimport tempfile\n",
    ),
    (
        "    for task_id in inference_config:\n        try:\n",
        "    for task_id in inference_config:\n"
        "        save_dir_full = None  # Only this task can own extracted frames.\n"
        "        try:\n",
    ),
    (
        "                save_dir_full = os.path.join(temp_dir, input_basename)\n"
        "                os.makedirs(save_dir_full, exist_ok=True)\n",
        "                save_dir_full = tempfile.mkdtemp(prefix='source_frames_', dir=temp_dir)\n",
    ),
    (
        "            shutil.rmtree(save_dir_full)\n",
        "            if save_dir_full is not None:\n"
        "                shutil.rmtree(save_dir_full)\n",
    ),
)


def apply_patch(source):
    source = source.replace("\r\n", "\n")
    # Check all anchors before returning any modified source. Repeated application
    # or a different upstream version fails clearly instead of partially patching.
    for before, _ in REPLACEMENTS:
        count = source.count(before)
        if count != 1:
            raise ValueError(f"Expected one exact source anchor, found {count}: {before!r}")
    patched = source
    for before, after in REPLACEMENTS:
        patched = patched.replace(before, after, 1)
    compile(patched, "scripts/inference.py", "exec")
    return patched


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = args.source.read_text(encoding="utf-8-sig")
    patched = apply_patch(source)
    if args.output:
        if args.output.resolve() == args.source.resolve():
            parser.error("output must differ from source; preserve the official original")
        with args.output.open("x", encoding="utf-8", newline="\n") as out:
            out.write(patched)
    else:
        print("".join(difflib.unified_diff(
            source.splitlines(keepends=True), patched.splitlines(keepends=True),
            fromfile="a/scripts/inference.py", tofile="b/scripts/inference.py",
        )), end="")


if __name__ == "__main__":
    main()
