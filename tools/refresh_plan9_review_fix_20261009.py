#!/usr/bin/env python3
"""Refresh Plan9 revision identities after the read-only review fix."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

RELEASE = Path(__file__).resolve().parents[1] / "releases/fin15k-plan9-20261009"
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
    artifact_manifest = _tree_manifest(artifact)
    revision_sha256 = _sha256_json({
        "parent_revision_sha256": parent,
        "composition": composition,
        "resolved_composition_sha256": resolved.sha256,
        "artifact_manifest": artifact_manifest,
    })
    return {
        "schema_version": "continuous-plugin-revision-v1",
        "revision_sha256": revision_sha256,
        "parent_revision_sha256": parent,
        "composition": composition,
        "resolved_composition_sha256": resolved.sha256,
        "artifact_manifest_sha256": _sha256_json(artifact_manifest),
        "artifact_manifest": artifact_manifest,
        "kernel_manifest_sha256": _sha256_json(_kernel_manifest(artifact, registry)),
        "mutation": mutation,
    }


def main() -> None:
    old_baseline = read(RELEASE / "baseline/revision.json")
    baseline_artifact = RELEASE / "baseline/artifact"
    manifest = read(RELEASE / "candidate-manifest.json")
    baseline = revision(
        baseline_artifact,
        RELEASE / "baseline/composition.json",
        old_baseline.get("parent_revision_sha256"),
        old_baseline.get("mutation"),
    )
    write(RELEASE / "baseline/revision.json", baseline)
    changed = []
    for row in manifest["candidates"]:
        candidate = RELEASE / "candidates" / row["candidate_id"]
        previous = read(candidate / "revision.json")
        with tempfile.TemporaryDirectory(prefix="plan9-review-fix-") as temporary:
            artifact = Path(temporary) / "artifact"
            shutil.copytree(baseline_artifact, artifact)
            for source in (candidate / "overlay").rglob("*"):
                if source.is_file():
                    destination = artifact / source.relative_to(candidate / "overlay")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
            updated = revision(
                artifact,
                candidate / "composition.json",
                baseline["revision_sha256"],
                previous.get("mutation"),
            )
        write(candidate / "revision.json", updated)
        row.update({
            "revision_sha256": updated["revision_sha256"],
            "artifact_manifest_sha256": updated["artifact_manifest_sha256"],
            "kernel_manifest_sha256": updated["kernel_manifest_sha256"],
            "overlay_manifest": {
                path: digest
                for path, digest in updated["artifact_manifest"].items()
                if baseline["artifact_manifest"].get(path) != digest
            },
        })
        changed.append({
            "candidate_id": row["candidate_id"],
            "old_revision_sha256": previous["revision_sha256"],
            "new_revision_sha256": updated["revision_sha256"],
        })
    manifest.update({
        "baseline_revision_sha256": baseline["revision_sha256"],
        "baseline_artifact_manifest_sha256": baseline["artifact_manifest_sha256"],
    })
    manifest["review_verifier_fix"] = {
        "name": "inconclusive-read-only-review-preserves-clean-submit-v1",
        "affected_candidates": [item["candidate_id"] for item in changed],
        "changes": changed,
    }
    write(RELEASE / "candidate-manifest.json", manifest)
    print(json.dumps(manifest["review_verifier_fix"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
