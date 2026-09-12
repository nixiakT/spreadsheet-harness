#!/usr/bin/env python3
"""Merge primary/continuation paper-reproduction trees without hiding failures."""
from __future__ import annotations
import json, pathlib, statistics
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks/results/spreadsheetbench-v2-glm52-nothinking-30-4arm-20260904/manifest.json"
OUT = ROOT / "benchmarks/results/paper_reproduction_v2_30_summary_20260905.json"

METHODS = {
    "spreadsheet_rl_thinking_official_checkpoint": [
        ROOT / "benchmarks/results/spreadsheet-rl-4b-v2-30-native-20260905-official-checkpoint-20260905",
        ROOT / "benchmarks/results/spreadsheet-rl-4b-v2-30-native-parallel-pending-20260905",
        ROOT / "benchmarks/results/spreadsheet-rl-4b-v2-30-native-rl-thinking-continuation-20260905-20260905",
    ],
    "spreadsheet_rl_no_thinking_supplement": [
        ROOT / "benchmarks/results/spreadsheet-rl-4b-v2-30-nothinking-official-checkpoint-20260905",
    ],
    "spreadsheetagent_paper_vision_clean_room_proxy": [
        ROOT / "benchmarks/results/spreadsheetagent-paper-vision-proxy-qwen36-v2-30-20260905",
        ROOT / "benchmarks/results/spreadsheetagent-paper-vision-proxy-parallel-pending-20260905",
        ROOT / "benchmarks/results/spreadsheetagent-paper-vision-proxy-paper-vision-continuation-20260905",
    ],
    "spreadsheetagent_output_limit_recovery_supplement": [
        ROOT / "benchmarks/results/spreadsheetagent-paper-vision-output-recovery-20260905",
    ],
    "spreadsheetagent_thinking_recovery_supplement": [
        ROOT / "benchmarks/results/spreadsheetagent-paper-vision-thinking-recovery-20260905",
    ],
    "sheetcompass_graph_memory_clean_room_proxy": [
        ROOT / "benchmarks/results/sheetcompass-graph-memory-proxy-qwen36-v2-30-20260905",
        ROOT / "benchmarks/results/sheetcompass-graph-memory-proxy-parallel-pending-20260905",
        ROOT / "benchmarks/results/sheetcompass-graph-memory-proxy-parallel2-20260905",
        ROOT / "benchmarks/results/sheetcompass-debug-retry-20260905",
        ROOT / "benchmarks/results/sheetcompass-graph-memory-proxy-compass-continuation-20260905-20260905",
    ],
    "trace2skill_model_substitution": [
        ROOT / "benchmarks/results/trace2skill-v2-qwen36-35b-a3b-30-skill-preloaded-20260905",
    ],
    "trace2skill_qwen36_envfixed_model_substitution": [
        ROOT / "benchmarks/results/trace2skill-v2-qwen36-35b-a3b-30-envfixed-20260905",
        ROOT / "benchmarks/results/trace2skill-v2-qwen36-35b-a3b-30-compatfix-20260905",
    ],
    "trace2skill_coderplus_model_substitution_supplement": [
        ROOT / "benchmarks/results/trace2skill-v2-dashscope-qwen3-coder-plus-30-envfixed-20260905",
        ROOT / "benchmarks/results/trace2skill-v2-dashscope-qwen3-coder-plus-30-supplement-20260905",
        ROOT / "benchmarks/results/trace2skill-v2-dashscope-qwen3-coder-plus-targeted-20260905",
    ],
}

TRACE_LOG_ROOTS = {
    "trace2skill_qwen36_envfixed_model_substitution": ROOT / "tmp/trace2skill_qwen36_compatfix_logs_20260905",
    "trace2skill_qwen36_envfixed_model_substitution": ROOT / "tmp/trace2skill_qwen36_envfixed_logs_20260905",
    "trace2skill_coderplus_model_substitution_supplement": ROOT / "tmp/trace2skill_coderplus_envfixed_logs_20260905",
}

def rows_from_tree(tree: pathlib.Path):
    direct = tree / "runner_results.json"
    if direct.exists():
        try:
            payload = json.loads(direct.read_text())
            rows = payload if isinstance(payload, list) else payload.get("results", [])
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and "task_id" not in row and "id" in row:
                        row = dict(row)
                        row["task_id"] = row["id"]
                    if isinstance(row, dict) and "status" not in row and "success" in row:
                        row = dict(row)
                        row["status"] = "completed"
                        row["passed"] = bool(row.get("success"))
                        if not row.get("success"):
                            row["model_failure_reason"] = "trace2skill_task_unsuccessful"
                    yield row
        except Exception:
            pass
    for f in tree.glob("**/results.json"):
        try:
            payload = json.loads(f.read_text())
        except Exception:
            continue
        rows = payload if isinstance(payload, list) else payload.get("results", [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and "task_id" not in row and "id" in row:
                    row = dict(row)
                    row["task_id"] = row["id"]
                if isinstance(row, dict) and "status" not in row and "success" in row:
                    row = dict(row)
                    row["status"] = "completed"
                    row["passed"] = bool(row.get("success"))
                    if not row.get("success"):
                        row["model_failure_reason"] = "trace2skill_task_unsuccessful"
                yield row

def choose(rows):
    # Prefer completed/scored rows; otherwise retain the most informative error.
    by_task = {}
    for row in rows:
        task = row.get("task_id") or row.get("instance_id")
        if not task:
            continue
        old = by_task.get(task)
        rank = (row.get("status") == "completed", row.get("outcome_kind") == "scored")
        old_rank = ((old or {}).get("status") == "completed", (old or {}).get("outcome_kind") == "scored")
        if old is None or rank > old_rank:
            by_task[task] = row
    return by_task

def summarize(rows, expected):
    chosen = choose(rows)
    completed = [r for r in chosen.values() if r.get("status") == "completed"]
    scores = [r.get("official_score", {}) for r in completed]
    def avg(name):
        vals = [s[name] for s in scores if isinstance(s.get(name), (int, float))]
        return round(statistics.fmean(vals), 6) if vals else None
    errors = [r for r in chosen.values() if r.get("status") != "completed"]
    error_types = {}
    for r in errors:
        k = r.get("error_type") or r.get("model_failure_reason") or r.get("status") or "unknown"
        error_types[k] = error_types.get(k, 0) + 1
    failures = [r for r in chosen.values() if r.get("model_failure_reason") or r.get("error_type") in {"AgentExecutionFailure", "ProviderError", "PaperStageValidationError"}]
    timeout_count = sum("timeout" in str(r.get("error_type", "")).lower() or "timeout" in str(r.get("model_failure_reason", "")).lower() or "timeout" in str(r.get("error", "")).lower() for r in chosen.values())
    return {
        "expected": expected,
        "unique_cases": len(chosen),
        "completed": len(completed),
        "errors_or_incomplete": len(errors),
        "model_execution_failures": len(failures),
        "timeout_count": timeout_count,
        "pass_count": sum(bool(r.get("passed")) for r in completed),
        "task_success_count": sum(bool(r.get("success")) for r in completed) if any("success" in r for r in completed) else None,
        "pass_rate_over_completed": round(sum(bool(r.get("passed")) for r in completed) / len(completed), 6) if completed else None,
        "modification_accuracy_mean": avg("modification_accuracy"),
        "regression_accuracy_mean": avg("regression_accuracy"),
        "accuracy_mean": avg("accuracy"),
        "error_types": error_types,
        "rows": sorted(chosen.values(), key=lambda r: r.get("task_id", "")),
    }

def trace_log_stats(root: pathlib.Path):
    files = list(root.glob("**/*.md")) if root.exists() else []
    result_files = [f for f in files if "## RESULT" in f.read_text(errors="ignore")]
    success = sum("Success: True" in f.read_text(errors="ignore") for f in result_files)
    failure = sum("Success: False" in f.read_text(errors="ignore") for f in result_files)
    return {"log_files": len(files), "task_results_in_logs": len(result_files),
            "task_success_in_logs": success, "task_failure_in_logs": failure}

def main():
    manifest = json.loads(MANIFEST.read_text())
    expected = [r["task_id"] for r in manifest["tasks"]]
    out = {"manifest": str(MANIFEST), "expected_cases": expected, "methods": {}}
    for name, trees in METHODS.items():
        rows = [r for t in trees for r in rows_from_tree(t) if (r.get("task_id") in expected)]
        out["methods"][name] = summarize(rows, len(expected))
        if name in TRACE_LOG_ROOTS:
            out["methods"][name]["trace_log_stats"] = trace_log_stats(TRACE_LOG_ROOTS[name])
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    for name, s in out["methods"].items():
        print(f"{name}: {s['completed']}/{s['expected']} completed; pass={s['pass_count']}; mod_acc={s['modification_accuracy_mean']}")
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
