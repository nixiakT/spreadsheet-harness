#!/usr/bin/env python3
"""Resume the four native proxy experiments and independently rescore artifacts.

Never changes original runs or benchmark answers. Task decisions are frozen
before inference; reruns are selected by missing/infrastructure state, not score.
Every task has an independent log, evaluator result and source provenance.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean

ROOT = Path(os.environ.get("NATIVE_BENCHMARK_REPO_ROOT", str(Path(__file__).resolve().parents[1])))
ARM = "spreadsheet-rl-native"
MODELS = {"deepseek": "DeepSeek-V4-Flash", "qwen480": "dashscope/qwen3-coder-480b-a35b-instruct"}
SLUGS = {"deepseek": "deepseek-v4-flash", "qwen480": "qwen3-coder-480b-a35b-instruct"}
DATASETS = {"v1": ROOT / "benchmarks/data/spreadsheetbench_912_v0.1",
            "v2": ROOT / "benchmarks/data/spreadsheetbench-v2"}
V1_EVALUATOR = ROOT / "benchmarks/vendor/spreadsheetbench1-official-49b73a9/evaluation.py"
V1_HASH = "4ae77cee8df01d1f34684fceab972810d696886533d33be2e89373de6b4d3de3"
V2_EVALUATOR = ROOT / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
V2_HASH = "04a2a75b29805ab40efe93e202384c365d1d32b9c924c1a4aed56e41249facb0"
API_KEY_FILE = Path("/tmp/spreadsheet-harness-litellm.key")
INFRA_ERRORS = {"ProviderError", "RenderError", "RecalculationIntegrityError", "ScoringInfrastructureError"}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def load(path):
    return json.loads(Path(path).read_text())


def tasks_for(version):
    if version == "v1":
        from spreadsheet_harness.spreadsheetbench_v1 import load_spreadsheetbench_v1
        return load_spreadsheetbench_v1(DATASETS[version])
    from spreadsheet_harness.spreadsheetbench_v2 import load_spreadsheetbench_v2_tasks
    return load_spreadsheetbench_v2_tasks(DATASETS[version], categories=("Debugging", "Financial_Model", "Template"))


def original_path(version, model):
    return ROOT / f"benchmarks/results/spreadsheetbench-{version}-{SLUGS[model]}-native-20260915"


def task_dir(root, job):
    return root / "tasks" / job["group"] / job["task_id"].replace("/", "__")


def initialize(root):
    if (root / "plan.json").exists():
        return load(root / "plan.json")
    for evaluator, expected in [(V1_EVALUATOR, V1_HASH), (V2_EVALUATOR, V2_HASH)]:
        if sha(evaluator) != expected:
            raise RuntimeError(f"Evaluator checksum mismatch: {evaluator}")
    jobs = []
    for version in ("v1", "v2"):
        tasks = tasks_for(version)
        for model in MODELS:
            source = original_path(version, model)
            rows = load(source / "results.json")
            group = f"{version}-{model}"
            # Snapshot files, not live references, freeze recovery decisions.
            write(root / "sources" / group / "results.json", rows)
            write(root / "sources" / group / "manifest.json", load(source / "manifest.json"))
            by_id = {r["task_id"]: r for r in rows}
            if len(by_id) != len(rows):
                raise RuntimeError(f"Duplicate original rows in {source}")
            for task in tasks:
                row = by_id.get(task.task_id)
                if not task.instruction.strip():
                    action, reason = "dataset_invalid", "empty_instruction_no_model_request"
                elif row is None:
                    action, reason = "generate", "unrecorded_or_interrupted"
                elif row.get("error_type") == "ProviderError":
                    action, reason = "generate", "provider_failure_including_scored_salvage"
                elif row.get("status") == "completed":
                    action, reason = "evaluate", "retain_completed_solution_regardless_of_score"
                elif row.get("run_dir") and (Path(row["run_dir"]) / ("case-1/artifacts/output.xlsx" if version == "v1" else "artifacts/output.xlsx")).is_file():
                    action, reason = "recover", "preserve_existing_solution_repair_execution_or_scoring"
                else:
                    action, reason = "generate", "no_recoverable_artifact"
                jobs.append({"group": group, "version": version, "model": model,
                             "task_id": task.task_id, "action": action, "reason": reason})
    plan = {"created_at": now(), "identity": "Spreadsheet-RL Tools+NativeHarness Linux/LibreOffice clean-room proxy; no RL",
            "v2_scope": "297 nonvisual tasks; Visualization excluded",
            "generation": {"max_turns": 50, "max_model_calls": 50, "temperature": 0, "top_p": 1,
                           "requested_thinking": True, "requested_reasoning_effort": "medium", "seed": 41,
                           "max_output_tokens": 32768, "max_total_tokens": 10000000,
                           "base_url": "http://10.130.138.46:8010/v1"},
            "images": "explicitly omitted for probed text-only routes; no alternate vision model",
            "evaluators": {"v1": {"path": str(V1_EVALUATOR), "sha256": V1_HASH},
                           "v2": {"path": str(V2_EVALUATOR), "sha256": V2_HASH}},
            "source_hashes": {str(p.relative_to(ROOT)): sha(p) for p in sorted((ROOT / "src/spreadsheet_harness").glob("*.py"))},
            "recovery_script_sha256": sha(__file__), "jobs": jobs}
    write(root / "plan.json", plan)
    return plan


def freeze_worker_sources(root):
    """Isolate long jobs from unrelated edits in the shared development tree."""
    frozen = root / "frozen-source"
    if not frozen.exists():
        frozen.mkdir()
        shutil.copytree(ROOT / "src/spreadsheet_harness", frozen / "spreadsheet_harness",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(__file__, frozen / "complete_native_benchmarks.py")
        write(frozen / "identity.json", {"created_at": now(), "files": {
            str(p.relative_to(frozen)): sha(p) for p in frozen.rglob("*.py")}})
    # Compact per-task row indexes avoid loading megabytes of request timings
    # for every individual evaluator process. Full snapshots remain untouched.
    for group in ("v1-deepseek", "v1-qwen480", "v2-deepseek", "v2-qwen480"):
        marker = root / "sources" / group / "index-complete.json"
        if marker.exists():
            continue
        for row in load(root / "sources" / group / "results.json"):
            compact = {k: v for k, v in row.items() if k != "agent"}
            write(root / "sources" / group / "rows" / (row["task_id"].replace("/", "__") + ".json"), compact)
        write(marker, {"finished_at": now()})
    return frozen


def copy_artifact(source, dest, provenance):
    from spreadsheet_harness.render import repair_mistyped_error_caches
    dest.parent.mkdir(parents=True, exist_ok=True)
    source_hash = sha(source)
    shutil.copy2(source, dest)
    repaired = repair_mistyped_error_caches(dest)
    if sha(source) != source_hash:
        raise RuntimeError("Original artifact changed during snapshot")
    provenance.append({"source": str(source), "source_sha256": source_hash,
                       "copy": str(dest), "copy_sha256": sha(dest), "mistyped_error_caches_repaired": repaired})
    return dest


def generate(job, directory):
    common = [sys.executable, "-m", "spreadsheet_harness.cli", "benchmark", f"{job['version']}-compare",
              "--dataset", str(DATASETS[job["version"]]), "--task-id", job["task_id"], "--arm", ARM,
              "--max-model-calls", "50", "--max-turns-per-arm", "50", "--max-total-tokens", "10000000",
              "--max-output-tokens", "32768", "--task-timeout", "21600", "--request-timeout", "600",
              "--litellm-timeout", "600", "--request-retries", "5", "--base-url", "http://10.130.138.46:8010/v1",
              "--api-key-file", str(API_KEY_FILE), "--model", MODELS[job["model"]],
              "--api-protocol", "chat-completions", "--reasoning-effort", "medium", "--seed", "41",
              "--temperature", "0", "--top-p", "1", "--enable-thinking"]
    if job["version"] == "v2":
        common += ["--category", job["task_id"].split("/")[0]]
    # Only infrastructure failures allow a second inference attempt. A normal
    # low score/model execution failure is never a trigger to sample again.
    for attempt in (1, 2):
        run = directory / f"generation-{attempt}"
        if not (run / "results.json").exists():
            if run.exists():
                raise RuntimeError(f"Interrupted generation requires inspection: {run}")
            with (directory / f"generation-{attempt}.log").open("w") as log:
                proc = subprocess.run(common + ["--output", str(run)], cwd=ROOT,
                                      stdout=log, stderr=subprocess.STDOUT, timeout=22000)
            if not (run / "results.json").exists():
                raise RuntimeError(f"Runner exited {proc.returncode} without results: {run}")
        row = load(run / "results.json")[0]
        if row.get("error_type") not in INFRA_ERRORS or attempt == 2:
            return row
    raise AssertionError("unreachable")


def v1_outputs(task, row, directory, action, provenance):
    from spreadsheet_harness.render import recalculate_workbook
    from spreadsheet_harness.spreadsheetbench_v1 import replay_v1_calls, successful_v1_replay_calls
    run = Path(row["run_dir"])
    calls = None
    # The prior extractor omitted mutating bash. Repair siblings only if the
    # frozen solution actually contains a successful bash mutation.
    trajectory = run / "case-1/trajectory.jsonl"
    if trajectory.is_file():
        try:
            calls = successful_v1_replay_calls(trajectory)
        except ValueError:
            if action == "recover":
                raise
    bash_replay = bool(calls and any(c["name"] == "bash" for c in calls))
    first_source = run / "case-1/artifacts/output.xlsx"
    outputs = {}
    for case in task.cases:
        if case.input_path is None or case.golden_path is None:
            continue
        source = run / f"case-{case.index}/artifacts/output.xlsx"
        replayed_sibling = False
        if case.index > 1 and (bash_replay or not source.is_file()):
            if calls is None:
                raise RuntimeError("Sibling replay lacks a complete frozen solution")
            replay_root = directory / f"sibling-replay-{case.index}"
            if (replay_root / "replay.json").exists():
                replay = load(replay_root / "replay.json")
            else:
                replay = replay_v1_calls(case.input_path, replay_root, calls, instruction=task.instruction)
                write(replay_root / "replay.json", replay)
            source = Path(replay["output_workbook"])
            replayed_sibling = True
            provenance.append({"sibling_replay": case.index, "bash_fix": bash_replay, "replay": replay})
        if not source.is_file():
            raise RuntimeError(f"No output for case {case.index}; first={first_source}")
        dest = copy_artifact(source, directory / "artifacts" / f"case-{case.index}.xlsx", provenance)
        needs_recalc = (action == "recover" and row.get("error_type") in {"RenderError", "RecalculationIntegrityError"}) or replayed_sibling
        if needs_recalc:
            recalc = recalculate_workbook(dest, dest, timeout_seconds=300)
            provenance.append({"recalculation": case.index, "metadata": recalc})
        outputs[case.index] = dest
    return outputs


def score_v1(task, outputs, log_path):
    if sha(V1_EVALUATOR) != V1_HASH:
        raise RuntimeError("v1 evaluator changed")
    spec = importlib.util.spec_from_file_location("native_v1_official", V1_EVALUATOR)
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    cases = []
    with log_path.open("w") as log, contextlib.redirect_stdout(log):
        for case in task.cases:
            output = outputs.get(case.index)
            if case.input_path is None or case.golden_path is None:
                passed, reason = False, "dataset_missing_case"
            elif output is None:
                raise RuntimeError("Inference/output missing; cannot silently score as zero")
            else:
                try:
                    passed, reason = evaluator.compare_workbooks(str(case.golden_path), str(output), task.instruction_type, task.answer_position)
                except Exception as exc:
                    # Exact official CLI evaluation() policy: exceptions are
                    # false, not a dropped case. Preserve diagnostics separately.
                    passed, reason = False, f"official_evaluator_exception:{type(exc).__name__}:{exc}"
            cases.append({"case_index": case.index, "passed": bool(passed), "reason": reason})
    return {"soft": sum(c["passed"] for c in cases) / 3,
            "hard": int(all(c["passed"] for c in cases)), "case_results": cases}


def work(root, job):
    from spreadsheet_harness.render import recalculate_workbook
    from spreadsheet_harness.spreadsheetbench_v2 import _load_official_evaluator, _official_score
    directory = task_dir(root, job)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "result.json").exists():
        return
    write(directory / "state.json", {"status": "running", "started_at": now(), "pid": os.getpid(), **job})
    provenance = []
    task = next(t for t in tasks_for(job["version"]) if t.task_id == job["task_id"])
    index = root / "sources" / job["group"] / "rows" / (job["task_id"].replace("/", "__") + ".json")
    if (index.parent.parent / "index-complete.json").exists():
        original = load(index) if index.exists() else None
    else:
        original = next((r for r in load(root / "sources" / job["group"] / "results.json") if r["task_id"] == job["task_id"]), None)
    try:
        if job["action"] == "dataset_invalid":
            result = {**job, "status": "dataset_invalid", "reason": "empty_instruction; no inference or fabricated instruction", "finished_at": now()}
            write(directory / "result.json", result)
            return
        row = generate(job, directory) if job["action"] == "generate" else original
        if row is None:
            raise RuntimeError("No source result")
        if row.get("error_type") == "ProviderError":
            raise RuntimeError(f"Provider remains unavailable: {row.get('error')}")
        outputs = {}
        if job["version"] == "v1":
            outputs = v1_outputs(task, row, directory, job["action"], provenance)
            score = score_v1(task, outputs, directory / "evaluator.log")
        else:
            source = Path(row["run_dir"]) / "artifacts/output.xlsx"
            artifact = copy_artifact(source, directory / "artifacts/output.xlsx", provenance)
            if row.get("status") != "completed":
                provenance.append({"recalculation": recalculate_workbook(artifact, artifact, cache_seed=task.input_path, timeout_seconds=300)})
            evaluator, _, evaluator_hash = _load_official_evaluator(V2_EVALUATOR)
            if evaluator_hash != V2_HASH:
                raise RuntimeError("v2 evaluator changed")
            with (directory / "evaluator.log").open("w") as log, contextlib.redirect_stdout(log):
                score = _official_score(evaluator, task, artifact, directory / "evaluation", model_calls=int(row.get("budget", {}).get("used", {}).get("model_calls", 0)))
            outputs = {1: artifact}
        result = {**job, "status": "scored", "finished_at": now(), "score": score,
                  "source_run_dir": row.get("run_dir"), "source_status": row.get("status"),
                  "source_error_type": row.get("error_type"), "source_outcome_kind": row.get("outcome_kind"),
                  "source_manifest_sha256": row.get("manifest_sha256"),
                  "evaluator_sha256": V1_HASH if job["version"] == "v1" else V2_HASH,
                  "original_manifest_sha256": original.get("manifest_sha256") if original else None,
                  "provenance": provenance,
                  "artifacts": {str(i): {"path": str(p), "sha256": sha(p)} for i, p in outputs.items()}}
    except Exception as exc:
        message = str(exc)
        if API_KEY_FILE.exists():
            message = message.replace(API_KEY_FILE.read_text().strip(), "[REDACTED]")
        result = {**job, "status": "unresolved", "finished_at": now(), "error_type": type(exc).__name__, "error": message, "provenance": provenance}
    write(directory / "result.json", result)


def report(root, plan):
    groups = {}
    for group in sorted({j["group"] for j in plan["jobs"]}):
        jobs = [j for j in plan["jobs"] if j["group"] == group]
        rows = [load(task_dir(root, j) / "result.json") for j in jobs if (task_dir(root, j) / "result.json").exists()]
        scored = [r for r in rows if r["status"] == "scored"]
        metrics = {}
        for key in (["soft", "hard"] if group.startswith("v1") else ["accuracy", "modification_accuracy", "regression_accuracy"]):
            values = [r["score"][key] for r in scored if isinstance(r["score"].get(key), (int, float))]
            metrics[key] = {"scored_mean": fmean(values) if values else None, "denominator": len(values),
                            "full_benchmark_mean": fmean(values) if len(values) == len(jobs) else None}
        groups[group] = {"expected": len(jobs), "processed": len(rows), "status_counts": dict(Counter(r["status"] for r in rows)),
                         "pending": len(jobs)-len(rows), "study_complete": len(scored) == len(jobs), "metrics": metrics}
        write(root / "reports" / group / "results.json", rows)
    summary = {"generated_at": now(), "identity": plan["identity"], "groups": groups,
               "all_jobs_processed": all(g["pending"] == 0 for g in groups.values()),
               "study_complete": all(g["study_complete"] for g in groups.values())}
    write(root / "summary.json", summary)
    return summary


def run(root, workers):
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    with (root / "supervisor.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = initialize(root)
        for job in plan["jobs"]:
            directory = task_dir(root, job)
            result_path = directory / "result.json"
            if result_path.exists():
                result = load(result_path)
                log = directory / "worker.log"
                if (result.get("error_type") == "WorkerProcessFailure" and log.exists()
                        and "No module named 'tomli'" in log.read_text()):
                    result_path.rename(directory / "launcher-interpreter-failure.json")
        frozen = freeze_worker_sources(root)
        environment = dict(os.environ, PYTHONPATH=str(frozen), NATIVE_BENCHMARK_REPO_ROOT=str(ROOT))
        # Separate queues ensure rescoring progresses while long inference runs.
        def dispatch(job):
            directory = task_dir(root, job)
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "worker.log").open("a") as log:
                completed = subprocess.run([sys.executable, str(frozen / "complete_native_benchmarks.py"), "worker", "--root", str(root),
                                       "--group", job["group"], "--task-id", job["task_id"]], cwd=ROOT,
                                      stdout=log, stderr=subprocess.STDOUT, env=environment)
            if completed.returncode and not (directory / "result.json").exists():
                write(directory / "result.json", {**job, "status": "unresolved", "finished_at": now(),
                      "error_type": "WorkerProcessFailure", "error": f"worker exited {completed.returncode}; see worker.log"})
            return completed.returncode
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as inference, concurrent.futures.ThreadPoolExecutor(max_workers=2) as evaluation:
            futures = []
            # Interleave groups so one large v1 does not starve the v2 queue.
            buckets = [[j for j in plan["jobs"] if j["group"] == g] for g in sorted({j["group"] for j in plan["jobs"]})]
            import itertools
            for bundle in itertools.zip_longest(*buckets):
                for job in bundle:
                    if job is None or (task_dir(root, job) / "result.json").exists():
                        continue
                    pool = inference if job["action"] == "generate" else evaluation
                    futures.append(pool.submit(dispatch, job))
            report(root, plan)
            pending = set(futures)
            while pending:
                _, pending = concurrent.futures.wait(pending, timeout=30, return_when=concurrent.futures.FIRST_COMPLETED)
                report(root, plan)
        summary = report(root, plan)
        write(root / "supervisor-finished.json", {"finished_at": now(), **summary})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["run", "worker", "report", "init"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--group")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.mode == "run":
        run(root, args.workers)
    elif args.mode == "init":
        p = initialize(root)
        print(dict(Counter((j["group"], j["action"]) for j in p["jobs"])))
    elif args.mode == "worker":
        import fcntl
        p = load(root / "plan.json")
        job = next(j for j in p["jobs"] if j["group"] == args.group and j["task_id"] == args.task_id)
        directory = task_dir(root, job)
        directory.mkdir(parents=True, exist_ok=True)
        # A manually verified canary may predate task locks; do not dispatch a
        # duplicate against its directory while that exact process is alive.
        state_path = directory / "state.json"
        if state_path.exists() and not (directory / "result.json").exists():
            state = load(state_path)
            pid = state.get("pid")
            if isinstance(pid, int) and pid != os.getpid():
                import time
                command_path = Path(f"/proc/{pid}/cmdline")
                while command_path.exists():
                    try:
                        command = command_path.read_bytes().decode(errors="replace")
                    except FileNotFoundError:
                        break
                    if "complete_native_benchmarks.py" not in command or job["task_id"] not in command:
                        break
                    time.sleep(5)
        with (directory / "worker.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            work(root, job)
    else:
        print(json.dumps(report(root, load(root / "plan.json")), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
