#!/usr/bin/env python3
"""Run Fin-1.5K trace-grounded candidates directly on a V2 canary.

This is deliberately not a Fin-1.5K promotion gate.  Fin-1.5K supplies the
trace/profile evidence and GLM proposals; every materialized candidate is then
sent directly to a fixed SpreadsheetBench-v2 task screen.  Existing bare,
basic, and financial results are treated as external comparison data and are
never rerun here.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from spreadsheet_harness.continuous_evolution import (
    CandidateProposal,
    ContinuousEvolutionConfig,
    EvolutionRoute,
    RevisionStore,
    _run_adapter_command,
)
from spreadsheet_harness.plugins import CompositionSpec, default_plugin_registry


REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_ROOT = REPO / (
    "benchmarks/results/fin15k-plugin-evolution-20260920-deepseek-"
    "formal-v2-500-profiled-v7-20260922"
)
DATASET = REPO / "benchmarks/data/spreadsheetbench-v2"
EVALUATOR = REPO / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py"
KEY_FILE = Path("/tmp/spreadsheet-harness-litellm.key")
BASE_URL = "http://10.130.138.46:8010/v1"
MODEL = "dashscope/deepseek-v4-flash-0731"
DEFAULT_TASK_TIMEOUT = 21600
DEFAULT_REQUEST_TIMEOUT = 1800
DEFAULT_LITELLM_TIMEOUT = 1800
MECHANISMS = ("h-only", "d-only", "joint")
SCOPE_BY_MECHANISM = {
    "h-only": "general-only",
    "d-only": "domain-only",
    "joint": "coevolution",
}


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def proposer_candidates(
    profile_root: Path,
    mechanism: str,
    count: int,
) -> list[CandidateProposal]:
    """Ask GLM for alternatives using the already frozen Fin trace request."""

    scope = SCOPE_BY_MECHANISM[mechanism]
    config = ContinuousEvolutionConfig.load(
        profile_root / "configs" / f"fin15k-500-{scope}.json"
    )
    workspace = profile_root / "workspaces" / f"fin15k-500-{scope}"
    original = read(workspace / "rounds/000001/proposal/request.json")
    request = dict(original)
    request["candidate_limit"] = count
    policy = dict(request.get("operator_policy") or {})
    policy["instruction"] = (
        f"Return exactly up to {count} materially different, contract-valid candidates. "
        "All candidates must use the deterministic Fin-1.5K trace route and must not use "
        "SpreadsheetBench scores as evidence."
    )
    request["operator_policy"] = policy
    round_dir = profile_root / "candidate-screen" / "proposals" / mechanism
    document = _run_adapter_command(
        config.proposer_command,
        request=request,
        directory=round_dir,
        timeout=config.command_timeout_seconds,
    )
    raw = document.get("candidates")
    if not isinstance(raw, list):
        raise RuntimeError(f"GLM response for {mechanism} has no candidates list")
    proposals: list[CandidateProposal] = []
    seen: set[str] = set()
    for index, item in enumerate(raw[:count], start=1):
        if not isinstance(item, dict):
            continue
        proposal = CandidateProposal.from_document(item)
        # Candidate IDs are workspace-local.  Keep the original proposal
        # unchanged except for a deterministic unique storage identifier.
        candidate_id = proposal.candidate_id
        if candidate_id in seen or candidate_id == "r001-edit":
            candidate_id = f"r001-cand{index:02d}"
        seen.add(candidate_id)
        if candidate_id != proposal.candidate_id:
            proposal = dataclasses.replace(proposal, candidate_id=candidate_id)
        proposals.append(proposal)
    return proposals


def _composition_variant(
    profile_root: Path,
    mechanism: str,
    variant_id: str,
    *,
    remove: str | None = None,
    replace: tuple[str, str] | None = None,
    add: str | None = None,
) -> dict[str, Any]:
    """Materialize a composition-only trace-grounded candidate.

    These are not invented code patches: they are the deterministic
    disable/replace/synthesis actions supported by the plugin contract.  The
    evidence for them is the frozen Fin-1.5K profile (profile truncation,
    dormant-plugin ablation, and multi-plugin coordination failures).
    """
    scope = SCOPE_BY_MECHANISM[mechanism]
    workspace = profile_root / "workspaces" / f"fin15k-500-{scope}"
    store = RevisionStore(workspace)
    state = store.load_state()
    incumbent = str(state["current_revision_sha256"])
    base_dir = store.revision_dir(incumbent)
    base_doc = read(base_dir / "composition.json")
    plugins = list(base_doc["plugins"])
    if remove is not None:
        plugins = [item for item in plugins if item != remove]
    if replace is not None:
        old, new = replace
        plugins = [new if item == old else item for item in plugins]
    if add is not None and add not in plugins:
        plugins.append(add)
    composition = CompositionSpec.create(
        f"fin15k-{mechanism}-{variant_id}", plugins, dict(base_doc.get("overrides") or {})
    )
    registry = default_plugin_registry()
    destination = profile_root / "candidate-screen" / "variants" / mechanism / variant_id
    if destination.exists():
        revision = read(destination / "revision.json")
        return {
            "mechanism": mechanism, "candidate_id": variant_id,
            "candidate_dir": str(destination.resolve()),
            "revision_sha256": revision["revision_sha256"],
            "source": "trace-grounded-composition-variant",
        }
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{variant_id}-", dir=destination.parent))
    try:
        shutil.copytree(base_dir / "artifact", staging / "artifact")
        revision = store._finalize_revision(
            staging, parent_revision=incumbent, composition=composition,
            registry=registry,
            mutation={
                "schema_version": "trace-grounded-composition-variant-v1",
                "mechanism": mechanism, "variant_id": variant_id,
                "remove": remove, "replace": replace, "add": add,
                "evidence_source": "Fin-1.5K plugin profile/trace",
            },
        )
        (staging / "proposal.json").write_text(json.dumps({
            "candidate_id": variant_id, "base_revision_sha256": incumbent,
            "operation": "disable" if remove else "replace" if replace else "synthesize",
            "target_plugin": (remove or (replace[0] if replace else add)),
            "replacement_plugin": replace[1] if replace else None,
            "scope": mechanism, "rationale": "Trace-grounded composition ablation/variant",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "mechanism": mechanism, "candidate_id": variant_id,
        "candidate_dir": str(destination.resolve()),
        "revision_sha256": revision["revision_sha256"],
        "source": "trace-grounded-composition-variant",
    }


def materialize_candidates(profile_root: Path, mechanism: str, count: int) -> list[dict[str, Any]]:
    scope = SCOPE_BY_MECHANISM[mechanism]
    config = ContinuousEvolutionConfig.load(
        profile_root / "configs" / f"fin15k-500-{scope}.json"
    )
    workspace = profile_root / "workspaces" / f"fin15k-500-{scope}"
    store = RevisionStore(workspace)
    state = store.load_state()
    incumbent = str(state["current_revision_sha256"])
    route = EvolutionRoute.from_dict(
        read(workspace / "rounds/000001/proposal/request.json")["route"]
    )
    registry = default_plugin_registry()
    records: list[dict[str, Any]] = []
    # Preserve the already-materialized candidate as candidate 01.  It is a
    # valid trace-grounded proposal and avoids wasting a second GLM call.
    existing = workspace / "candidates/r000001-r001-edit"
    if existing.is_dir() and (existing / "revision.json").is_file():
        revision = read(existing / "revision.json")
        records.append({
            "mechanism": mechanism,
            "candidate_id": "r000001-r001-edit",
            "candidate_dir": str(existing.resolve()),
            "revision_sha256": revision["revision_sha256"],
            "source": "existing-trace-grounded-proposal",
        })
    # The first materialized candidate is the actual GLM proposal.  The next
    # candidates are deterministic actions selected from the same profile;
    # none consumes SpreadsheetBench feedback or changes the Fin evidence.
    if mechanism == "h-only":
        variants = [
            ("h-profile-full", None, ("observe-profile-compact", "observe-profile-full"), None),
            ("h-disable-memory", "knowledge-memory", None, None),
        ]
    elif mechanism == "d-only":
        variants = [("d-disable-financial", "knowledge-financial-model", None, None)]
    else:
        variants = [
            ("joint-add-coordination", None, None, "knowledge-coordination"),
            ("joint-profile-full", None, ("observe-profile-compact", "observe-profile-full"), None),
        ]
    for variant_id, remove, replace, add in variants:
        if len(records) >= count:
            break
        records.append(_composition_variant(
            profile_root, mechanism, variant_id, remove=remove, replace=replace, add=add
        ))
    return records[:count]


def choose_tasks(per_category: int | None = 2) -> list[str]:
    """Fixed non-visual canary with a balanced number per category."""

    selected: list[str] = []
    for category in ("Debugging", "Financial_Model", "Template"):
        rows = read(DATASET / category / "dataset.json")
        selected_rows = rows if per_category is None else rows[:per_category]
        for row in selected_rows:
            selected.append(f"{category}/{row['id']}")
    return selected


def run_task(job: tuple[dict[str, Any], str, Path, bool, int]) -> dict[str, Any]:
    candidate, task_id, output_root, retry_unscored, task_timeout = job
    candidate_dir = Path(candidate["candidate_dir"])
    category = task_id.split("/", 1)[0]
    output = output_root / candidate["mechanism"] / candidate["candidate_id"] / task_id.replace("/", "__")
    summary = output / "summary.json"
    if summary.is_file() and not retry_unscored:
        return {"candidate": candidate, "task_id": task_id, "status": "existing", "output": str(output)}
    if summary.is_file() and retry_unscored:
        # Retry every incomplete/not-scored result.  The evaluator writes a
        # summary with ``completed=0``/``errors>0`` for several infrastructure
        # and provider paths without a stable ``outcome_kind`` marker, so
        # marker-only filtering silently left ``not_scored`` tasks behind.
        # Completed tasks remain immutable and are reused below.
        retryable = False
        try:
            prior_summary = read(summary)
            arm = (prior_summary.get("arms") or {}).get("ours") or {}
            retryable = (
                int(arm.get("completed", 0) or 0) < int(arm.get("expected", 1) or 1)
                or int(arm.get("errors", 0) or 0) > 0
                or arm.get("scored_accuracy") is None
            )
        except Exception:
            retryable = True
        result_file = output / "results.json"
        if not result_file.is_file():
            retryable = True
        elif not retryable:
            try:
                records = read(result_file)
                records = records if isinstance(records, list) else [records]
                for record in records:
                    kind = str(record.get("outcome_kind", "")).lower() if isinstance(record, dict) else ""
                    # A cached result is complete only when the model run was
                    # actually scored.  Earlier recovery runs required an
                    # additional provider-error keyword, which incorrectly
                    # preserved not_scored/model_execution_failure rows and
                    # labelled them as existing-zero.
                    if kind != "scored":
                        retryable = True
                        break
            except Exception:
                retryable = True
        if not retryable:
            return {"candidate": candidate, "task_id": task_id, "status": "existing", "output": str(output)}
        prior = output.with_name(output.name + ".prior-unscored")
        if not prior.exists():
            os.replace(output, prior)
        else:
            shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "spreadsheet_harness.cli", "benchmark", "v2-compare",
        "--dataset", str(DATASET), "--official-evaluator", str(EVALUATOR),
        "--category", category, "--task-id", task_id, "--arm", "ours",
        "--composition-file", f"ours={candidate_dir / 'composition.json'}",
        "--skill-root", str(candidate_dir / "artifact/skills"), "--output", str(output),
        "--max-model-calls", "50", "--max-turns-per-arm", "50",
        "--max-total-tokens", "unlimited", "--max-output-tokens", "unlimited",
        "--task-timeout", str(task_timeout), "--request-timeout", str(DEFAULT_REQUEST_TIMEOUT),
        "--litellm-timeout", str(DEFAULT_LITELLM_TIMEOUT), "--request-retries", "3",
        "--arm-order-seed", "20260923", "--base-url", BASE_URL,
        "--api-key-file", str(KEY_FILE), "--model", MODEL,
        "--api-protocol", "chat-completions", "--reasoning-effort", "medium",
        "--temperature", "0", "--top-p", "1", "--enable-thinking",
    ]
    # A killed worker can leave a validated manifest before results.json is
    # ever created.  There is nothing to resume in that state, so preserve the
    # manifest beside the run and start clean.  Once results.json exists, use
    # the harness' fail-closed continuation path.
    if (output / "manifest.json").is_file() and not (output / "results.json").is_file():
        interrupted = output.with_name(output.name + ".manifest-only-interrupted")
        if not interrupted.exists():
            os.replace(output, interrupted)
        else:
            shutil.rmtree(output)
    elif (output / "manifest.json").is_file():
        command.extend(["--resume", "--seal-interrupted-current"])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(candidate_dir / "artifact/src") + os.pathsep + str(REPO / "src")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    log = output.with_suffix(".log")
    with log.open("w", encoding="utf-8") as handle:
        completed = subprocess.run(command, cwd=REPO, env=env, stdout=handle, stderr=subprocess.STDOUT, check=False)
    result = {"candidate": candidate, "task_id": task_id, "status": "scored" if summary.is_file() else "infrastructure", "returncode": completed.returncode, "output": str(output)}
    if summary.is_file():
        result["summary"] = read(summary)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-root", type=Path, default=DEFAULT_PROFILE_ROOT)
    parser.add_argument("--output-root", type=Path, default=REPO / "benchmarks/results/fin15k-v2-candidate-screen-20260923")
    parser.add_argument("--candidates-per-mechanism", type=int, default=3)
    parser.add_argument("--parallelism", type=int, default=6)
    parser.add_argument("--tasks-per-category", type=int, default=2)
    parser.add_argument("--full-nonvisual", action="store_true")
    parser.add_argument("--retry-unscored", action="store_true")
    parser.add_argument("--task-timeout", type=int, default=DEFAULT_TASK_TIMEOUT)
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--valid-scale-only", action="store_true")
    parser.add_argument("--mechanisms", nargs="*", choices=MECHANISMS)
    args = parser.parse_args()
    profile_root = args.profile_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    if args.candidate_manifest:
        loaded = read(args.candidate_manifest.expanduser().resolve())
        all_candidates = list(loaded.get("candidates", loaded) if isinstance(loaded, dict) else loaded)
    else:
        all_candidates = []
        for mechanism in MECHANISMS:
            all_candidates.extend(materialize_candidates(profile_root, mechanism, args.candidates_per_mechanism))
    if args.valid_scale_only:
        all_candidates = [
            item for item in all_candidates
            if item.get("source") == "strict-scale-specific-glm-proposal"
        ]
    if args.mechanisms:
        all_candidates = [item for item in all_candidates if item.get("mechanism") in args.mechanisms]
    manifest = {
        "schema_version": "fin15k-v2-candidate-screen-v1",
        "policy": "Fin-1.5K trace/profile proposes; no Fin gate prerequisite; candidate-only V2 evaluation",
        "solver_model": MODEL,
        "proposer_model": "dashscope/glm-5.2",
        "tasks": choose_tasks(None if args.full_nonvisual else args.tasks_per_category),
        "candidates": all_candidates,
        "original_arms_rerun": False,
    }
    # When an explicit candidate manifest is supplied, it is the immutable
    # source of truth for resume runs.  Do not overwrite the shared output
    # manifest: concurrent per-mechanism resumes would otherwise erase each
    # other's candidate list before filtering by --mechanisms.
    if args.candidate_manifest is None:
        write(output_root / "candidate-screen-manifest.json", manifest)
    print(json.dumps({"candidates": all_candidates, "tasks": manifest["tasks"]}, ensure_ascii=False, indent=2), flush=True)
    if args.generate_only:
        return 0
    jobs = [
        (candidate, task_id, output_root, args.retry_unscored, args.task_timeout)
        for candidate in all_candidates
        if candidate.get("candidate_dir")
        for task_id in manifest["tasks"]
    ]
    results: list[dict[str, Any]] = []

    # Each candidate gets its own pool.  Thus --parallelism is a per-candidate
    # limit (8 candidates × 6 means up to 48 independent evaluator workers),
    # while a slow candidate cannot starve the others.
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], str, Path]]] = {}
    for job in jobs:
        c = job[0]
        grouped.setdefault((str(c["mechanism"]), str(c["candidate_id"])), []).append(job)

    def run_group(group_jobs: list[tuple[dict[str, Any], str, Path]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with futures.ThreadPoolExecutor(max_workers=args.parallelism) as pool:
            pending = [pool.submit(run_task, job) for job in group_jobs]
            for future in futures.as_completed(pending):
                try:
                    row = future.result()
                except Exception as exc:
                    # Isolate one evaluator/provider failure so the remaining
                    # candidate tasks continue and can be resumed later.
                    candidate, task_id, _, _, _ = group_jobs[pending.index(future)] if future in pending else (None, "", None, False, 0)
                    row = {"candidate": candidate or {}, "task_id": task_id, "status": "worker_error", "error": str(exc)}
                rows.append(row)
                print(json.dumps({"mechanism": row.get("candidate", {}).get("mechanism"), "candidate": row.get("candidate", {}).get("candidate_id"), "task": row.get("task_id"), "status": row.get("status")}, ensure_ascii=False), flush=True)
        return rows

    with futures.ThreadPoolExecutor(max_workers=max(1, len(grouped))) as groups_pool:
        group_futures = [groups_pool.submit(run_group, group_jobs) for group_jobs in grouped.values()]
        for future in futures.as_completed(group_futures):
            results.extend(future.result())
    if args.candidate_manifest is None:
        write(output_root / "results.json", results)
    else:
        write(output_root / f"results-{','.join(sorted({str(r.get('candidate', {}).get('mechanism', 'unknown')) for r in results}))}.json", results)
    return 0 if results and all(row["status"] in {"scored", "existing"} for row in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
