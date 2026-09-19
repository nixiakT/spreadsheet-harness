"""Read-only audit of the static V1 arms; never invokes a model or changes artifacts."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import fmean

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks/results"
COHORTS = {
    "deepseek": {
        "bare": "spreadsheetbench-v1-bare-deepseek-v4-flash-representative-200-internal-seed41-20260915",
        "basic": "ours-basic-deepseek-v1-20260917",
        "financial": "ours-deepseek-v1-run2-20260916",
    },
    "qwen": {
        "bare": "spreadsheetbench-v1-bare-qwen3-coder-480b-full-20260915-r3",
        "basic": "ours-basic-qwen-v1-20260917",
        "financial": "ours-qwen-v1-run2-20260916",
    },
}


def read_rows(root: Path) -> dict[str, dict]:
    paths = sorted(root.glob("workers/*/results.json"))
    paths += sorted(root.glob("shards/worker-*/results.json"))
    paths += sorted(root.glob("continuation-4w/tasks/*/results.json"))
    rows = {}
    for path in paths:
        for row in json.loads(path.read_text()):
            tid = str(row["task_id"])
            if tid in rows:
                raise ValueError(f"Duplicate task {tid}: explicit retry selection required")
            rows[tid] = row
    if not rows:
        raise ValueError(f"No results found: {root}")
    return rows


def scored(row: dict) -> bool:
    return row.get("status") == "completed" and all(
        isinstance(row.get(key), (int, float)) for key in ("soft", "hard")
    )


def case_pass(row: dict, index: int = 1):
    cases = row.get("case_results", [])
    return next((c.get("passed") for c in cases if c.get("case_index") == index), None)


def metrics(rows: list[dict]) -> dict:
    valid = [r for r in rows if scored(r)]
    return {
        "recorded": len(rows), "scored": len(valid),
        "soft_pct": 100 * fmean(r["soft"] for r in valid) if valid else None,
        "hard_pct": 100 * fmean(r["hard"] for r in valid) if valid else None,
        "case_pass_counts": [sum(case_pass(r, i) is True for r in valid) for i in (1, 2, 3)],
        "not_scored_errors": dict(Counter(r.get("error_type", "unknown") for r in rows if not scored(r))),
    }


def trajectory_metrics(rows: list[dict]) -> dict:
    counts = Counter()
    examples = {}
    for row in rows:
        if not scored(row):
            continue
        path = Path(row["run_dir"]) / "case-1/trajectory.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        counts["trajectories"] += 1
        selected = [s for e in events if e["event"] == "harness.skills.routed"
                    for s in e.get("payload", {}).get("selected", [])]
        counts["financial_skill_selected"] += int("spreadsheet-financial-model" in selected)
        verified = [e for e in events if e["event"] == "harness.planner_actions.verified"]
        stages = [s["name"] for s in row.get("agent", {}).get("stages", [])]
        no_executor = bool(verified) and "execute" not in stages
        escaped = any("\\u003c" in str(e["payload"].get("actions", [])) or
                      "\\u003e" in str(e["payload"].get("actions", [])) for e in verified)
        for name, hit in (("verified_without_executor", no_executor), ("escaped_verified_actions", escaped)):
            counts[name] += int(hit)
            counts[name + "_case1_failed"] += int(hit and case_pass(row) is False)
            if hit:
                examples.setdefault(name, []).append(row["task_id"])
    return {"counts": dict(counts), "example_task_ids": examples}


def main() -> None:
    report = {}
    for model, roots in COHORTS.items():
        groups = {arm: read_rows(RESULTS / name) for arm, name in roots.items()}
        common = sorted(set.intersection(*(set(g) for g in groups.values())))
        common_scored = [tid for tid in common if all(scored(g[tid]) for g in groups.values())]
        report[model] = {
            "roots": roots,
            "all_recorded": {a: metrics(list(g.values())) for a, g in groups.items()},
            "common_scored_count": len(common_scored),
            "common_scored": {a: metrics([g[t] for t in common_scored]) for a, g in groups.items()},
            "trajectories": {a: trajectory_metrics(list(g.values())) for a, g in groups.items() if a != "bare"},
        }
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
