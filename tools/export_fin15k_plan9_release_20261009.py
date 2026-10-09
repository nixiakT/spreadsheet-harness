#!/usr/bin/env python3
"""Export immutable candidates as one baseline plus hash-verified file overlays.

Only allowlisted source/metadata is exported. No development traces, benchmark
answers, provider logs, keys, bytecode, or mutable workspace source are copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode()).hexdigest()


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def copy(source: Path, target: Path, expected: str | None = None) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Not a regular source file: {source}")
    if expected and hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise ValueError(f"Frozen source checksum mismatch: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def export(bundle: Path, output: Path, repository: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    manifest = read(bundle / "candidate-manifest.json")
    baseline = read(bundle / "baseline/revision.json")
    for name in ("composition.json", "revision.json"):
        copy(bundle / "baseline" / name, output / "baseline" / name)
    for relative, sha in baseline["artifact_manifest"].items():
        copy(bundle / "baseline/artifact" / relative,
             output / "baseline/artifact" / relative, sha)
    rows = []
    for original in manifest["candidates"]:
        row = dict(original)
        directory = bundle / "materialization/candidates" / Path(row["candidate_dir"]).name
        revision = read(directory / "revision.json")
        destination = output / "candidates" / row["candidate_id"]
        for name in ("composition.json", "revision.json", "proposal.json",
                     "proposal-digest.json", "plan-route.json"):
            copy(directory / name, destination / name)
        overlay = {}
        for relative, sha in revision["artifact_manifest"].items():
            if baseline["artifact_manifest"].get(relative) != sha:
                copy(directory / "artifact" / relative, destination / "overlay" / relative, sha)
                overlay[relative] = sha
        row["overlay_manifest"] = overlay
        row["deleted_files"] = sorted(set(baseline["artifact_manifest"]) - set(revision["artifact_manifest"]))
        size, mode = row["evidence_size"], row["mechanism"]
        original_request = read(bundle / f"size-{size}/{mode}/request.json")
        # Retain frozen evidence constraints, not private trace paths/contents.
        request = {key: original_request[key] for key in
                   ("schema_version", "base_revision_sha256", "proposal_constraints")}
        request["source_request_sha256"] = row["request_sha256"]
        row["source_request_sha256"] = row["request_sha256"]
        row["request_sha256"] = digest(request)
        row["candidate_dir"] = f"materialization/candidates/{directory.name}"
        row["artifact"] = row["candidate_dir"] + "/artifact"
        row["response"] = f"size-{size}/{mode}/response.json"
        write(output / f"size-{size}/{mode}/request.json", request)
        copy(bundle / f"size-{size}/{mode}/response.json", output / row["response"])
        rows.append(row)
    manifest["candidates"] = rows
    manifest["distribution"] = "baseline-plus-exact-source-overlays-v1"
    manifest["source_candidate_manifest_sha256"] = hashlib.sha256(
        (bundle / "candidate-manifest.json").read_bytes()).hexdigest()
    write(output / "candidate-manifest.json", manifest)
    copy(bundle / "CANDIDATES.md", output / "CANDIDATES.md")
    for source, name in (
        ("benchmarks/run_fin15k_plugin_plan9_v2_20261009.py", "scheduler.py"),
        ("tools/verify_fin15k_plugin_candidates_20261009.py", "verify.py"),
        ("tools/probe_fin15k_plan9_live_20261009.py", "probe.py"),
    ):
        copy(repository / source, output / "runtime" / name)
    print(json.dumps({"release": str(output), "candidates": len(rows),
                      "baseline_revision": baseline["revision_sha256"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    args = parser.parse_args()
    export(args.bundle.resolve(), args.output.resolve(), args.repository.resolve())
