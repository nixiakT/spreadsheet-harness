from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks/results/v2-sota-matrix-20260926-r12"
PYTHON = ROOT / ".venv/bin/python"
RUNNER = ROOT / "benchmarks/run_v2_target_category_parallel_20260925.py"


def failed_tasks() -> list[tuple[str, str, str, str, list[str]]]:
    groups: dict[tuple[str, str, str, str], list[str]] = {}
    for summary in OUT.glob("*/*/*/*/*/summary.json"):
        rel = summary.relative_to(OUT).parts
        slug, arm, category, item_id = rel[:4]
        try:
            payload = json.loads(summary.read_text(encoding="utf-8"))
            arm_data = next(iter(payload.get("arms", {}).values()))
        except (OSError, ValueError, StopIteration, TypeError):
            continue
        if arm_data.get("completed") == 1 and arm_data.get("errors", 0) == 0:
            continue
        groups.setdefault((slug, arm, category, item_id), [])
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for slug, arm, category, item_id in groups:
        grouped.setdefault((slug, arm, category), []).append(item_id)
    return [(slug, arm, category, ids) for (slug, arm, category), ids in grouped.items()]


def run(spec: tuple[str, str, str, list[str]]) -> dict[str, object]:
    slug, arm, category, ids = spec
    log = OUT / "rerun-logs" / f"{slug}__{arm}__{category}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(PYTHON), str(RUNNER),
        "--category", category, "--arm", arm,
        "--model", f"dashscope/{slug}", "--model-slug", slug,
        "--output", str(OUT), "--parallelism", "6",
        "--task-ids", *sorted(ids),
        "--base-url", "http://10.130.138.46:8010/v1",
        "--api-key-file", "/tmp/spreadsheet-harness-litellm.key",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    with log.open("a", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    return {"slug": slug, "arm": arm, "category": category, "count": len(ids), "returncode": result.returncode}


specs = failed_tasks()
(OUT / "rerun-specs.json").write_text(json.dumps(specs, ensure_ascii=False, indent=2), encoding="utf-8")
with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, max(1, len(specs)))) as pool:
    for result in pool.map(run, specs):
        print(json.dumps(result, ensure_ascii=False), flush=True)
