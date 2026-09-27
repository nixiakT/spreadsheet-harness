from pathlib import Path
import concurrent.futures, json, os, subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks/results/v2-sota-matrix-control16-20260927"
PYTHON = ROOT / ".venv/bin/python"
RUNNER = ROOT / "benchmarks/run_v2_target_category_parallel_20260925.py"
MODELS = {
    "deepseek-v4-flash-0731": "dashscope/deepseek-v4-flash-0731",
    "qwen3-coder-480b-a35b-instruct": "dashscope/qwen3-coder-480b-a35b-instruct",
}

def collect_failed():
    failed = {}
    for path in OUT.glob("*/*/*/*/run/results.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))[0]
        except (OSError, ValueError, IndexError):
            continue
        # Salvaged artifacts are now accepted as valid model outputs; retry only
        # tasks that still have no usable scored artifact.
        if row.get("outcome_kind") not in {"model_execution_failure", "not_scored"}:
            continue
        rel = path.relative_to(OUT).parts
        failed.setdefault(tuple(rel[:3]), []).append(rel[3])
    return failed

def run(spec):
    slug, arm, category, ids = spec
    log = OUT / "rerun-logs" / f"{slug}__{arm}__{category}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(PYTHON), str(RUNNER), "--category", category, "--arm", arm,
           "--model", MODELS[slug], "--model-slug", slug, "--output", str(OUT),
           "--parallelism", "8", "--task-ids", *sorted(set(ids)),
           "--base-url", "http://47.96.153.159:8010/v1",
           "--api-key-file", "/tmp/spreadsheet-harness-litellm.key"]
    env = os.environ.copy(); env["PYTHONPATH"] = str(ROOT / "src")
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(cmd, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
    return {"slug": slug, "arm": arm, "category": category, "count": len(ids), "returncode": result.returncode}

failed = collect_failed()
specs = [(slug, arm, category, ids) for (slug, arm, category), ids in sorted(failed.items())]
(OUT / "rerun-failed-specs.json").write_text(json.dumps(specs, ensure_ascii=False, indent=2), encoding="utf-8")
with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(12, len(specs)))) as pool:
    rows = list(pool.map(run, specs))
(OUT / "rerun-failed-results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
