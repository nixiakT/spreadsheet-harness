#!/usr/bin/env python3
"""Workbook-disjoint, activation-audited plugin evolution.

No training of model weights. Only development traces enter GLM prompts.
Selection cases rank candidates; sealed test cases are opened once for a frozen
winner. Provider failures remain missing scores, never synthetic solver failures.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/data/zju-160/tongzeyuan/spreadsheet-harness")
PYTHON = REPO / ".venv/bin/python"
DEFAULT_ROOT = REPO / "benchmarks/results/attributed-evolution-v3b-20260917"
HISTORICAL = REPO / "benchmarks/results/expanded-multi-plugin-validation-qwen36-attributed-glm52-attributed-20260915"
OLD_SPLIT = REPO / "benchmarks/results/true-coevolution-deepseekpro-fast-20260911/split-manifest.json"
OLD_BASE = OLD_SPLIT.parent / "skill-roots/round-00-h0d0"
DATASETS = {
    "v06": REPO / "benchmarks/data/normalized-harbor/v06-financial-269",
    "enhanced-v2": REPO / "benchmarks/data/normalized-harbor/v2-enhanced-financial-1565",
}
SKILLS = {"h": "spreadsheet-structure", "d": "spreadsheet-financial-model", "c": "spreadsheet-coordination"}
ARM_EDITS = {"h0d0": (), "h1d0": ("h",), "h0d1": ("d",), "h1d1": ("h", "d"), "h1d1-coordination": ("h", "d", "c")}
KEY_FILE = Path("/tmp/spreadsheet-harness-litellm.key")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    os.replace(temp, path)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def log(root, event, **data):
    item = {"time": stamp(), "event": event, **data}
    print(json.dumps(item, ensure_ascii=False), flush=True)
    with (root / "events.jsonl").open("a") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def task_key(task):
    return task["dataset"] + "::" + task["task_id"]


def split_tasks(catalog, legacy_tasks, excluded_sources, seed=20260917):
    """Stratify by dataset/difficulty, globally disjoint by source workbook."""
    rng = random.Random(seed)
    bykey = {task_key(t): t for t in catalog}
    development = [dict(bykey[task_key(t)]) for t in legacy_tasks]
    used = set(excluded_sources) | {t["source_workbook"] for t in development}
    result = {"development": development, "selection": [], "test": []}
    for role in result:
        for dataset in DATASETS:
            for difficulty in ("C1", "C2", "C3"):
                candidates = sorted(
                    (t for t in catalog if t["dataset"] == dataset and t["complexity"] == difficulty),
                    key=task_key,
                )
                rng.shuffle(candidates)
                chosen = 0
                for task in candidates:
                    if task["source_workbook"] in used:
                        continue
                    result[role].append(dict(task))
                    used.add(task["source_workbook"])
                    chosen += 1
                    if chosen == 2:
                        break
                if chosen < 2:
                    raise ValueError(f"Insufficient independent workbooks for {role}/{dataset}/{difficulty}")
    sets = [{t["source_workbook"] for t in result[r]} for r in result]
    assert all(not sets[i] & sets[j] for i in range(3) for j in range(i))
    return result


def prepare(root):
    if (root / "protocol.json").exists():
        verify_snapshot(root)
        return
    if root.exists() and any(root.iterdir()):
        raise ValueError("Refusing to overwrite a partial preparation; choose a new result root")
    root.mkdir(parents=True, exist_ok=True)
    catalog = []
    datasets = {}
    for name, path in DATASETS.items():
        meta = path / "Financial_Model/dataset.json"
        datasets[name] = {"root": str(path), "metadata_sha256": digest(meta)}
        for row in load(meta):
            catalog.append({
                "dataset": name, "task_id": "Financial_Model/" + row["id"],
                "source_workbook": row["source_workbook"], "complexity": row["complexity"],
                "input_path": str(path / "Financial_Model" / row["spreadsheet_path"]),
            })
    legacy = load(HISTORICAL / "tasks.json")
    excluded = {t["source_workbook"] for t in load(OLD_SPLIT)["tasks"]}
    # Quarantine all known task IDs used by previous GPT/GLM candidate generators,
    # not just the most recent round. Workbooks are excluded across all variants.
    byid = {t["task_id"]: t for t in catalog}
    for manifest in (REPO / "benchmarks/results").glob("gpt56-sol-candidates*/attribution.json"):
        for record in load(manifest).get("records", {}).values():
            if record.get("task_id") in byid:
                excluded.add(byid[record["task_id"]]["source_workbook"])
    split = split_tasks(catalog, legacy, excluded)
    for role, tasks in split.items():
        for t in tasks:
            t["role"] = role
            t["input_sha256"] = digest(t["input_path"])
    write(root / "split.json", {"seed": 20260917, "unit": "source_workbook", "roles": split,
                               "excluded_previous_sources": sorted(excluded)})
    shutil.copytree(REPO / "src/spreadsheet_harness", root / "source/spreadsheet_harness",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(OLD_BASE, root / "baseline-skills", ignore=shutil.ignore_patterns("__pycache__"))
    # Coordination starts from a frozen seed, not an unvalidated historical candidate.
    c = root / "baseline-skills/spreadsheet-coordination/SKILL.md"
    c.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "skills/spreadsheet-coordination/SKILL.md", c)
    shutil.copy2(Path(__file__), root / "controller.py")
    frozen = [root / "controller.py", *(root / "source").rglob("*.py"),
              *(root / "baseline-skills").rglob("SKILL.md")]
    hashes = {str(p.relative_to(root)): digest(p) for p in sorted(frozen)}
    protocol = {
        "version": "attributed-evolution-v3", "created": stamp(), "datasets": datasets,
        "hashes": hashes, "split_sha256": digest(root / "split.json"),
        "solver": "qwen36-35b-a3b", "generator": "dashscope/glm-5.2",
        "base_url": "http://10.130.138.46:8010/v1", "max_turns": 50,
        "temperature": 0.0, "top_p": 1.0, "thinking": True, "seed": 41,
        "max_output_tokens": None, "max_total_tokens": None,
        "parallelism": 4, "request_timeout": 600, "task_timeout": 3600,
        "max_rounds": 3, "generation_cases_per_coordinate": 8,
        "policy": "development-only feedback; selection-ranked candidates; test once",
        "test_scope_note": "Disjoint from known evolution evidence, not a claim of zero prior use by unrelated experiments.",
    }
    write(root / "protocol.json", protocol)
    write(root / "status.json", {"state": "prepared", "time": stamp(),
                               "counts": {r: len(v) for r, v in split.items()}})
    print(json.dumps({"prepared": str(root), "counts": {r: len(v) for r, v in split.items()}}))


def verify_snapshot(root):
    protocol = load(root / "protocol.json")
    for rel, expected in protocol["hashes"].items():
        if digest(root / rel) != expected:
            raise ValueError(f"Frozen experiment file changed: {rel}")
    if digest(root / "split.json") != protocol["split_sha256"]:
        raise ValueError("Frozen split changed")
    for meta in protocol["datasets"].values():
        if digest(Path(meta["root"]) / "Financial_Model/dataset.json") != meta["metadata_sha256"]:
            raise ValueError("Dataset metadata changed")
    return protocol


def child_environment(root):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "source")
    env["SPREADSHEET_EVOLUTION_ROUTE_STRUCTURE"] = "1"
    # ProviderConfig is built explicitly. Do not read or change interactive Codex auth.
    for name in ("OPENAI_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    return env


def arm_paths(root, round_number, arm):
    if arm == "h0d0":
        return root / "baseline-skills", root / "baseline-composition.json"
    base = root / f"round-{round_number:02d}"
    return base / "skills" / arm, base / "compositions" / f"{arm}.json"


def build_arms(root, round_number, candidates):
    from spreadsheet_harness.plugins import (
        PLUGEOLVE_SEED_COMPOSITION,
        CompositionSpec,
        default_plugin_registry,
    )
    seed = PLUGEOLVE_SEED_COMPOSITION
    baseline = root / "baseline-composition.json"
    if not baseline.exists():
        write(baseline, {"schema_version": "plugevolve-composition-v1", **seed.to_dict()})
    for arm, edits in ARM_EDITS.items():
        if not edits:
            continue
        skillroot, composition_path = arm_paths(root, round_number, arm)
        if not skillroot.exists():
            shutil.copytree(root / "baseline-skills", skillroot)
            for coordinate in edits:
                shutil.copy2(candidates[coordinate], skillroot / SKILLS[coordinate] / "SKILL.md")
        spec = seed
        if "c" in edits:
            spec = CompositionSpec.create(
                arm, (*seed.plugins, "knowledge-coordination"), dict(seed.overrides),
            )
        default_plugin_registry().resolve(spec)
        if not composition_path.exists():
            write(composition_path, {"schema_version": "plugevolve-composition-v1", **spec.to_dict()})


def expected_skills(skillroot, arm):
    names = ["spreadsheet-structure", "spreadsheet-financial-model", "spreadsheet-formula",
             "spreadsheet-verification"]
    if arm == "h1d1-coordination":
        names.append("spreadsheet-coordination")
    return {name: digest(skillroot / name / "SKILL.md") for name in names}


def audit_activation(trajectory, expected):
    started = []
    for line in trajectory.read_text().splitlines():
        row = json.loads(line)
        if row["event"] == "agent.started":
            actual = {s["name"]: s["sha256"] for s in row["payload"].get("skills", [])}
            started.append(actual)
    return bool(started) and all(all(actual.get(k) == v for k, v in expected.items()) for actual in started)


def run_cell(root, protocol, task, arm, round_number):
    group = "baseline" if arm == "h0d0" else f"round-{round_number:02d}/{arm}"
    out = root / "runs" / group / task["role"] / task["dataset"] / task["task_id"].replace("/", "_")
    out.mkdir(parents=True, exist_ok=True)
    record = out / "cell.json"
    previous = load(record) if record.exists() else None
    recovering = bool(protocol.get("recover_infrastructure"))
    if previous is not None and (not recovering or previous["status"] != "infrastructure"):
        return previous
    skillroot, composition = arm_paths(root, round_number, arm)
    expected = expected_skills(skillroot, arm)
    if digest(task["input_path"]) != task["input_sha256"]:
        raise ValueError("Task input changed after split freeze")
    result = None
    # Retries are for execution/provider failures only. Never resample a scored miss.
    first_attempt = int(previous.get("attempt", 0)) + 1 if previous else 1
    attempts_per_pass = 1 if recovering else 2
    for attempt in range(first_attempt, first_attempt + attempts_per_pass):
        workspace = out / f"attempt-{attempt}"
        if workspace.exists() and not (workspace / "summary.json").exists():
            result = {"status": "interrupted_unresolved", "attempt": attempt}
            break
        if not workspace.exists():
            command = [
                str(PYTHON), "-m", "spreadsheet_harness.cli", "benchmark", "v2-compare",
                "--dataset", str(DATASETS[task["dataset"]]), "--category", "Financial_Model",
                "--task-id", task["task_id"], "--arm", "ours",
                "--composition-file", f"ours={composition}", "--skill-root", str(skillroot),
                "--output", str(workspace), "--max-model-calls", "50", "--max-turns-per-arm", "50",
                "--max-total-tokens", "unlimited", "--max-output-tokens", "unlimited",
                "--task-timeout", str(protocol["task_timeout"]),
                "--request-timeout", str(protocol["request_timeout"]),
                "--litellm-timeout", str(protocol["request_timeout"]),
                "--request-retries", "2", "--request-interval-seconds", "1.1",
                "--arm-order-seed", "20260917", "--base-url", protocol["base_url"],
                "--model", protocol["solver"], "--api-protocol", "chat-completions",
                "--reasoning-effort", "medium", "--seed", "41", "--temperature", "0",
                "--top-p", "1", "--enable-thinking",
            ]
            env = child_environment(root)
            env["OPENAI_API_KEY"] = KEY_FILE.read_text().strip()
            log(root, "cell.started", task=task_key(task), arm=arm, round=round_number, attempt=attempt)
            with (out / f"attempt-{attempt}.log").open("w") as handle:
                try:
                    completed = subprocess.run(command, cwd=REPO, env=env,
                                               stdout=handle, stderr=subprocess.STDOUT,
                                               timeout=protocol["task_timeout"] + 90)
                    code = completed.returncode
                except subprocess.TimeoutExpired:
                    code = 124
        else:
            code = None
        trajectories = list(workspace.glob("runs/*/*/ours/trajectory.jsonl"))
        trajectory = trajectories[0] if len(trajectories) == 1 else None
        valid = trajectory is not None and audit_activation(trajectory, expected)
        summary = load(workspace / "summary.json") if (workspace / "summary.json").exists() else {}
        scores = next(iter(summary.get("arms", {}).values()), {})
        result = {
            "status": "scored" if summary.get("study_complete") and valid else "execution_error",
            "attempt": attempt, "returncode": code, "activation_valid": valid,
            "trajectory": str(trajectory) if trajectory else None, "workspace": str(workspace),
            "exact": scores.get("accuracy"), "modification": scores.get("modification_accuracy"),
            "regression": scores.get("regression_accuracy"),
            "skills": expected,
        }
        if recovering:
            error_rows = load(workspace / "results.json") if (workspace / "results.json").is_file() else []
            provider_failures = []
            if trajectory is not None:
                provider_failures = [json.loads(line)["payload"].get("provider_error", {})
                                     for line in trajectory.read_text().splitlines()
                                     if json.loads(line).get("event") == "model.failed"]
            infra_types = {"ProviderError", "InterruptedAmbiguousRequest", "ScoringInfrastructureError"}
            if provider_failures or code == 124 or any(r.get("error_type") in infra_types for r in error_rows):
                result.update(status="infrastructure", exact=None, modification=None, regression=None,
                              error_types=[r.get("error_type") for r in error_rows],
                              provider_status_codes=[e.get("status_code") for e in provider_failures])
                break
        if result["status"] == "scored":
            break
        if summary.get("study_complete") and not valid:
            result["status"] = "invalid_activation"
            break
        if (workspace / "results.json").is_file():
            errors = load(workspace / "results.json")
            result["error_types"] = [r.get("error_type") for r in errors]
            # Unknown runtime errors need diagnosis, not repeated paid sampling.
            if not any(r.get("error_type") in {"ProviderError", "InterruptedAmbiguousRequest"} for r in errors):
                break
    result.update({"task": task, "arm": arm, "round": 0 if arm == "h0d0" else round_number})
    if recovering and previous is not None:
        write(out / f"cell-attempt-{previous['attempt']}.json", previous)
    write(record, result)
    log(root, "cell.finished", task=task_key(task), arm=arm, round=round_number,
        status=result["status"], exact=result.get("exact"))
    return result


def batch(root, protocol, tasks, arms, round_number):
    jobs = [(t, a) for t in tasks for a in arms]
    random.Random(20260917 + round_number).shuffle(jobs)
    rows = []
    with cf.ThreadPoolExecutor(max_workers=protocol["parallelism"]) as pool:
        futures = [pool.submit(run_cell, root, protocol, t, a, round_number) for t, a in jobs]
        for f in cf.as_completed(futures):
            rows.append(f.result())
    return rows


def compare(baseline, candidate, tasks):
    b = {task_key(r["task"]): r for r in baseline}
    c = {task_key(r["task"]): r for r in candidate}
    paired = [(b[task_key(t)], c[task_key(t)]) for t in tasks
              if b.get(task_key(t), {}).get("status") == "scored"
              and c.get(task_key(t), {}).get("status") == "scored"]
    wins = sum(y["exact"] > x["exact"] for x, y in paired)
    losses = sum(y["exact"] < x["exact"] for x, y in paired)
    n = len(paired)
    deltas = {k: sum(y[k] - x[k] for x, y in paired) / n if n else None
              for k in ("exact", "modification", "regression")}
    n_discordant = wins + losses
    p = (min(1.0, 2 * sum(math.comb(n_discordant, i) for i in range(min(wins, losses) + 1))
             / 2 ** n_discordant) if n_discordant else 1.0)
    return {
        "expected": len(tasks), "paired_scored": n, "complete": n == len(tasks),
        "baseline_passes": sum(x["exact"] for x, _ in paired),
        "candidate_passes": sum(y["exact"] for _, y in paired),
        "wins": wins, "losses": losses, "delta": deltas, "exact_mcnemar_two_sided_p": p,
        "qualifies": n == len(tasks) and wins > losses
        and deltas["modification"] >= -0.02 and deltas["regression"] >= -0.02,
        "interpretation": "observed paired improvement, not proof of broad generalization",
    }


def diverse_select(records, coordinate, limit=8):
    """One representative per case before any duplicate; reserve passing controls."""
    bytask = defaultdict(list)
    for r in records:
        bytask[r["task_id"]].append(r)
    representatives = []
    for items in bytask.values():
        representatives.append(max(items, key=lambda r: (
            r["attribution_scores"][coordinate], not r["passed"], r["trajectory"])))
    failures = sorted((r for r in representatives if not r["passed"]),
                      key=lambda r: (-r["attribution_scores"][coordinate], r["task_id"]))
    controls = sorted((r for r in records if r["passed"]), key=lambda r: r["task_id"])
    unique_controls = []
    for r in controls:
        if r["task_id"] not in {c["task_id"] for c in unique_controls}:
            unique_controls.append(r)
    reserved = unique_controls[:min(2, max(1, limit // 4))]
    control_tasks = {r["task_id"] for r in reserved}
    chosen = [r for r in failures if r["task_id"] not in control_tasks][:limit-len(reserved)]
    chosen += reserved
    for r in failures + representatives:
        if len(chosen) >= limit:
            break
        if r["task_id"] not in {x["task_id"] for x in chosen}:
            chosen.append(r)
    return chosen


def evidence_records(root, development):
    from spreadsheet_harness.evolution import extract_trajectory_evidence
    allowed = {t["task_id"] for t in development}
    records = {}
    # Historical results are weak observational evidence, never factorial causal estimates.
    for task in development:
        d = HISTORICAL / "runs/h0d0" / task["dataset"] / task["task_id"].replace("/", "_")
        for p in d.glob("runs/*/*/ours/trajectory.jsonl"):
            records[str(p)] = {"task_id": task["task_id"], "arm": "legacy", "round": 0,
                               "trajectory": str(p), "activation_valid": False}
    for p in (root / "runs").rglob("cell.json"):
        row = load(p)
        if row["task"]["role"] != "development" or row["status"] != "scored":
            continue
        records[row["trajectory"]] = {**row, "task_id": row["task"]["task_id"]}
    gathered = []
    for row in records.values():
        if row["task_id"] not in allowed:
            raise ValueError("Nondevelopment evidence rejected")
        evidence = extract_trajectory_evidence(row["trajectory"], max_items_per_category=8)
        if evidence.evaluator_outcome is None:
            continue
        outcome = evidence.evaluator_outcome
        official = outcome.get("payload", {}).get("official_score", {})
        error = str(official.get("error_message", "")).lower()
        scores = {"h": 1.0, "d": 1.0, "c": 1.0}
        # Signatures are hypotheses about mechanisms, NOT causal blame assignments.
        if "regression" in error:
            scores["h"] += 1
            scores["c"] += 1
        if "modification" in error:
            scores["d"] += 1
        record = {**row, "sha256": evidence.sha256, "passed": outcome["passed"],
                  "official_score": official, "attribution_scores": scores,
                  "causal_claim": False, "counterfactuals": {},
                  "rationale": ["Error signatures guide hypotheses; historical inactive H cannot support a causal contrast."]}
        gathered.append(record)
    bytask = defaultdict(dict)
    for r in gathered:
        if r.get("activation_valid"):
            bytask[r["task_id"]][(r["round"], r["arm"])] = r
    for r in gathered:
        matrix = bytask[r["task_id"]]
        base = matrix.get((0, "h0d0"))
        if base is None:
            continue
        for arm, coordinate, changed in (("h1d0", "h", "spreadsheet-structure"),
                                         ("h0d1", "d", "spreadsheet-financial-model")):
            other = matrix.get((r["round"], arm))
            if other is None:
                continue
            unchanged = {k: v for k, v in base["skills"].items() if k != changed}
            if not all(other["skills"].get(k) == v for k, v in unchanged.items()):
                continue
            delta = {k: other[k] - base[k] for k in ("exact", "modification", "regression")}
            r["counterfactuals"][coordinate] = delta
            if delta["exact"] < 0 or delta["modification"] < -0.03 or delta["regression"] < -0.02:
                r["attribution_scores"][coordinate] += 2
            r["rationale"].append(f"Activation-audited paired {coordinate} contrast; one run per arm, not proof of causality.")
    return gathered


def generate_one(root, protocol, number, coordinate, records):
    from spreadsheet_harness.agent import _provider_client
    from spreadsheet_harness.config import ProviderConfig
    from spreadsheet_harness.evolution import _normalize_skill, generate_candidate
    work = root / f"round-{number:02d}/generation" / coordinate
    candidate = work / "candidates" / coordinate / "SKILL.md"
    if candidate.exists():
        _normalize_skill(candidate.read_text(), expected_name=SKILLS[coordinate])
        provenance = load(candidate.parent / "provenance.json")
        if provenance["candidate_sha256"] != digest(candidate):
            raise ValueError("Candidate changed since generation")
        return candidate
    selected = diverse_select(records, coordinate, protocol["generation_cases_per_coordinate"])
    if len({r["task_id"] for r in selected}) < 4:
        raise ValueError("Candidate needs evidence from at least four distinct development cases")
    context = {
        "coordinate": coordinate, "method": "activation-aware mechanism hypotheses and paired deltas",
        "selection_policy": "one trajectory per task; reserve controls; development only",
        "records": {r["trajectory"]: r for r in selected},
    }
    if (work / "attribution.json").exists():
        # A restart may see more baseline cells; keep the original evidence fixed
        # so an interrupted generation can resume its fingerprinted checkpoint.
        context = load(work / "attribution.json")
        selected = list(context["records"].values())
    else:
        write(work / "attribution.json", context)
    config = ProviderConfig(
        protocol["base_url"], KEY_FILE.read_text().strip(), protocol["generator"],
        api_protocol="chat-completions", reasoning_effort="medium", temperature=0, top_p=1,
        seed=41, enable_thinking=True, timeout_seconds=600, litellm_timeout_seconds=600,
        max_retries=2, request_interval_seconds=1.1,
    )
    for attempt in range(1, 4):
        try:
            with _provider_client(config) as client:
                generated = generate_candidate(
                    [r["trajectory"] for r in selected], work, client, candidate_id=coordinate,
                    skill_name=SKILLS[coordinate],
                    base_skill=root / "baseline-skills" / SKILLS[coordinate] / "SKILL.md",
                    lesson_max_output_tokens=None, consolidation_max_output_tokens=None,
                    attribution_context=context, evidence_max_items_per_category=8, format_retries=2,
                )
            log(root, "candidate.generated", round=number, coordinate=coordinate,
                case_count=len(selected), hash=generated.sha256)
            return generated.skill_path
        except Exception as exc:
            # No credential-bearing exception content is written.
            log(root, "candidate.retry", round=number, coordinate=coordinate,
                attempt=attempt, error_type=type(exc).__name__)
            if attempt == 3:
                raise RuntimeError(f"Candidate {coordinate} exhausted three attempts; checkpoints retained") from None


def rank_report(report):
    delta = report["delta"]
    return (report["qualifies"], report["wins"] - report["losses"],
            delta["modification"] if delta["modification"] is not None else -math.inf,
            delta["regression"] if delta["regression"] is not None else -math.inf)


def generation_process(root, number, coordinate, records):
    """Provider deadlines use POSIX signals: each generator runs on its own main thread."""
    work = root / f"round-{number:02d}/generation" / coordinate
    inputs = work / "input-records.json"
    if not inputs.exists():
        write(inputs, records)
    with (work / "worker.log").open("a") as handle:
        completed = subprocess.run(
            [str(PYTHON), str(root / "controller.py"), "--root", str(root),
             "--generate-coordinate", coordinate, "--round", str(number)],
            cwd=REPO, env=child_environment(root), stdout=handle, stderr=subprocess.STDOUT,
            timeout=21600,
        )
    candidate = work / "candidates" / coordinate / "SKILL.md"
    if completed.returncode or not candidate.is_file():
        raise RuntimeError(f"Generator {coordinate} failed; see {work / 'worker.log'}")
    return candidate


def set_status(root, state, **extra):
    write(root / "status.json", {"state": state, "time": stamp(), **extra})
    log(root, "phase", state=state, **extra)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--generate-coordinate", choices=tuple(SKILLS))
    parser.add_argument("--round", type=int, default=1)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.prepare:
        prepare(root)
        return
    if args.generate_coordinate:
        protocol = verify_snapshot(root)
        records = load(root / f"round-{args.round:02d}/generation" / args.generate_coordinate / "input-records.json")
        generate_one(root, protocol, args.round, args.generate_coordinate, records)
        return
    if not args.run:
        parser.error("Choose --prepare or --run")
    protocol = verify_snapshot(root)
    # Controller and imported harness are frozen together before paid calls.
    if Path(__file__).resolve() != root / "controller.py":
        os.execve(str(PYTHON), [str(PYTHON), str(root / "controller.py"), "--root", str(root), "--run"],
                  child_environment(root))
    import fcntl
    lock = (root / "controller.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (root / "final.json").exists():
        print("Experiment already terminal; no calls made.")
        return
    split = load(root / "split.json")["roles"]
    dev = split["development"]
    # Six fixed legacy development cases spanning both datasets and C1/C2/C3.
    pilot = []
    for dataset in DATASETS:
        for difficulty in ("C1", "C2", "C3"):
            pilot.append(next(t for t in dev[:12] if t["dataset"] == dataset and t["complexity"] == difficulty))
    try:
        build_arms_baseline = root / "baseline-composition.json"
        if not build_arms_baseline.exists():
            from spreadsheet_harness.plugins import PLUGEOLVE_SEED_COMPOSITION
            write(build_arms_baseline, {"schema_version": "plugevolve-composition-v1",
                                       **PLUGEOLVE_SEED_COMPOSITION.to_dict()})
        for number in range(1, protocol["max_rounds"] + 1):
            set_status(root, "candidate_generation_and_baseline_pilot", round=number)
            records = evidence_records(root, dev)
            # Development baseline pilot proceeds while GLM generates candidates.
            with cf.ThreadPoolExecutor(max_workers=4) as pool:
                baseline_future = pool.submit(batch, root, protocol, pilot, ["h0d0"], 0)
                futures = {k: pool.submit(generation_process, root, number, k, records) for k in SKILLS}
                candidates = {k: f.result() for k, f in futures.items()}
                baseline_pilot = baseline_future.result()
            build_arms(root, number, candidates)
            set_status(root, "development_factorial_pilot", round=number)
            pilot_rows = batch(root, protocol, pilot, list(ARM_EDITS)[1:], number)
            reports = {a: compare(baseline_pilot, [r for r in pilot_rows if r["arm"] == a], pilot)
                       for a in list(ARM_EDITS)[1:]}
            write(root / f"round-{number:02d}/pilot.json", reports)
            valid = [a for a, r in reports.items() if r["complete"]]
            if len(valid) < 2:
                set_status(root, "pilot_incomplete_no_effect_claim", round=number)
                continue
            finalists = sorted(valid, key=lambda a: rank_report(reports[a]), reverse=True)[:2]
            set_status(root, "expanded_development", round=number, finalists=finalists, cases=len(dev))
            dev_rows = batch(root, protocol, dev, ["h0d0", *finalists], number)
            development_reports = {a: compare([r for r in dev_rows if r["arm"] == "h0d0"],
                                              [r for r in dev_rows if r["arm"] == a], dev) for a in finalists}
            write(root / f"round-{number:02d}/development.json", development_reports)
            set_status(root, "selection", round=number, finalists=finalists, cases=len(split["selection"]))
            rows = batch(root, protocol, split["selection"], ["h0d0", *finalists], number)
            reports = {a: compare([r for r in rows if r["arm"] == "h0d0"],
                                 [r for r in rows if r["arm"] == a], split["selection"]) for a in finalists}
            write(root / f"round-{number:02d}/selection.json", reports)
            eligible = [a for a in finalists if reports[a]["qualifies"] and development_reports[a]["qualifies"]]
            if not eligible:
                log(root, "no_selection_winner", round=number)
                continue
            winner = max(eligible, key=lambda a: rank_report(reports[a]))
            decision = {"round": number, "arm": winner, "selection": reports[winner],
                        "skills": expected_skills(arm_paths(root, number, winner)[0], winner),
                        "time": stamp(), "test_feedback_must_not_enter_generation": True}
            decision_path = root / "locked-decision.json"
            if decision_path.exists() and load(decision_path)["skills"] != decision["skills"]:
                raise ValueError("Test winner cannot change after test opened")
            if not decision_path.exists():
                write(decision_path, decision)
            set_status(root, "sealed_test", round=number, winner=winner)
            rows = batch(root, protocol, split["test"], ["h0d0", winner], number)
            report = compare([r for r in rows if r["arm"] == "h0d0"],
                             [r for r in rows if r["arm"] == winner], split["test"])
            write(root / "final.json", {"winner": winner, "round": number, "test": report,
                                      "production_promoted": False})
            set_status(root, "test_complete" if report["complete"] else "test_incomplete", result=report)
            return
        write(root / "final.json", {"status": "no_qualifying_selection_winner", "rounds": protocol["max_rounds"],
                                  "test_opened": False, "production_promoted": False})
        set_status(root, "no_qualifying_selection_winner")
    except Exception as exc:
        set_status(root, "needs_attention", error_type=type(exc).__name__)
        raise


if __name__ == "__main__":
    main()
