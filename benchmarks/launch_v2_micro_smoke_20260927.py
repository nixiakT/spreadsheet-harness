from pathlib import Path
import concurrent.futures
import json
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks/results/v2-micro-smoke-20260927"
PYTHON = ROOT / ".venv/bin/python"
RUNNER = ROOT / "benchmarks/run_v2_target_category_parallel_20260925.py"
CASES = {
    "Template": ["02_03", "06_13", "06_18"],
    "Financial_Model": ["01_01", "10_03", "13_04"],
    "Debugging": ["02_06", "05_04", "07_06"],
}
SPECS = [
    (slug, model, arm, category)
    for slug, model in [
        ("deepseek-v4-flash", "dashscope/deepseek-v4-flash"),
        ("qwen3-coder-480b-a35b-instruct", "dashscope/qwen3-coder-480b-a35b-instruct"),
    ]
    for arm in ("spreadsheet-harness-basic", "spreadsheet-harness-financial")
    for category in ("Template", "Financial_Model", "Debugging")
]


def run(spec):
    slug, model, arm, category = spec
    log = OUT / "logs" / f"{slug}__{arm}__{category}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(PYTHON), str(RUNNER), "--category", category, "--arm", arm,
        "--model", model, "--model-slug", slug, "--output", str(OUT),
        "--parallelism", "26", "--task-ids", *CASES[category],
        "--base-url", "http://47.96.153.159:8010/v1",
        "--api-key-file", "/tmp/spreadsheet-harness-litellm.key",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    with log.open("a", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    return {"slug": slug, "arm": arm, "category": category, "returncode": result.returncode}


OUT.mkdir(parents=True, exist_ok=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
    results = list(pool.map(run, SPECS))
(OUT / "launcher-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
