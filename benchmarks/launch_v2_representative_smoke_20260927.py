from pathlib import Path
import concurrent.futures
import json
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks/results/v2-representative-smoke-20260927"
PYTHON = ROOT / ".venv/bin/python"
RUNNER = ROOT / "benchmarks/run_v2_target_category_parallel_20260925.py"
CASES = {
    "Template": ["01_01", "01_03", "02_03", "03_02", "04_04", "06_01", "06_12", "06_13", "06_18", "06_21", "07_01", "07_03", "08_02", "09_01", "10_01", "11_02", "13_03", "14_02", "15_03", "16_07"],
    "Financial_Model": ["01_01", "01_02", "03_05", "04_05", "05_05", "08_01", "08_02", "10_03", "12_02", "13_02", "13_04", "15_01", "15_02", "16_02", "17_04", "17_05", "19_04", "20_02", "20_04", "20_05"],
    "Debugging": ["01_01", "02_06", "03_02", "03_05", "04_02", "05_01", "05_04", "05_05", "05_06", "05_09", "06_02", "06_07", "07_06", "08_01", "09_01", "09_04", "10_01", "10_03", "10_06", "10_09"],
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
    cmd = [str(PYTHON), str(RUNNER), "--category", category, "--arm", arm,
           "--model", model, "--model-slug", slug, "--output", str(OUT),
           "--parallelism", "26", "--task-ids", *CASES[category],
           "--base-url", "http://47.96.153.159:8010/v1",
           "--api-key-file", "/tmp/spreadsheet-harness-litellm.key"]
    env = os.environ.copy(); env["PYTHONPATH"] = str(ROOT / "src")
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(cmd, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    return {"slug": slug, "arm": arm, "category": category, "returncode": result.returncode}

OUT.mkdir(parents=True, exist_ok=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
    rows = list(pool.map(run, SPECS))
(OUT / "launcher-results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
