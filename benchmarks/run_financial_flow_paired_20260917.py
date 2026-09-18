"""Bounded flow regression experiment; no changes to bare or evaluation semantics."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "benchmarks/results/financial-flow-paired-20260917"
# Development cases fixed before candidate scores are observed. Not a held-out estimate.
CASES = {
    "spreadsheet-harness-basic": ("02_01", "09_02"),
    "spreadsheet-harness-financial": ("01_04", "11_04"),
}


def command(variant, composition, case):
    out = ROOT / "tasks" / variant / composition / case
    return [
        str(REPO / ".venv/bin/python"), "-m", "spreadsheet_harness.cli",
        "benchmark", "v2-compare", "--dataset", str(REPO / "benchmarks/data/spreadsheetbench-v2"),
        "--category", "Financial_Model", "--task-id", f"Financial_Model/{case}",
        "--arm", "ours", "--composition", f"ours={composition}",
        "--skill-root", str(ROOT / "skills"), "--output", str(out),
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


def run(job):
    variant, composition, case = job
    out = ROOT / "tasks" / variant / composition / case
    if out.exists():
        return dict(variant=variant, composition=composition, case=case, status="existing-not-overwritten")
    out.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(ROOT / variant)
    with out.with_suffix(".log").open("w") as log:
        try:
            proc = subprocess.run(command(*job), cwd=ROOT / variant, env=env,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=21900)
            status = dict(exit_code=proc.returncode)
        except subprocess.TimeoutExpired:
            status = dict(status="wall-timeout")
    return dict(variant=variant, composition=composition, case=case, **status)


def main():
    jobs = [(v, c, case) for c, cases in CASES.items() for case in cases for v in ("control", "candidate")]
    if "--dry-run" in sys.argv:
        print(json.dumps([command(*job) for job in jobs], indent=2))
        return
    provenance = {}
    for variant in ("control", "candidate"):
        provenance[variant] = {str(p.relative_to(ROOT / variant)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted((ROOT / variant / "spreadsheet_harness").rglob("*.py"))}
    (ROOT / "experiment.json").write_text(json.dumps({
        "purpose": "development validation, not a held-out benchmark",
        "budget_reference": "old independent Financial run; 50 turns, 32768 output tokens, 6h",
        "jobs": jobs, "source_sha256": provenance, "concurrency": 4,
    }, indent=2))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        with (ROOT / "status.jsonl").open("a") as status:
            for future in concurrent.futures.as_completed(futures):
                line = json.dumps(future.result())
                print(line, flush=True)
                status.write(line + "\n")
                status.flush()


if __name__ == "__main__":
    main()
