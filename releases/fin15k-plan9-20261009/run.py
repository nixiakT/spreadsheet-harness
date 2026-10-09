#!/usr/bin/env python3
"""Portable entrypoint; always import the frozen baseline, never checkout src.

materialize reconstructs exact revisions from a shared baseline and overlays.
verify is offline. canary calls the selected provider on an artificial task.
run evaluates all nine candidates and baseline; status is read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import shutil
import sys
from pathlib import Path

RELEASE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def contained(root: Path, relative: str) -> Path:
    value = Path(relative)
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise ValueError(f"Unsafe release path: {relative}")
    target = root / value
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise ValueError(f"Release paths must not contain symlinks: {relative}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Release path escaped its root: {relative}")
    return target


def check_tree(root: Path, manifest: dict[str, str]) -> None:
    actual = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink in frozen source: {path}")
        if path.is_file():
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                raise ValueError(f"Bytecode cache in frozen release: {path}")
            actual[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != manifest:
        raise ValueError(f"Frozen file inventory/checksum differs: {root}")


def bootstrap() -> None:
    baseline = read(RELEASE / "baseline/revision.json")
    check_tree(RELEASE / "baseline/artifact", baseline["artifact_manifest"])
    sys.path.insert(0, str(RELEASE / "baseline/artifact/src"))
    from spreadsheet_harness import continuous_evolution
    if not Path(continuous_evolution.__file__).resolve().is_relative_to(RELEASE / "baseline/artifact/src"):
        raise ValueError("Entrypoint imported a non-frozen harness")


def materialize(argv: list[str]) -> int:
    from spreadsheet_harness.continuous_evolution import RevisionStore, _sha256_json
    parser = argparse.ArgumentParser(description="Reconstruct exact frozen artifacts without model calls")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if output.is_relative_to(RELEASE):
        parser.error("Output must be outside the published release")
    # Never mutate a prior bundle or remove partial output after an error.
    output.mkdir(parents=True, exist_ok=False)
    manifest = read(RELEASE / "candidate-manifest.json")
    baseline = read(RELEASE / "baseline/revision.json")
    shutil.copytree(RELEASE / "baseline", output / "baseline")
    store = RevisionStore(output / "materialization")
    store.verify_materialized_candidate(output / "baseline", baseline)
    for row in manifest["candidates"]:
        source = contained(RELEASE, "candidates/" + row["candidate_id"])
        destination = contained(output, row["candidate_dir"])
        revision = read(source / "revision.json")
        if revision["revision_sha256"] != row["revision_sha256"]:
            raise ValueError("Published candidate revision differs from its manifest")
        expected_overlay = {
            path: sha for path, sha in revision["artifact_manifest"].items()
            if baseline["artifact_manifest"].get(path) != sha
        }
        if expected_overlay != row["overlay_manifest"]:
            raise ValueError("Overlay inventory differs from frozen candidate revision")
        check_tree(source / "overlay", expected_overlay)
        shutil.copytree(output / "baseline/artifact", destination / "artifact")
        for relative in row["deleted_files"]:
            contained(destination / "artifact", relative).unlink()
        for relative in expected_overlay:
            target = contained(destination / "artifact", relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(contained(source / "overlay", relative), target)
        for name in ("composition.json", "revision.json", "proposal.json",
                     "proposal-digest.json", "plan-route.json"):
            shutil.copyfile(source / name, destination / name)
        size, mode = row["evidence_size"], row["mechanism"]
        request = read(RELEASE / f"size-{size}/{mode}/request.json")
        if _sha256_json(request) != row["request_sha256"]:
            raise ValueError("Portable request checksum mismatch")
        store.verify_materialized_candidate(destination, revision)
    for size in (50, 200, 500):
        shutil.copytree(RELEASE / f"size-{size}", output / f"size-{size}")
    for name in ("candidate-manifest.json", "CANDIDATES.md"):
        shutil.copyfile(RELEASE / name, output / name)
    print(json.dumps({"status": "materialized-exact-frozen-revisions", "bundle": str(output),
                      "candidates": 9, "baseline": baseline["revision_sha256"], "model_calls": 0}))
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(__doc__)
        print("Usage: python run.py {materialize,verify,canary,run,status} [options]")
        return 0
    command, argv = sys.argv[1], sys.argv[2:]
    if command not in {"materialize", "verify", "canary", "run", "status"}:
        raise ValueError("Unknown release command: " + command)
    bootstrap()
    if command == "materialize":
        return materialize(argv)
    filename = {"verify": "verify.py", "canary": "probe.py",
                "run": "scheduler.py", "status": "scheduler.py"}[command]
    if command == "verify":
        # The verifier takes the materialized bundle as its positional argument.
        parser = argparse.ArgumentParser()
        parser.add_argument("--bundle", type=Path, required=True)
        parser.add_argument("--write", action="store_true")
        parsed = parser.parse_args(argv)
        sys.argv = [str(RELEASE / "runtime" / filename), str(parsed.bundle),
                    *( ["--write"] if parsed.write else [] )]
    else:
        sys.argv = [str(RELEASE / "runtime" / filename),
                    *( ["--status"] if command == "status" else [] ), *argv]
    runpy.run_path(sys.argv[0], run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
