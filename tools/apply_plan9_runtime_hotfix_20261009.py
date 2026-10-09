#!/usr/bin/env python3
"""Apply one audited kernel hotfix to every frozen Plan9 artifact.

The operation creates a new release identity: every artifact, revision,
candidate manifest and overlay is updated together. It never weakens strict
isolation; only require_isolation=False stops opportunistically invoking bwrap.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

RELEASE = Path(__file__).resolve().parents[1] / "releases/fin15k-plan9-20261009"
OLD_BASELINE = "522324bf96fe720993ab1b45ae7d533463e179885040f51220fa5692108c9dbd"
OLD_SOURCE = "8f33723e1173e92114378f838312977dfd381700b145fae4963bcae4499a05a6"
sys.path.insert(0, str(RELEASE / "baseline/artifact/src"))

from spreadsheet_harness.continuous_evolution import _kernel_manifest, _sha256_json, _tree_manifest
from spreadsheet_harness.plugins import CompositionSpec, load_candidate_plugin_registry


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def revision(artifact: Path, composition_path: Path, parent: str | None, mutation):
    composition = read(composition_path)
    registry = load_candidate_plugin_registry(artifact)
    resolved = registry.resolve(CompositionSpec.create(
        composition["name"], composition["plugins"], composition.get("overrides")
    ))
    manifest = _tree_manifest(artifact)
    revision_sha = _sha256_json({
        "parent_revision_sha256": parent,
        "composition": composition,
        "resolved_composition_sha256": resolved.sha256,
        "artifact_manifest": manifest,
    })
    return {
        "schema_version": "continuous-plugin-revision-v1",
        "revision_sha256": revision_sha,
        "parent_revision_sha256": parent,
        "composition": composition,
        "resolved_composition_sha256": resolved.sha256,
        "artifact_manifest_sha256": _sha256_json(manifest),
        "artifact_manifest": manifest,
        "kernel_manifest_sha256": _sha256_json(_kernel_manifest(artifact, registry)),
        "mutation": mutation,
    }


def main() -> None:
    source = Path(sys.argv[1]).resolve()
    relative = "src/spreadsheet_harness/code_interpreter.py"
    baseline_artifact = RELEASE / "baseline/artifact"
    target = baseline_artifact / relative
    old_source_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    if old_source_sha != OLD_SOURCE:
        raise ValueError("Release is not at the expected pre-hotfix source identity")
    shutil.copyfile(source, target)
    new_source_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    old_baseline = read(RELEASE / "baseline/revision.json")
    baseline = revision(baseline_artifact, RELEASE / "baseline/composition.json", None, None)
    write(RELEASE / "baseline/revision.json", baseline)
    manifest = read(RELEASE / "candidate-manifest.json")
    for row in manifest["candidates"]:
        candidate = RELEASE / "candidates" / row["candidate_id"]
        old = read(candidate / "revision.json")
        # Revision needs the full tree. Recompute from the hotfixed baseline
        # plus the candidate's existing plugin-owned overlay.
        scratch = candidate / ".hotfix-artifact"
        shutil.copytree(baseline_artifact, scratch)
        for path in (candidate / "overlay").rglob("*"):
            if path.is_file():
                destination = scratch / path.relative_to(candidate / "overlay")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        updated = revision(scratch, candidate / "composition.json",
                           baseline["revision_sha256"], old.get("mutation"))
        shutil.rmtree(scratch)
        write(candidate / "revision.json", updated)
        row.update({
            "revision_sha256": updated["revision_sha256"],
            "artifact_manifest_sha256": updated["artifact_manifest_sha256"],
            "kernel_manifest_sha256": updated["kernel_manifest_sha256"],
            "overlay_manifest": {
                path: digest for path, digest in updated["artifact_manifest"].items()
                if baseline["artifact_manifest"].get(path) != digest
            },
        })
    manifest.update({
        "baseline_revision_sha256": baseline["revision_sha256"],
        "baseline_artifact_manifest_sha256": baseline["artifact_manifest_sha256"],
        "runtime_hotfix": {
            "name": "trusted-execution-skips-bwrap-v1",
            "source": relative,
            "old_source_sha256": old_source_sha,
            "new_source_sha256": new_source_sha,
            "old_baseline_revision_sha256": old_baseline["revision_sha256"],
            "strict_isolation_unchanged": True,
        },
    })
    if manifest["runtime_hotfix"]["old_baseline_revision_sha256"] != OLD_BASELINE:
        raise ValueError("Release is not at the expected pre-hotfix baseline identity")
    write(RELEASE / "candidate-manifest.json", manifest)
    print(json.dumps(manifest["runtime_hotfix"], indent=2))


if __name__ == "__main__":
    main()
