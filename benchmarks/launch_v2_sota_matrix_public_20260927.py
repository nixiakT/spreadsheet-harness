from pathlib import Path
import concurrent.futures, json, os, subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks/results/v2-sota-matrix-public-20260927"
PYTHON = ROOT / ".venv/bin/python"
RUNNER = ROOT / "benchmarks/run_v2_target_category_parallel_20260925.py"
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
           "--parallelism", "26", "--base-url", "http://47.96.153.159:8010/v1",
           "--api-key-file", "/tmp/spreadsheet-harness-litellm.key"]
    env = os.environ.copy(); env["PYTHONPATH"] = str(ROOT / "src")
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(cmd, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    return {"slug": slug, "arm": arm, "category": category, "returncode": result.returncode}

OUT.mkdir(parents=True, exist_ok=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
    rows = list(pool.map(run, SPECS))
(OUT / "launcher-results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
