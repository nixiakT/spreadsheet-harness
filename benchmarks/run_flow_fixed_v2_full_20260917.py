"""Run both repaired harness arms from the tested snapshot; never overwrite attempts."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
REFERENCE = REPO / "benchmarks/results/financial-flow-paired-20260917"
DEFAULT_ROOT = REPO / "benchmarks/results/flow-fixed-basic-financial-v2-full-20260917"
DATASET = REPO / "benchmarks/data/spreadsheetbench-v2"
VISUAL = REPO / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
VISUAL_SCRIPT = Path("/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py")


def hashes(root, pattern):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob(pattern)) if p.is_file() and "__pycache__" not in p.parts}


def jobs():
    plan = []
    for category, expected in (("Financial_Model", 100), ("Debugging", 100),
                               ("Template", 97), ("Visualization", 24)):
        rows = json.loads((DATASET / category / "dataset.json").read_text())
        if len(rows) != expected:
            raise ValueError(f"Unexpected dataset size: {category}")
        for row in rows:
            for arm in ("basic", "financial"):
                plan.append((arm, category, str(row["id"])))
    assert len(plan) == 642 and len(set(plan)) == 642
    return plan


def command(root, job):
    arm, category, case = job
    output = root / arm / "tasks" / f"{category}__{case}"
    args = [str(REPO / ".venv/bin/python"), "-m", "spreadsheet_harness.cli", "benchmark"]
    if category == "Visualization":
        args += ["v2-visual-generate", "--visual-evaluator", str(VISUAL_SCRIPT)]
    else:
        args += ["v2-compare", "--category", category]
    return args + [
        "--dataset", str(DATASET), "--task-id", f"{category}/{case}",
        "--arm", "ours", "--composition", f"ours=spreadsheet-harness-{arm}",
        "--skill-root", str(root / "skills"), "--output", str(output),
        "--max-model-calls", "50", "--max-turns-per-arm", "50",
        "--max-total-tokens", "10000000", "--max-output-tokens", "32768",
        "--task-timeout", "21600", "--arm-order-seed", "20260908",
        "--base-url", "http://47.96.153.159:8010/v1",
        "--api-key-file", "/tmp/spreadsheet-harness-litellm.key",
        "--model", "dashscope/deepseek-v4-flash", "--api-protocol", "chat-completions",
        "--reasoning-effort", "medium", "--temperature", "0", "--top-p", "1",
        "--request-timeout", "700", "--litellm-timeout", "600", "--request-retries", "5",
        "--enable-thinking",
    ]


def prepare(root, plan, workers):
    # Verify the tested candidate before copying; do not use mutable live source.
    experiment = json.loads((REFERENCE / "experiment.json").read_text())
    expected = experiment["source_sha256"]["candidate"]
    candidate = REFERENCE / "candidate"
    actual = {"spreadsheet_harness/" + k: v
              for k, v in hashes(candidate / "spreadsheet_harness", "*.py").items()}
    if actual != expected:
        raise ValueError("Tested candidate snapshot changed")
    root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(candidate / "spreadsheet_harness", root / "source/spreadsheet_harness",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(REFERENCE / "skills", root / "skills",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (root / "source/benchmarks").symlink_to(REPO / "benchmarks", target_is_directory=True)
    manifest = {
        "reference": str(REFERENCE), "jobs": plan, "workers": workers,
        "worker_history": [{"workers": workers, "started_at": datetime.now(timezone.utc).isoformat()}],
        "source_sha256": actual, "skills_sha256": hashes(root / "skills", "*"),
        "dataset_sha256": {cat: hashlib.sha256((DATASET / cat / "dataset.json").read_bytes()).hexdigest()
                            for cat in {j[1] for j in plan}},
        "evaluator_sha256": hashlib.sha256(VISUAL.read_bytes()).hexdigest(),
        "visual_evaluator_sha256": hashlib.sha256(VISUAL_SCRIPT.read_bytes()).hexdigest(),
        "sample_command": command(root, plan[0]),
        "policy": "fresh single attempt per case; retain failures; no best-of or result pooling",
        "visualization": "generation only; official visual scoring still pending",
    }
    (root / "experiment.json").write_text(json.dumps(manifest, indent=2))


def resume(root, plan, workers):
    """Validate the frozen experiment and recover results not yet journalled."""
    manifest_path = root / "experiment.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest["jobs"] != [list(job) for job in plan]:
        raise ValueError("Resume plan differs from the frozen experiment")
    actual = {"spreadsheet_harness/" + k: v
              for k, v in hashes(root / "source/spreadsheet_harness", "*.py").items()}
    if actual != manifest["source_sha256"]:
        raise ValueError("Frozen experiment source changed")

    records = []
    for status_path in (root / "status.jsonl", root / "accelerator-status.jsonl"):
        if status_path.exists():
            records.extend(json.loads(line) for line in status_path.read_text().splitlines()
                           if line.strip())
    completed = {(row["arm"], row["category"], row["case"]) for row in records}
    recovered = []
    for arm, category, case in plan:
        job = (arm, category, case)
        if job in completed:
            continue
        result_path = root / arm / "tasks" / f"{category}__{case}" / "results.json"
        if not result_path.exists():
            continue
        row = json.loads(result_path.read_text())[0]
        recovered.append({
            "arm": arm, "category": category, "case": case,
            "exit_code": 0, "outcome_kind": row.get("outcome_kind"),
            "official_score": row.get("official_score"), "recovered_on_resume": True,
        })
        completed.add(job)

    history = manifest.setdefault("worker_history", [
        {"workers": manifest.get("workers", 6), "started_at": "initial-launch"}
    ])
    history.append({"workers": workers, "started_at": datetime.now(timezone.utc).isoformat(),
                    "resume": True})
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return completed, recovered


def run(root, job):
    arm, category, case = job
    out = root / arm / "tasks" / f"{category}__{case}"
    out.parent.mkdir(parents=True, exist_ok=True)
    record = dict(arm=arm, category=category, case=case)
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(root / "source")
    try:
        with Path(str(out) + ".log").open("x") as log:
            result = subprocess.run(command(root, job), cwd=root / "source", env=env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=21900)
        record["exit_code"] = result.returncode
        p = out / "results.json"
        if p.exists():
            row = json.loads(p.read_text())[0]
            record["outcome_kind"] = row.get("outcome_kind")
            record["official_score"] = row.get("official_score")
        else:
            record["outcome_kind"] = "missing_result"
    except Exception as exc:
        record.update(outcome_kind="launcher_error", error_type=type(exc).__name__)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    root = args.root.resolve()
    plan = jobs()
    if args.dry_run:
        print(json.dumps({"jobs": len(plan), "first": command(root, plan[0]),
                          "last": command(root, plan[-1])}, indent=2))
        return
    if args.workers < 1:
        raise ValueError("--workers must be positive")
    if args.resume:
        completed, recovered = resume(root, plan, args.workers)
        remaining = [job for job in plan if job not in completed]
        mode = "a"
        print(f"Resuming {len(remaining)} of {len(plan)} jobs; recovered={len(recovered)}; "
              f"concurrency={args.workers}", flush=True)
    else:
        prepare(root, plan, args.workers)
        recovered = []
        remaining = plan
        mode = "x"
        print(f"Prepared {len(plan)} jobs from tested snapshot; concurrency={args.workers}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, root, job) for job in remaining]
        with (root / "status.jsonl").open(mode) as status:
            for record in recovered:
                line = json.dumps(record)
                status.write(line + "\n")
                status.flush()
                print(line, flush=True)
            for future in concurrent.futures.as_completed(futures):
                line = json.dumps(future.result())
                status.write(line + "\n")
                status.flush()
                print(line, flush=True)


if __name__ == "__main__":
    main()
