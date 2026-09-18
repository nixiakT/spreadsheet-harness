#!/usr/bin/env python3
"""Method-aligned local search with resumable infrastructure abstention.

One skill-content revision at a time; every proposal includes an event-cited
hypothesis and append-only rule patch. Recomposition/synthesis are deliberately
not claimed by this adapter. Kernel and partner groups are frozen.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/data/zju-160/tongzeyuan/spreadsheet-harness")
SOURCE = REPO / "benchmarks/results/attributed-evolution-v3b-20260917"
DEFAULT = REPO / "benchmarks/results/evidence-gated-v4d-20260917"
PYTHON = REPO / ".venv/bin/python"
KEY_FILE = Path("/tmp/spreadsheet-harness-litellm.key")
FAILURE_TASK = "Financial_Model/fina_Fina_eus_564b8c19d3_my_financia_c1"
CONTROL_TASK = "Financial_Model/fina_Temp_02_04_c0"


def load(p):
    return json.loads(Path(p).read_text())


def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def write(p, value):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    os.replace(tmp, p)


def state(root, phase, **extra):
    write(root / "status.json", {"time": now(), "phase": phase, **extra})
    print(json.dumps({"time": now(), "phase": phase, **extra}, ensure_ascii=False), flush=True)


def tree(root):
    return {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob("SKILL.md"))}


def prepare(root, *, solver="qwen36-35b-a3b", probe_interval_seconds=600):
    if (root / "protocol.json").exists():
        verify(root)
        return
    if root.exists() and any(root.iterdir()):
        raise ValueError("Use a new directory; never overwrite partial or historical trials")
    root.mkdir(parents=True, exist_ok=True)
    # Reuse untouched family assignments; transfer/regression split declared NOW,
    # before obtaining any feedback from these families.
    prior = load(SOURCE / "split.json")
    selection = prior["roles"]["selection"]
    split = {
        "replay": prior["roles"]["development"],
        "transfer": selection[::2],
        "regression": selection[1::2],
        "test": prior["roles"]["test"],
    }
    seen = set()
    for context, tasks in split.items():
        families = {t["source_workbook"] for t in tasks}
        if seen & families:
            raise ValueError("Family overlap")
        seen |= families
        for t in tasks:
            t["role"] = context
    write(root / "split.json", split)
    # Solver implementation stays byte-for-byte identical to v3b. The only
    # overlays are evolution/proposal code, not the execution kernel.
    shutil.copytree(
        SOURCE / "source/spreadsheet_harness",
        root / "source/spreadsheet_harness",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("evolution.py", "evidence_gate.py"):
        shutil.copy2(
            REPO / "src/spreadsheet_harness" / name, root / "source/spreadsheet_harness" / name
        )
    shutil.copytree(SOURCE / "baseline-skills", root / "baseline-skills")
    shutil.copy2(SOURCE / "baseline-composition.json", root / "baseline-composition.json")
    shutil.copy2(REPO / "benchmarks/run_attributed_evolution.py", root / "runner.py")
    shutil.copy2(Path(__file__), root / "controller.py")
    seed = root / "versions/seed"
    shutil.copytree(root / "baseline-skills", seed / "skills")
    shutil.copy2(root / "baseline-composition.json", seed / "composition.json")
    write(
        seed / "version.json",
        {"parent": None, "skills": tree(seed / "skills"), "status": "incumbent"},
    )
    # Preserve the concrete pre-execution objection discovered during v4c
    # review. Do not present this as a measured performance rejection.
    from spreadsheet_harness.evidence_gate import patch_objections

    prior = REPO / "benchmarks/results/evidence-gated-v4c-20260917/trials/01/proposal-1.json"
    rejections = []
    if prior.is_file():
        proposal = load(prior)
        objections = patch_objections(proposal.get("patch", ""))
        if objections:
            rejections.append(
                {
                    "plugin": proposal["plugin"],
                    "intervention": proposal["intervention"],
                    "decision": {
                        "status": "rejected_before_execution",
                        "reasons": objections,
                        "performance_measured": False,
                    },
                }
            )
    write(
        root / "search.json",
        {
            "incumbent": "seed",
            "completed_trials": 0,
            "preferred_group": "D",
            "phase": "ready",
            "rejections": rejections,
        },
    )
    immutable = [
        root / "controller.py",
        root / "runner.py",
        root / "split.json",
        root / "baseline-composition.json",
        *(root / "source").rglob("*.py"),
        *(root / "baseline-skills").rglob("SKILL.md"),
    ]
    protocol = {
        "version": "method-evidence-gate-v4",
        "created": now(),
        "base_url": "http://10.130.138.46:8010/v1",
        "solver": solver,
        "generator": "dashscope/glm-5.2",
        "max_calls": 50,
        "temperature": 0,
        "top_p": 1,
        "thinking": True,
        "seed": 41,
        "request_timeout": 600,
        "task_timeout": 3600,
        "parallelism": 2,
        "recover_infrastructure": True,
        "max_completed_trials": 4,
        "max_provider_recovery_hours": 24,
        "probe_interval_seconds": probe_interval_seconds,
        "max_attempts_per_cell": 5,
        "gate": {
            "replay_margin": 0.0,
            "transfer_margin": 0.0,
            "regression_tolerance": 0.0,
            "confidence": 0.95,
            "bootstrap_samples": 4000,
            "min_families": 4,
        },
        "objective": "Exact workbook success, family-uniform macro average",
        "operators_implemented": ["revision"],
        "operators_deferred": ["recomposition", "synthesis"],
        "proposal_surface": "one active SKILL.md; append-only <=1800 chars",
        "hashes": {str(p.relative_to(root)): digest(p) for p in sorted(immutable)},
        "datasets": load(SOURCE / "protocol.json")["datasets"],
        "test_policy": "test once after final incumbent freeze; no test feedback to proposer",
        "scope": "pilot and development study, not a claim of demonstrated evolution gain",
    }
    write(root / "protocol.json", protocol)
    state(root, "prepared", contexts={k: len(v) for k, v in split.items()})


def verify(root):
    p = load(root / "protocol.json")
    for rel, h in p["hashes"].items():
        if digest(root / rel) != h:
            raise ValueError("Frozen experiment changed: " + rel)
    for meta in p["datasets"].values():
        if digest(Path(meta["root"]) / "Financial_Model/dataset.json") != meta["metadata_sha256"]:
            raise ValueError("Dataset metadata changed")
    return p


def provider(protocol, model):
    from spreadsheet_harness.config import ProviderConfig

    return ProviderConfig(
        protocol["base_url"],
        KEY_FILE.read_text().strip(),
        model,
        api_protocol="chat-completions",
        reasoning_effort="medium",
        timeout_seconds=600,
        litellm_timeout_seconds=600,
        max_retries=0,
        temperature=0,
        top_p=1,
        seed=41,
        enable_thinking=True,
    )


def solver_health(protocol):
    """A real tool-call probe, not /models availability; no max_tokens override."""
    import httpx

    body = {
        "model": protocol["solver"],
        "temperature": 0,
        "top_p": 1,
        "chat_template_kwargs": {"enable_thinking": True},
        "messages": [{"role": "user", "content": 'Call report_status with status="ok".'}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "report_status",
                    "description": "Connectivity probe result.",
                    "parameters": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"],
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "report_status"}},
    }
    try:
        with httpx.Client(timeout=45, trust_env=False) as c:
            r = c.post(
                protocol["base_url"] + "/chat/completions",
                json=body,
                headers={"Authorization": "Bearer " + KEY_FILE.read_text().strip()},
            )
        if r.status_code != 200:
            return {"healthy": False, "http_status": r.status_code}
        choice = r.json()["choices"][0]
        calls = choice["message"].get("tool_calls", [])
        ok = any(x.get("function", {}).get("name") == "report_status" for x in calls)
        return {"healthy": ok, "http_status": 200, "finish_reason": choice.get("finish_reason")}
    except Exception as exc:
        return {"healthy": False, "error_type": type(exc).__name__}


def ensure_health(root, protocol, pending):
    check = solver_health(protocol)
    if not check["healthy"]:
        state(
            root,
            "paused_infrastructure",
            pending=pending,
            health=check,
            completed_trials=load(root / "search.json")["completed_trials"],
        )
        return False
    state(root, "provider_recovered", pending=pending)
    return True


def old_evidence(root):
    from spreadsheet_harness.evidence_gate import evidence_packet
    from spreadsheet_harness.evolution import extract_trajectory_evidence

    allowed = {t["task_id"] for t in load(root / "split.json")["replay"]}
    packets = []
    # Prefer current-version replay; also retain old candidate mistakes as explicitly
    # observational negative evidence. No transfer/regression/test trajectories.
    for source in (SOURCE, root):
        for p in sorted((source / "runs").rglob("cell.json")):
            row = load(p)
            if row["task"]["task_id"] not in allowed or row["task"]["role"] not in {
                "development",
                "replay",
            }:
                continue
            if row["status"] != "scored" or not row.get("trajectory"):
                continue
            evidence = extract_trajectory_evidence(row["trajectory"], max_items_per_category=8)
            packet = evidence_packet(evidence)
            if packet["classification"] != "evaluated":
                continue
            packet.update(
                task_id=row["task"]["task_id"],
                source_version=row.get("arm"),
                source_kind="current_search" if source == root else "historical_observation",
                task_outcome=row["exact"],
            )
            packets.append(packet)
    # Deduplicate by content, not path.
    return list({p["evidence_id"]: p for p in packets}.values())


def model_json(client, protocol, instructions, data):
    from spreadsheet_harness.evolution import proposer_visible

    response = client.create(
        client.config.apply_generation(
            {
                "model": protocol["generator"],
                "instructions": instructions,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": json.dumps(proposer_visible(data), ensure_ascii=False),
                            }
                        ],
                    }
                ],
                "store": False,
            }
        )
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("Expected an object")
    return result


def create_trial(root, protocol, search):
    from spreadsheet_harness.agent import _provider_client
    from spreadsheet_harness.evidence_gate import (
        coordinate_breaches,
        patch_objections,
        route_options,
        validate_hypothesis,
    )
    from spreadsheet_harness.evolution import _normalize_skill, proposer_visible

    number = search["completed_trials"] + 1
    trial = root / f"trials/{number:02d}"
    if (trial / "trial.json").exists():
        return load(trial / "trial.json")
    packets = old_evidence(root)
    options = route_options(packets, search["preferred_group"])
    if not options:
        state(root, "deferred_ambiguous_evidence", completed_trials=search["completed_trials"])
        return None
    parent = root / "versions" / search["incumbent"]
    parent_meta = load(parent / "version.json")
    if tree(parent / "skills") != parent_meta["skills"]:
        raise ValueError("Incumbent skill contents changed")
    # Avoid repeating an identical intervention after rejection; alternate eligible groups.
    route = options[0]
    motivating_ids = {r["evidence_id"] for r in route["evidence"]}
    relevant = [p for p in packets if p["evidence_id"] in motivating_ids]
    controls = [p for p in packets if p.get("task_outcome") == 1]
    motivating_pair = [p for p in relevant if p.get("task_id") == FAILURE_TASK]
    chosen = (motivating_pair[-2:] if motivating_pair else relevant[-2:]) + controls[:1]
    # The router can inspect all replay evidence, but a proposal must only cite
    # the bounded packet that it actually receives.
    chosen_ids = {p["evidence_id"] for p in chosen}
    route = {**route, "evidence": [e for e in route["evidence"] if e["evidence_id"] in chosen_ids]}
    request = {
        "route": route,
        "evidence": chosen,
        "parent_version": search["incumbent"],
        "base_skill": (parent / "skills" / route["plugin"] / "SKILL.md").read_text(),
        "rejection_memory": search["rejections"][-4:],
        "proposal_schema": {
            **{
                k: "string"
                for k in (
                    "group",
                    "operator",
                    "plugin",
                    "observation",
                    "suspected_cause",
                    "intervention",
                    "prediction",
                    "falsification",
                    "patch",
                )
            },
            "citations": [{"evidence_id": "existing sha256", "event_index": 1}],
            "count_claims": [
                {"evidence_id": "existing sha256", "field": "diagnostic counter", "value": 0}
            ],
            "prediction_checks": [{"metric": "modification", "relation": "increase"}],
        },
    }
    if not (trial / "proposal-request.json").exists():
        write(trial / "proposal-request.json", proposer_visible(request))
    else:
        request = load(trial / "proposal-request.json")
        chosen, route = request["evidence"], request["route"]
    proposal = None
    for attempt in range(1, 4):
        saved = trial / f"proposal-{attempt}.json"
        if saved.exists():
            response = load(saved)
        else:
            try:
                state(
                    root,
                    "generating_hypothesis",
                    trial=number,
                    attempt=attempt,
                    request_chars=len(json.dumps(request, ensure_ascii=False)),
                )
                with _provider_client(provider(protocol, protocol["generator"])) as client:
                    response = model_json(
                        client,
                        protocol,
                        "Return JSON only matching proposal_schema. Propose ONE local append-only plugin patch. "
                        "Use cited observed events, not fabricated causes. Planner verified means persisted, not correct. "
                        "Do not claim zero writes if counters show writes. Reference answer values are withheld. "
                        "Do not encode case-specific cell addresses, answer numbers, workbook names or fitted constants. "
                        "patch must be <=1800 characters of reusable operating instructions, no frontmatter. "
                        "Preserve the base skill. Do not broaden allowed edits or require nonzero results generally. "
                        "Separate a plausible cause from a proven cause. Include a concrete falsification test. "
                        "Include 1-3 prediction_checks comparing candidate to incumbent on the motivating failure. "
                        "Allowed metrics: exact, modification, regression, tool_errors, max_readonly_code_streak, "
                        "code_changes, planner_applied_writes; relations: increase, decrease, not_decrease, not_increase. "
                        "At least one citation must match route.evidence event_indices: repair that mechanism, "
                        "not an unrelated generic workflow symptom. For D, focus on financial formula semantic "
                        "grounding in existing row labels and dependencies, not a code-call quota. "
                        "The executor NEVER sees external evaluator messages, references, or scores. "
                        "Do not require external regression-error feedback during execution. "
                        "Never force an unsupported edit or premature submission to satisfy a call quota. "
                        "Pending changes can originate from planner actions or any editing tool, not only code. "
                        "Read rejection_memory; change the remedy if it was tested and rejected.",
                        request,
                    )
                write(saved, proposer_visible(response))
            except (ValueError, TypeError) as exc:
                request["previous_validation_error"] = str(exc)[:500]
                write(trial / f"format-error-{attempt}.json", {"error_type": type(exc).__name__})
                continue
            except Exception as exc:
                state(
                    root,
                    "paused_generation",
                    trial=number,
                    error_type=type(exc).__name__,
                    http_status=getattr(exc, "status_code", None),
                    provider_phase=getattr(exc, "phase", None),
                )
                return None
        try:
            hypothesis = validate_hypothesis(response, route, chosen)
            if not 1 <= len(response.get("prediction_checks", [])) <= 3:
                raise ValueError("Provide 1-3 machine-checkable prediction_checks")
            patch = response.get("patch")
            if (
                not isinstance(patch, str)
                or not patch.strip()
                or len(patch) > 1800
                or "---" in patch
            ):
                raise ValueError("Invalid bounded patch")
            objections = patch_objections(patch)
            if objections:
                raise ValueError(
                    "Unsafe or unavailable runtime assumptions: " + ", ".join(objections)
                )
            # Ban the observed benchmark names/addresses from reusable instructions.
            import re

            if re.search(r"\b[A-Z]{1,3}[1-9][0-9]*\b|answer\s*=|golden|fina_", patch, re.I):
                raise ValueError("Case-specific identifiers or references in patch")
            proposal = (hypothesis, patch)
            break
        except ValueError as exc:
            request["previous_validation_error"] = str(exc)
            write(
                trial / f"proposal-rejected-{attempt}.json",
                {
                    "reason": str(exc),
                    "status": "rejected_before_execution",
                    "counts_as_evolution_trial": False,
                },
            )
    if proposal is None:
        state(root, "proposal_validation_failed", trial=number)
        return None
    hypothesis, patch = proposal
    candidate = f"trial-{number:02d}"
    version = root / "versions" / candidate
    if version.exists():
        raise ValueError("Unsealed partial candidate exists")
    shutil.copytree(parent / "skills", version / "skills")
    skill = version / "skills" / route["plugin"] / "SKILL.md"
    content = (
        skill.read_text().rstrip()
        + "\n\n## Evidence-guided local correction\n\n"
        + patch.strip()
        + "\n"
    )
    _normalize_skill(content, expected_name=route["plugin"])
    skill.write_text(content)
    shutil.copy2(parent / "composition.json", version / "composition.json")
    breaches = coordinate_breaches(
        tree(parent / "skills"), tree(version / "skills"), route["plugin"], route["group"]
    )
    if breaches:
        raise ValueError("Candidate breached frozen partner: " + repr(breaches))
    metadata = {
        "parent": search["incumbent"],
        "skills": tree(version / "skills"),
        "hypothesis": hypothesis,
        "status": "candidate",
        "contract_breaches": breaches,
    }
    write(version / "version.json", metadata)
    record = {
        "number": number,
        "candidate": candidate,
        "parent": search["incumbent"],
        "hypothesis": hypothesis,
        "phase": "smoke",
        "created": now(),
    }
    write(trial / "trial.json", record)
    state(root, "candidate_ready", trial=number, group=route["group"], plugin=route["plugin"])
    return record


def audit_version(root, version):
    folder = root / "versions" / version
    meta = load(folder / "version.json")
    if tree(folder / "skills") != meta["skills"]:
        raise ValueError("Version skill contents changed")
    if meta["parent"] is None and digest(folder / "composition.json") != digest(
        root / "baseline-composition.json"
    ):
        raise ValueError("Seed composition changed")
    if meta["parent"]:
        parent = root / "versions" / meta["parent"]
        h = meta["hypothesis"]
        from spreadsheet_harness.evidence_gate import coordinate_breaches

        if coordinate_breaches(tree(parent / "skills"), meta["skills"], h["plugin"], h["group"]):
            raise ValueError("Frozen coordinate violated")
        if digest(folder / "composition.json") != digest(parent / "composition.json"):
            raise ValueError("Content revision changed composition")


def run_version_cell(root, protocol, task, version):
    import runner

    audit_version(root, version)

    # Reuse the tested solver adapter with per-version immutable skill roots.
    def paths(_root, _number, arm):
        return root / "versions" / arm / "skills", root / "versions" / arm / "composition.json"

    runner.arm_paths = paths
    return runner.run_cell(root, protocol, task, version, 0)


def checked_batch(root, protocol, tasks, versions, pending):
    """At most two in flight; stop scheduling as soon as infra/unknown error appears."""
    jobs = []
    rows = []
    for task in tasks:
        for version in versions:
            cell = (
                root
                / "runs/round-00"
                / version
                / task["role"]
                / task["dataset"]
                / task["task_id"].replace("/", "_")
                / "cell.json"
            )
            if cell.exists():
                row = load(cell)
                if row["status"] == "scored":
                    rows.append(row)
                    continue
                if row["status"] != "infrastructure":
                    state(root, "needs_attention", cell=str(cell), reason=row["status"])
                    return None
                if row["attempt"] >= protocol["max_attempts_per_cell"]:
                    state(root, "needs_attention", cell=str(cell), reason="recovery_attempt_budget")
                    return None
            jobs.append((task, version))
    if jobs and not ensure_health(root, protocol, pending):
        return None
    for start in range(0, len(jobs), protocol["parallelism"]):
        with cf.ThreadPoolExecutor(max_workers=protocol["parallelism"]) as pool:
            futures = [
                pool.submit(run_version_cell, root, protocol, t, v)
                for t, v in jobs[start : start + protocol["parallelism"]]
            ]
            wave = [f.result() for f in futures]
        rows.extend(wave)
        if any(row["status"] != "scored" for row in wave):
            state(
                root,
                "paused_infrastructure"
                if all(r["status"] in {"scored", "infrastructure"} for r in wave)
                else "needs_attention",
                pending=pending,
            )
            return None
        state(
            root,
            "evaluating",
            pending=pending,
            completed=len(rows),
            scheduled=len(tasks) * len(versions),
        )
    return rows


def pairs(rows, incumbent, candidate):
    bytask = {}
    for r in rows:
        bytask.setdefault(r["task"]["task_id"], {})[r["arm"]] = r
    return [
        {
            "task_id": key,
            "family": arms[incumbent]["task"]["source_workbook"],
            "incumbent": arms[incumbent]["exact"],
            "candidate": arms[candidate]["exact"],
            "incumbent_status": arms[incumbent]["status"],
            "candidate_status": arms[candidate]["status"],
        }
        for key, arms in bytask.items()
    ]


def smoke_result(rows, incumbent, candidate, hypothesis=None):
    p = pairs(rows, incumbent, candidate)
    failure = next(x for x in p if x["task_id"] == FAILURE_TASK)
    control = next(x for x in p if x["task_id"] == CONTROL_TASK)
    bytask = {(r["task"]["task_id"], r["arm"]): r for r in rows}
    a, b = bytask[(FAILURE_TASK, incumbent)], bytask[(FAILURE_TASK, candidate)]
    no_regression = all(
        bytask[(x["task_id"], candidate)][metric]
        >= bytask[(x["task_id"], incumbent)][metric] - 0.02
        for x in p
        for metric in ("exact", "modification", "regression")
    )
    progress = b["exact"] > a["exact"] or b["modification"] > a["modification"] + 0.03
    predictions = []
    if hypothesis:
        from spreadsheet_harness.evidence_gate import check_predictions
        from spreadsheet_harness.evolution import extract_trajectory_evidence

        observed = []
        for row in (a, b):
            evidence = extract_trajectory_evidence(row["trajectory"], max_items_per_category=8)
            observed.append(
                {
                    **evidence.diagnostics,
                    **{k: row[k] for k in ("exact", "modification", "regression")},
                }
            )
        predictions = check_predictions(hypothesis, *observed)
    return {
        "status": "advance" if progress and no_regression else "rejected_smoke",
        "pairs": p,
        "failure_repaired": failure["candidate"] > failure["incumbent"],
        "control_preserved": control["candidate"] >= control["incumbent"],
        "not_promotion": True,
        "modification_delta": b["modification"] - a["modification"],
        "regression_delta": b["regression"] - a["regression"],
        "hypothesis_checks": predictions,
        "mechanism_evidence_note": "Observational paired checks; a score improvement alone does not prove the proposed cause.",
    }


def complete_trial(root, record, decision):
    from spreadsheet_harness.evidence_gate import transition

    search = load(root / "search.json")
    if search["completed_trials"] >= record["number"]:
        return
    updated = transition(search, decision, record["candidate"])
    if decision["status"] != "accepted":
        updated["rejections"] = search["rejections"] + [
            {
                "hypothesis_id": record["hypothesis"]["id"],
                "plugin": record["hypothesis"]["plugin"],
                "intervention": record["hypothesis"]["intervention"],
                "decision": decision,
                "parent": record["parent"],
            }
        ]
    else:
        version = root / "versions" / record["candidate"] / "version.json"
        meta = load(version)
        meta["status"] = "accepted"
        write(version, meta)
        updated["accepted_count"] = search.get("accepted_count", 0) + 1
    updated["preferred_group"] = "D" if record["hypothesis"]["group"] == "H" else "H"
    write(root / "search.json", updated)


def run_once(root, protocol):
    from spreadsheet_harness.evidence_gate import GatePolicy, promotion_gate

    search = load(root / "search.json")
    if (root / "final.json").exists():
        return True
    if (root / "locked-test-version.json").exists():
        return finish_test(root, protocol)
    if search["completed_trials"] >= protocol["max_completed_trials"]:
        if search["incumbent"] == "seed":
            write(
                root / "final.json",
                {
                    "status": "search_budget_exhausted",
                    "incumbent": "seed",
                    "test_opened": False,
                    "not_success": True,
                },
            )
            state(root, "search_budget_exhausted")
            return True
        write(root / "locked-test-version.json", {"version": search["incumbent"], "time": now()})
        return finish_test(root, protocol)
    record = create_trial(root, protocol, search)
    if record is None:
        return False
    trial = root / f"trials/{record['number']:02d}"
    split = load(root / "split.json")
    smoke = [t for t in split["replay"] if t["task_id"] in {FAILURE_TASK, CONTROL_TASK}]
    if not (trial / "smoke.json").exists():
        rows = checked_batch(
            root, protocol, smoke, [record["parent"], record["candidate"]], "smoke"
        )
        if rows is None:
            return False
        report = smoke_result(rows, record["parent"], record["candidate"], record["hypothesis"])
        write(trial / "smoke.json", report)
    smoke_report = load(trial / "smoke.json")
    if smoke_report["status"] != "advance":
        decision = {
            "status": "rejected",
            "reason": "hypothesis_not_supported_in_smoke",
            "smoke": smoke_report,
        }
        write(trial / "decision.json", decision)
        complete_trial(root, record, decision)
        state(root, "trial_rejected", trial=record["number"])
        return False
    contexts = {}
    expected = {}
    for context in ("replay", "transfer", "regression"):
        rows = checked_batch(
            root, protocol, split[context], [record["parent"], record["candidate"]], context
        )
        if rows is None:
            return False
        contexts[context] = pairs(rows, record["parent"], record["candidate"])
        expected[context] = {t["task_id"]: t["source_workbook"] for t in split[context]}
        write(trial / f"{context}.json", {"pairs": contexts[context]})
    audit_version(root, record["candidate"])
    decision = promotion_gate(contexts, expected, GatePolicy(**protocol["gate"]))
    write(trial / "decision.json", decision)
    if decision["status"] == "deferred":
        state(root, "deferred_evidence", trial=record["number"])
        return False
    complete_trial(root, record, decision)
    if decision["status"] != "accepted":
        state(root, "trial_rejected", trial=record["number"])
        return False
    # The next proposal starts from the accepted version, with the new partner
    # frozen. Test remains sealed until the fixed search budget is exhausted.
    state(root, "trial_accepted", trial=record["number"], incumbent=record["candidate"])
    return False


def finish_test(root, protocol):
    version = load(root / "locked-test-version.json")["version"]
    split = load(root / "split.json")
    rows = checked_batch(root, protocol, split["test"], ["seed", version], "sealed_test")
    if rows is None:
        return False
    finalpairs = pairs(rows, "seed", version)
    write(
        root / "final.json",
        {
            "status": "test_complete",
            "version": version,
            "pairs": finalpairs,
            "production_promoted": False,
            "exact_delta": sum(r["candidate"] - r["incumbent"] for r in finalpairs)
            / len(finalpairs),
        },
    )
    state(root, "test_complete")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--solver", default="qwen36-35b-a3b")
    parser.add_argument("--probe-interval-seconds", type=int, default=600)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.prepare:
        if args.probe_interval_seconds < 5:
            parser.error("--probe-interval-seconds must be at least 5")
        prepare(
            root,
            solver=args.solver,
            probe_interval_seconds=args.probe_interval_seconds,
        )
        return
    protocol = verify(root)
    if Path(__file__).resolve() != root / "controller.py":
        env = dict(os.environ)
        for k in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            env.pop(k, None)
        env["PYTHONPATH"] = str(root / "source")
        command = [str(PYTHON), str(root / "controller.py"), "--root", str(root), "--run"]
        if args.watch:
            command.append("--watch")
        os.execve(str(PYTHON), command, env)
    import fcntl

    lock = (root / "controller.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started = time.monotonic()
    while True:
        try:
            verify(root)
            finished = run_once(root, protocol)
        except Exception as exc:
            state(root, "needs_attention", error_type=type(exc).__name__)
            raise
        if finished or not args.watch:
            break
        phase = load(root / "status.json")["phase"]
        if phase not in {
            "paused_infrastructure",
            "paused_generation",
            "trial_rejected",
            "trial_accepted",
        }:
            break
        if time.monotonic() - started > protocol["max_provider_recovery_hours"] * 3600:
            state(
                root,
                "paused_recovery_window_exhausted",
                incumbent=load(root / "search.json")["incumbent"],
            )
            break
        delay = protocol["probe_interval_seconds"] if phase.startswith("paused_") else 1
        # Short waits allow graceful termination; no work/round advancement in outage.
        while delay > 0:
            interval = min(delay, 45)
            time.sleep(interval)
            delay -= interval


if __name__ == "__main__":
    main()
