"""Fix the pinned realtime consumer's last-frame boundary; preserve originals.

python 实时帧数补丁.py 官方实时推理.py --output realtime_inference.patched.py
Without --output, print the one-line unified diff only.
"""
import argparse
import difflib
from pathlib import Path


BEFORE = "            if self.idx >= video_len - 1:\n"
AFTER = "            if self.idx >= video_len:\n"


def apply_patch(source):
    source = source.replace("\r\n", "\n")
    count = source.count(BEFORE)
    if count != 1:
        raise ValueError(f"Expected exactly one upstream boundary anchor, found {count}")
    patched = source.replace(BEFORE, AFTER, 1)
    compile(patched, "scripts/realtime_inference.py", "exec")
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
            parser.error("output must differ from source; preserve the original")
        with args.output.open("x", encoding="utf-8", newline="\n") as out:
            out.write(patched)
    else:
        print("".join(difflib.unified_diff(
            source.splitlines(keepends=True), patched.splitlines(keepends=True),
            fromfile="a/scripts/realtime_inference.py", tofile="b/scripts/realtime_inference.py",
        )), end="")


if __name__ == "__main__":
    main()
