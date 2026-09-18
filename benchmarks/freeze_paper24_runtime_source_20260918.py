#!/usr/bin/env python3
"""Materialize the canonical package source recorded by the paper24 cells.

The held-out pilot was launched from a dirty research worktree.  This utility
reconstructs the exact 36-file source snapshot recorded in the canonical cell
manifests so infrastructure-only recovery can execute every cell with one
package version, independent of later edits to ``src/``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PILOT = REPO / "benchmarks/results/paper24-heldout-pilot-qwen36plus-20260917"
REFERENCE = (
    REPO
    / "benchmarks/results/spreadsheetagent-clean-room-four-full-20260917"
    / "frozen-source/spreadsheet_harness"
)
DESTINATION = PILOT / "frozen-runtime-source/spreadsheet_harness"
CANONICAL_VARIANT = "31ff9b96bbe399810f6e1b974c1f27ad0b361257077c10109f646c200068f3ac"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_variant(source_files: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(source_files, sort_keys=True).encode("utf-8")
    ).hexdigest()


def canonical_source_files() -> dict[str, str]:
    for manifest in sorted((PILOT / "runs/heldout-pilot").rglob("manifest.json")):
        document = json.loads(manifest.read_text(encoding="utf-8"))
        source_files = document.get("implementation", {}).get("source_files", {})
        if source_variant(source_files) == CANONICAL_VARIANT:
            return {str(name): str(value) for name, value in source_files.items()}
    raise RuntimeError("No canonical paper24 runtime manifest was found")


def materialize() -> None:
    expected = canonical_source_files()
    if DESTINATION.exists():
        actual = {
            str(path.relative_to(DESTINATION)): digest(path)
            for path in sorted(DESTINATION.rglob("*.py"))
        }
        if actual != expected:
            raise RuntimeError("Existing frozen paper24 runtime differs from canonical manifest")
        return

    temporary = DESTINATION.with_name(DESTINATION.name + ".tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    current = REPO / "src/spreadsheet_harness"
    for name, expected_hash in expected.items():
        candidates = [REFERENCE / name, current / name]
        source = next(
            (path for path in candidates if path.is_file() and digest(path) == expected_hash),
            None,
        )
        if source is None and name == "evolution.py":
            # The canonical file is exactly the pre-attribution-prompt version
            # of the current module.  The later prompt-only block is unrelated
            # to solver execution; reconstruct it and verify the manifest hash.
            text = (current / name).read_text(encoding="utf-8")
            block = (
                "Evidence roles are directional. ``repair`` is a failure of the accepted incumbent\n"
                "and is the only role that defines a new repair target. ``no-regression-constraint``\n"
                "and ``current-incumbent-no-regression-anchor`` are passing controls to preserve.\n"
                "``rejected-candidate-regression`` and ``rejected-candidate-failure-example`` show\n"
                "what a rejected proposal made worse; do not turn those failures into desired\n"
                "behavior or broaden the requested coordinate to fix them. A\n"
                "``successful-rejected-candidate-example`` may show a useful mechanism, but it does\n"
                "not override the accepted incumbent or the no-regression controls.\n\n"
            )
            reconstructed = text.replace(block, "")
            destination = temporary / name
            destination.write_text(reconstructed, encoding="utf-8")
            if digest(destination) != expected_hash:
                raise RuntimeError("Could not reconstruct canonical evolution.py")
            continue
        if source is None:
            raise RuntimeError(f"Canonical source is unavailable: {name} {expected_hash}")
        destination = temporary / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    actual = {
        str(path.relative_to(temporary)): digest(path)
        for path in sorted(temporary.rglob("*.py"))
    }
    if actual != expected:
        raise RuntimeError("Materialized paper24 runtime does not match canonical manifest")
    temporary.rename(DESTINATION)
    lock = {
        "schema_version": "paper24-frozen-runtime-source-v1",
        "canonical_source_variant": CANONICAL_VARIANT,
        "file_count": len(expected),
        "source_files": expected,
    }
    (DESTINATION.parent / "lock.json").write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    materialize()
    print(DESTINATION.parent / "lock.json")
