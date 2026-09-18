"""Add four disjoint workers to the frozen full run without disturbing active jobs."""
import concurrent.futures
import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "benchmarks/results/flow-fixed-basic-financial-v2-full-20260917"
STATUS = ROOT / "accelerator-status.jsonl"
WORKERS = 4
TAIL_JOBS = 120


def load_launcher():
    path = REPO / "benchmarks/run_flow_fixed_v2_full_20260917.py"
    spec = importlib.util.spec_from_file_location("flow_fixed_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    launcher = load_launcher()
    plan = launcher.jobs()
    selected = plan[-TAIL_JOBS:]

    manifest = json.loads((ROOT / "experiment.json").read_text())
    if manifest["jobs"] != [list(job) for job in plan]:
        raise ValueError("Accelerator plan differs from the frozen experiment")
    actual = {"spreadsheet_harness/" + key: value
              for key, value in launcher.hashes(ROOT / "source/spreadsheet_harness", "*.py").items()}
    if actual != manifest["source_sha256"]:
        raise ValueError("Frozen experiment source changed")
    if STATUS.exists():
        raise FileExistsError(STATUS)

    # This tail is far from the primary launcher's current forward position.
    collisions = []
    for arm, category, case in selected:
        out = ROOT / arm / "tasks" / f"{category}__{case}"
        if out.exists() or Path(str(out) + ".log").exists():
            collisions.append((arm, category, case))
    if collisions:
        raise ValueError(f"Selected accelerator jobs already started: {collisions[:3]}")

    print(f"Accelerating {len(selected)} disjoint tail jobs with concurrency={WORKERS}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(launcher.run, ROOT, job) for job in selected]
        with STATUS.open("x") as status:
            for future in concurrent.futures.as_completed(futures):
                line = json.dumps(future.result())
                status.write(line + "\n")
                status.flush()
                print(line, flush=True)


if __name__ == "__main__":
    main()
