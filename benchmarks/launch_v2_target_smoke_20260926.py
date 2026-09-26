from pathlib import Path
import concurrent.futures
import json
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks/results/v2-target-smoke-20260926"
PYTHON = ROOT / ".venv/bin/python"
RUNNER = ROOT / "benchmarks/run_v2_target_category_parallel_20260925.py"
CASES = {
    "Template": ["02_03", "03_02", "04_04", "06_12", "06_13", "06_18"],
    "Financial_Model": ["08_02", "13_04", "04_05", "10_03", "01_01", "13_02"],
    "Debugging": ["02_06", "03_02", "05_01", "05_04", "07_06", "08_01"],
}
SPECS = [
    (slug, model, arm, category)
    for slug, model in [
        ("deepseek-v4-flash-0731", "dashscope/deepseek-v4-flash-0731"),
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
        "--base-url", "http://10.130.138.46:8010/v1",
        "--api-key-file", "/tmp/spreadsheet-harness-litellm.key",
    ]
    env = os.environ.copy(); env["PYTHONPATH"] = str(ROOT / "src")
    with log.open("a", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    record = {"slug": slug, "arm": arm, "category": category, "returncode": result.returncode}
    with (OUT / "launcher-results.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


OUT.mkdir(parents=True, exist_ok=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
    for result in pool.map(run, SPECS):
        print(json.dumps(result, ensure_ascii=False), flush=True)
