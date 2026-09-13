#!/usr/bin/env python3
"""Finalize a resumed Claude Code SpreadsheetBench run from audit logs.

This utility replaces one superseded task with a strict rerun, updates task
turn counts from the proxy audit, and rebuilds the top-level results file.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def accepted_requests(audit: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for line in audit.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        task_key = event.get("task_key")
        if not isinstance(task_key, str):
            continue
        if (
            event.get("request_index")
            and "upstream_attempt" not in event
            and "response_bytes" not in event
            and "error" not in event
            and not event.get("limit_exceeded")
        ):
            counts[task_key] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-run", type=Path, required=True)
    parser.add_argument("--strict-run", type=Path, required=True)
    parser.add_argument("--replacement-task", required=True)
    args = parser.parse_args()

    main_run = args.main_run.resolve()
    strict_run = args.strict_run.resolve()
    task_slug = args.replacement_task.replace("/", "__", 1)
    main_task = main_run / "tasks" / task_slug
    strict_task = strict_run / "tasks" / task_slug
    archive_task = main_run / "superseded" / f"{task_slug}__over50"

    if not archive_task.exists():
        archive_task.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(main_task, archive_task)

    for source in strict_task.iterdir():
        if source.is_file():
            shutil.copy2(source, main_task / source.name)

    replacement_status = read_json(main_task / "status.json")
    for key in ("output", "trajectory", "final_message"):
        value = replacement_status.get(key)
        if isinstance(value, str):
            replacement_status[key] = value.replace(str(strict_run), str(main_run))
    replacement_status["superseded_artifact"] = str(archive_task)
    replacement_status["strict_repair_run"] = str(strict_run)

    main_counts = accepted_requests(main_run / "proxy-requests.jsonl")
    strict_counts = accepted_requests(strict_run / "proxy-requests.jsonl")
    strict_turns = strict_counts[task_slug]
    replacement_status["turns"] = strict_turns
    replacement_status["model_requests"] = strict_turns
    replacement_status["audited_model_requests"] = strict_turns
    replacement_status["turn_audit_source"] = str(strict_run / "proxy-requests.jsonl")
    if isinstance(replacement_status.get("official_score"), dict):
        replacement_status["official_score"]["interaction_turns"] = strict_turns
    write_json(main_task / "status.json", replacement_status)

    results: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for status_path in sorted((main_run / "tasks").glob("*/status.json")):
        status = read_json(status_path)
        slug = status["task_id"].replace("/", "__", 1)
        audited = strict_turns if slug == task_slug else main_counts.get(slug, 0)
        previous = status.get("turns")
        if audited:
            status["turns"] = audited
            status["model_requests"] = audited
            status["audited_model_requests"] = audited
            if isinstance(status.get("official_score"), dict):
                status["official_score"]["interaction_turns"] = audited
            if previous != audited:
                corrections.append(
                    {"task_id": status["task_id"], "recorded": previous, "audited": audited}
                )
            write_json(status_path, status)
        results.append(status)

    results.sort(key=lambda item: item["task_id"])
    write_json(main_run / "results.json", results)
    write_json(
        main_run / "audit-summary.json",
        {
            "schema_version": 1,
            "result_count": len(results),
            "status_counts": dict(Counter(item.get("status") for item in results)),
            "replacement_task": args.replacement_task,
            "replacement_accepted_requests": strict_turns,
            "superseded_main_audit_requests": main_counts[task_slug],
            "strict_run": str(strict_run),
            "superseded_artifact": str(archive_task),
            "turn_corrections": corrections,
        },
    )


if __name__ == "__main__":
    main()
