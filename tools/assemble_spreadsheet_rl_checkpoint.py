#!/usr/bin/env python3
"""Assemble the range-downloaded Spreadsheet-RL safetensors checkpoint.

The model was downloaded as 16 ordered byte-range files.  This utility copies
them into a temporary file, validates the expected byte count, then atomically
replaces the partial model.safetensors file.  It never overwrites a complete
file with a short or malformed result.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path


EXPECTED_BYTES = 8_822_894_520


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_dir", type=Path)
    parser.add_argument("--expected-bytes", type=int, default=EXPECTED_BYTES)
    args = parser.parse_args()

    model_dir = args.model_dir.resolve()
    parts_dir = model_dir / "parts"
    target = model_dir / "model.safetensors"
    temp = model_dir / "model.safetensors.assembling"
    parts = [parts_dir / f"part_{i:02d}" for i in range(16)]
    missing = [str(p) for p in parts if not p.is_file()]
    if missing:
        raise SystemExit("Missing checkpoint parts: " + ", ".join(missing))

    total = sum(p.stat().st_size for p in parts)
    if total != args.expected_bytes:
        raise SystemExit(f"Part size mismatch: got {total}, expected {args.expected_bytes}")

    # Always rebuild the temporary file from byte zero.  This is slower than
    # trying to resume an interrupted buffered copy, but it makes the result
    # deterministic and avoids trusting a partial write boundary.
    with temp.open("wb") as out:
        for part in parts:
            with part.open("rb") as src:
                while True:
                    block = src.read(16 * 1024 * 1024)
                    if not block:
                        break
                    out.write(block)
        out.flush()
        os.fsync(out.fileno())

    assembled = temp.stat().st_size
    if assembled != args.expected_bytes:
        temp.unlink(missing_ok=True)
        raise SystemExit(f"Assembled size mismatch: got {assembled}, expected {args.expected_bytes}")
    os.replace(temp, target)
    print(f"Assembled {target} ({assembled} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
