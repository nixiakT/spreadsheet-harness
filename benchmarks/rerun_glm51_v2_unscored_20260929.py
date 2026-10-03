from __future__ import annotations
import concurrent.futures, json, os, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv/bin/python"
RUNNER = ROOT / "benchmarks/run_v2_target_category_parallel_20260925.py"
OUT = ROOT / "benchmarks/results/v2-glm51-public-20260928"
REPORTS = sorted((OUT / "glm-5.1").glob("rescore-spreadsheet-harness-*-20260929.json")) + sorted((OUT / "glm-5.1").glob("rescore-bare-*-20260929.json"))

def run(report: Path):
    rows = json.loads(report.read_text())
    ids = sorted(r["task_id"].split("/", 1)[1] for r in rows if r.get("status") != "scored")
    if not ids:
        return {"report": report.name, "count": 0, "returncode": 0}
    parts = report.stem.split("-")
    # rescore-{arm}-{category}-20260929, with arm containing hyphens.
    marker = "-20260929"
    stem = report.stem.removeprefix("rescore-").removesuffix(marker)
    category = stem.rsplit("-", 1)[-1]
    arm = stem[: -(len(category) + 1)]
    log = OUT / "unscored-rerun-logs" / f"{arm}__{category}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(PY), str(RUNNER), "--category", category, "--arm", arm,
           "--model", "dashscope/glm-5.1", "--model-slug", "glm-5.1",
           "--output", str(OUT), "--parallelism", "3", "--task-ids", *ids,
           "--base-url", "http://47.96.153.159:8010/v1",
           "--api-key-file", "/tmp/spreadsheet-harness-litellm.key",
           "--task-timeout", "7200", "--request-timeout", "1800", "--litellm-timeout", "1800"]
    env = os.environ.copy(); env["PYTHONPATH"] = str(ROOT / "src")
    with log.open("w") as f:
        rc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT).returncode
    return {"report": report.name, "arm": arm, "category": category, "count": len(ids), "returncode": rc}

def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(run, REPORTS))
    (OUT / "unscored-rerun-results-20260929.json").write_text(json.dumps(rows, indent=2) + "\n")
    print(json.dumps(rows, indent=2))

if __name__ == "__main__": main()
