#!/usr/bin/env python3
"""Retry API/provider failures from the paper audit, writing additive roots."""
from __future__ import annotations
import argparse, json, os, re, shlex, subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv/bin/python"
KEY = Path("/tmp/spreadsheet-harness-litellm.key")
BASE = "http://10.130.138.46:8010/v1"
V1 = ROOT / "benchmarks/data/spreadsheetbench_912_v0.1"
V2 = ROOT / "benchmarks/data/spreadsheetbench-v2"
EVAL2 = ROOT / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
AUDIT = Path("/tmp/paper-api-audit.Gg8lRy/audit.json")

def slug(x: str) -> str:
    return x.replace("/", "__").replace(" ", "_")

def v1_cmd(model: str, arm: str, task: str, out: Path) -> list[str]:
    return [str(PY), "-m", "spreadsheet_harness.cli", "benchmark", "v1-compare",
            "--dataset", str(V1), "--task-id", task, "--arm", arm, "--output", str(out),
            "--max-model-calls", "50", "--max-turns-per-arm", "50",
            "--max-total-tokens", "10000000", "--max-output-tokens", "32768",
            "--task-timeout", "21600", "--request-timeout", "1200",
            "--litellm-timeout", "1200", "--request-retries", "5",
            "--request-interval-seconds", "1.5", "--arm-order-seed", "20261001",
            "--base-url", BASE, "--api-key-file", str(KEY), "--model", model,
            "--api-protocol", "chat-completions", "--reasoning-effort", "medium",
            "--seed", "41", "--temperature", "0", "--top-p", "1", "--enable-thinking"]

def v2_cmd(model: str, arm: str, task: str, out: Path) -> list[str]:
    cat = task.split("/", 1)[0]
    return [str(PY), "-m", "spreadsheet_harness.cli", "benchmark", "v2-compare",
            "--dataset", str(V2), "--official-evaluator", str(EVAL2),
            "--category", cat, "--task-id", task, "--arm", arm, "--output", str(out),
            "--max-model-calls", "50", "--max-turns-per-arm", "50",
            "--max-total-tokens", "10000000", "--max-output-tokens", "32768",
            "--task-timeout", "21600", "--request-timeout", "1200",
            "--litellm-timeout", "1200", "--request-retries", "5",
            "--request-interval-seconds", "1.5", "--arm-order-seed", "20261001",
            "--base-url", BASE, "--api-key-file", str(KEY), "--model", model,
            "--api-protocol", "chat-completions", "--reasoning-effort", "medium",
            "--seed", "41", "--temperature", "0", "--top-p", "1", "--enable-thinking"]

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", type=Path, default=AUDIT)
    ap.add_argument("--output-root", type=Path, default=ROOT / "benchmarks/results/api-retry-20261001")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mode", choices=("all", "external", "ours"), default="all")
    ap.add_argument("--ours-workers", type=int, default=6)
    ap.add_argument("--canary", action="store_true", help="Use one API-failed task per method group")
    ap.add_argument("--canary-count", type=int, default=20)
    args = ap.parse_args()
    rows = json.loads(args.audit.read_text())["api_retry"]
    groups = defaultdict(set)
    for r in rows:
        # The dedicated GLM Spreadsheet-RL provider retry is already running.
        if r["model"] == "GLM-5.1" and r["method"] == "Spreadsheet-RL":
            continue
        groups[(r["model"], r["method"], r["suite"])].add(r["task_id"])
    args.output_root.mkdir(parents=True, exist_ok=True)
    plan = {"source_audit": str(args.audit),
            "groups": {"|".join(k): sorted(v) for k, v in groups.items()}}
    (args.output_root / "retry-plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    )
    commands = []
    for (model, method, suite), tasks in sorted(groups.items()):
        if args.canary:
            tasks = set(sorted(tasks)[:args.canary_count])
        is_glm = model == "GLM-5.1"
        model_arg = "dashscope/glm-5.1" if is_glm else "dashscope/deepseek-v4-flash-0731"
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", method.lower())
        root = args.output_root / ("glm51" if is_glm else "deepseek") / stem / suite
        # If an earlier attempt left a manifest with a different protocol
        # (for example parallelism=20), keep it immutable and use an additive
        # retry root. Task-level completed outputs in the new root are still
        # reused by the runners.
        if method in {"Codex", "Claude Code", "DeepSeekHarness",
                      "Codex + xlsx", "Claude Code + xlsx", "DeepSeekHarness + xlsx"} and (root / "manifest.json").is_file():
            root = root.with_name(root.name + "-r2")
        root.mkdir(parents=True, exist_ok=True)
        if method in {"Codex", "Claude Code", "DeepSeekHarness",
                      "Codex + xlsx", "Claude Code + xlsx", "DeepSeekHarness + xlsx"}:
            harness = {"Codex":"codex", "Claude Code":"claude", "DeepSeekHarness":"dsh",
                       "Codex + xlsx":"codex", "Claude Code + xlsx":"claude",
                       "DeepSeekHarness + xlsx":"dsh"}[method]
            run = (ROOT / "benchmarks/run_external_harness_spreadsheetbench_v1.py"
                   if suite == "v1" else
                   {"codex": ROOT / "benchmarks/run_codex_spreadsheetbench_v2.py",
                    "claude": ROOT / "benchmarks/run_claude_spreadsheetbench_v2.py",
                    "dsh": ROOT / "benchmarks/run_deepseek_harness_spreadsheetbench_v2.py"}[harness])
            cmd = [str(PY), str(run), "--run-root", str(root), "--model", model_arg,
                   "--base-url", BASE, "--api-key-file", str(KEY), "--parallelism", "4"]
            if suite == "v1":
                cmd += ["--dataset", str(V1), "--harness", harness,
                        "--max-turns", "50", "--task-timeout", "21600",
                        "--replay-timeout", "1800"]
            else:
                cmd += ["--dataset", str(V2), "--max-turns", "50",
                        "--task-timeout", "21600"]
            cmd += (["--skill", str(ROOT / "skills/xlsx/SKILL.md")]
                    if method.endswith("+ xlsx") else ["--no-skill"])
            for task in sorted(tasks):
                cmd += ["--task-id", task]
            commands.append((f"{model}-{method}-{suite}", cmd, root))
        elif method in {"Bare configuration", "SheetHarness-Basic", "SheetHarness-Financial"}:
            arm = {"Bare configuration":"bare", "SheetHarness-Basic":"spreadsheet-harness-basic",
                   "SheetHarness-Financial":"spreadsheet-harness-financial"}[method]
            for task in sorted(tasks):
                out = root / slug(task)
                cmd = v1_cmd(model_arg, arm, task, out) if suite == "v1" else v2_cmd(model_arg, arm, task, out)
                commands.append((f"{model}-{method}-{suite}-{task}", cmd, root))
        elif method == "SpreadsheetAgent":
            run = ROOT / "benchmarks/run_glm51_spreadsheetagent_official_compat_20260928.py"
            cmd = [str(PY), str(run), "--suite", suite, "--output", str(root),
                   "--workers", "4", "--task-timeout", "10800",
                   "--request-timeout", "1200", "--request-interval", "1.5"]
            for task in sorted(tasks):
                cmd += (["--v1-task", task] if suite == "v1" else ["--v2-task", task])
            commands.append((f"{model}-{method}-{suite}", cmd, root))
    if args.mode == "external":
        commands = [x for x in commands
                    if not any(t in x[0] for t in ("Bare configuration",
                                                   "SheetHarness-Basic",
                                                   "SheetHarness-Financial"))]
    elif args.mode == "ours":
        commands = [x for x in commands if any(name in x[0] for name in ("Bare configuration", "SheetHarness-Basic", "SheetHarness-Financial"))]
    (args.output_root / f"commands-{args.mode}.sh").write_text(
        "\n".join(" ".join(shlex.quote(x) for x in cmd) for _, cmd, _ in commands) + "\n"
    )
    print("groups", len(groups), "commands", len(commands),
          "tasks", sum(len(v) for v in groups.values()))
    if args.dry_run:
        return 0
    if args.mode == "ours":
        # Keep isolated task runners bounded; detached shell workers are not
        # allowed to flood the provider.
        queue = args.output_root / "ours-queue.txt"
        queue.write_text("\n".join(
            json.dumps({"name": n, "cmd": c, "root": str(r)})
            for n, c, r in commands
        ) + "\n")
        worker = args.output_root / "run_ours_queue.py"
        worker.write_text("""import concurrent.futures, json, os, subprocess, sys\nfrom pathlib import Path\nq=Path(sys.argv[1]); rows=[json.loads(x) for x in q.read_text().splitlines() if x.strip()]\ndef run(row):\n  log=Path(row['root'])/'task-launcher.log'; log.parent.mkdir(parents=True,exist_ok=True)\n  env=os.environ.copy()\n  for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'): env.pop(k,None)\n  env['NO_PROXY']='127.0.0.1,localhost'; env['no_proxy']='127.0.0.1,localhost'\n  with log.open('a') as f: rc=subprocess.run(row['cmd'],cwd='/data/zju-160/tongzeyuan/spreadsheet-harness',env=env,stdout=f,stderr=subprocess.STDOUT).returncode\n  return {'name':row['name'],'rc':rc}\nwith concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:\n  for i,f in enumerate(concurrent.futures.as_completed([pool.submit(run,r) for r in rows]),1):\n    print(json.dumps({'done':i,'total':len(rows),**f.result()}),flush=True)\n""")
        shell = f"cd {shlex.quote(str(ROOT))} && exec {shlex.quote(str(PY))} {shlex.quote(str(worker))} {shlex.quote(str(queue))}"
        subprocess.run(["tmux", "-S", "/tmp/api-retry-20261001.sock", "new-session",
                        "-d", "-s", "api-retry-ours", shell], check=False)
        print("started bounded ours queue", len(commands), "tasks", flush=True)
        return 0
    if args.mode == "external":
        queue = args.output_root / "external-queue.jsonl"
        queue.write_text("\n".join(json.dumps({"name": n, "cmd": c, "root": str(r)})
                                     for n, c, r in commands) + "\n")
        worker = args.output_root / "run_external_queue.py"
        worker.write_text("""import json, os, subprocess, sys
from pathlib import Path
q=Path(sys.argv[1]); rows=[json.loads(x) for x in q.read_text().splitlines() if x.strip()]
env=os.environ.copy()
for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'): env.pop(k,None)
env['NO_PROXY']='127.0.0.1,localhost'; env['no_proxy']='127.0.0.1,localhost'
for i,row in enumerate(rows,1):
  root=Path(row['root']); root.mkdir(parents=True,exist_ok=True)
  log=root/'launcher.log'
  print(json.dumps({'started':i,'total':len(rows),'name':row['name']}),flush=True)
  with log.open('a') as f:
    rc=subprocess.run(row['cmd'],cwd='/data/zju-160/tongzeyuan/spreadsheet-harness',env=env,stdout=f,stderr=subprocess.STDOUT).returncode
  print(json.dumps({'finished':i,'total':len(rows),'name':row['name'],'rc':rc}),flush=True)
""")
        shell = (f"cd {shlex.quote(str(ROOT))} && unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy && "
                 f"export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost && "
                 f"exec {shlex.quote(str(PY))} {shlex.quote(str(worker))} {shlex.quote(str(queue))} > "
                 f"{shlex.quote(str(args.output_root/'external-scheduler.log'))} 2>&1")
        subprocess.run(["tmux","-S","/tmp/api-retry-20261001.sock","new-session","-d",
                        "-s","api-retry-external",shell],check=False)
        print("started sequential external scheduler",len(commands),"method groups",flush=True)
        return 0
    # Run at most 32 method/task groups at once.  Each grouped method runner
    # itself is configured with four workers; completed outputs are reused by
    # the runner instead of being overwritten.
    queue = args.output_root / "all-queue.jsonl"
    queue.write_text("\n".join(json.dumps({"name": n, "cmd": c, "root": str(r)})
                                 for n, c, r in commands) + "\n")
    worker = args.output_root / "run_all_queue.py"
    worker.write_text("""import concurrent.futures, json, os, subprocess, sys\nfrom pathlib import Path\nq=Path(sys.argv[1]); rows=[json.loads(x) for x in q.read_text().splitlines() if x.strip()]\ndef run(row):\n  root=Path(row['root']); root.mkdir(parents=True,exist_ok=True); log=root/'launcher.log'; cmd=row['cmd']\n  if '--output' in cmd:\n    try:\n      out=Path(cmd[cmd.index('--output')+1])\n      if (out/'results.json').is_file() or (out/'summary.json').is_file(): return {'name':row['name'],'rc':0,'status':'reused'}\n    except Exception: pass\n  env=os.environ.copy()\n  for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'): env.pop(k,None)\n  env['NO_PROXY']='127.0.0.1,localhost'; env['no_proxy']='127.0.0.1,localhost'\n  with log.open('a') as f: rc=subprocess.run(cmd,cwd='/data/zju-160/tongzeyuan/spreadsheet-harness',env=env,stdout=f,stderr=subprocess.STDOUT).returncode\n  return {'name':row['name'],'rc':rc}\nwith concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:\n  for i,f in enumerate(concurrent.futures.as_completed([pool.submit(run,r) for r in rows]),1): print(json.dumps({'done':i,'total':len(rows),**f.result()}),flush=True)\n""")
    shell = (f"cd {shlex.quote(str(ROOT))} && unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy && "
             f"export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost && exec "
             f"{shlex.quote(str(PY))} {shlex.quote(str(worker))} {shlex.quote(str(queue))} > "
             f"{shlex.quote(str(args.output_root/'all-scheduler.log'))} 2>&1")
    subprocess.run(["tmux", "-S", "/tmp/api-retry-20261001.sock", "new-session", "-d",
                    "-s", "api-retry-all", shell], check=False)
    print("started bounded all queue", len(commands), "groups", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
