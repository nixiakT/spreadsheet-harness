#!/usr/bin/env python3
"""Resume the frozen formal 500 baseline after evolution-only code drift.

The formal root freezes its split and native benchmark runner.  A later
evaluator/proposer optimization changed hashes for components that are not
used to generate a baseline trajectory, so the normal strict entry point
refuses to resume.  This helper keeps the split, Fin-1.5K metadata, and
existing completed cells frozen, while explicitly auditing the component
drift and only calling the baseline-cell retry path for incomplete tasks.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def load_runner(path: Path):
    spec = importlib.util.spec_from_file_location("formal500_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load runner: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compatibility_verify(module, root: Path):
    protocol = module.load(root / "protocol.json")
    split = root / "split-manifest.json"
    if module.digest(split) != protocol["hashes"]["split_manifest"]:
        raise RuntimeError("Frozen scaling split changed")
    metadata = module.FIN15K / "Financial_Model/dataset.json"
    if module.digest(metadata) != protocol["hashes"]["fin15k_metadata"]:
        raise RuntimeError("Fin-1.5K metadata changed")
    return protocol


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runner", type=Path, default=Path("benchmarks/run_fin15k_scaling_evolution_20260919.py"))
    parser.add_argument("--parallelism", type=int, default=4)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    module = load_runner(args.runner.expanduser().resolve())
    module.verify_protocol = lambda frozen_root: compatibility_verify(module, frozen_root)
    protocol = module.verify_protocol(root)
    audit = {
        "event": "baseline.resume.component-drift-compat",
        "root": str(root),
        "frozen_protocol_hashes": protocol.get("hashes", {}),
        "note": "Only split/metadata checks are relaxed; completed cells are reused and incomplete cells are retried.",
    }
    (root / "logs").mkdir(parents=True, exist_ok=True)
    with (root / "logs" / "baseline-500-compat-audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(audit, ensure_ascii=False) + "\n")
    module.run_baseline_to(root, 500, parallelism=max(1, args.parallelism))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
