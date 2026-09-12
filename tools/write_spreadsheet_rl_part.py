#!/usr/bin/env python3
"""Write one ordered range part into the checkpoint assembly file."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

EXPECTED = 8_822_894_520
PART_COUNT = 16


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model_dir", type=Path)
    ap.add_argument("index", type=int)
    args = ap.parse_args()
    if not 0 <= args.index < PART_COUNT:
        raise SystemExit("part index must be 0..15")
    root = args.model_dir.resolve()
    src = root / "parts" / f"part_{args.index:02d}"
    dst = root / "model.safetensors.assembling"
    if not src.is_file():
        raise SystemExit(f"missing {src}")
    if not dst.exists():
        with dst.open("wb") as f:
            f.truncate(EXPECTED)
    elif dst.stat().st_size != EXPECTED:
        raise SystemExit(f"assembly size is {dst.stat().st_size}, expected {EXPECTED}")
    offset = sum((root / "parts" / f"part_{i:02d}").stat().st_size for i in range(args.index))
    with src.open("rb") as inp, dst.open("r+b", buffering=0) as out:
        pos = offset
        while True:
            block = inp.read(16 * 1024 * 1024)
            if not block:
                break
            out.seek(pos)
            out.write(block)
            pos += len(block)
        out.flush()
        os.fsync(out.fileno())
    print(f"part {args.index:02d} written at offset {offset} ({src.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
