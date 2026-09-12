#!/usr/bin/env python3
"""Run the official paired evaluator for one continuous plugin candidate."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from spreadsheet_harness.config import ProviderConfig
from spreadsheet_harness.continuous_evolution import SpreadsheetBenchV2EvaluationAdapter


def main() -> int:
    request_path, response_path = Path(sys.argv[1]), Path(sys.argv[2])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    provider = ProviderConfig(
        base_url=os.environ.get("SPREADSHEET_EVOLUTION_BASE_URL", "http://10.130.138.46:8010/v1"),
        api_key=Path(os.environ.get("SPREADSHEET_EVOLUTION_API_KEY_FILE", "/tmp/spreadsheet-harness-litellm.key")).read_text(encoding="utf-8").strip(),
        model=os.environ.get("SPREADSHEET_EVOLUTION_MODEL", "DeepSeek-V4-Pro"),
        api_protocol="chat-completions",
        reasoning_effort="medium",
        requested_reasoning_effort="medium",
        timeout_seconds=1800.0,
        max_retries=5,
        request_interval_seconds=0.8,
        temperature=0.0,
        top_p=1.0,
        seed=41,
    )
    binding = request.get("evaluation_binding") or {}
    repo_root = Path(__file__).resolve().parents[1]
    workspace_root = Path(str(request.get("candidate_directory", ""))).resolve().parents[1]
    adapter = SpreadsheetBenchV2EvaluationAdapter(
        provider_config=provider,
        dataset_root=repo_root / "benchmarks/data/normalized-harbor/v06-financial-269",
        evaluator_path=repo_root / "benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py",
        output_root=repo_root / "benchmarks/results/continuous-financial-plugin-evaluations" / workspace_root.name,
        max_model_calls=int(binding.get("max_model_calls", 8)),
        max_turns_per_arm=int(binding.get("max_model_calls", 8)),
        max_total_tokens=None,
        max_output_tokens=None,
        task_timeout_seconds=float(binding.get("task_timeout_seconds", 1800)),
        arm_order_seed=int(binding.get("seed", 20260911)),
    )
    report = adapter.evaluate(request, Path(str(request["candidate_directory"])))
    response_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
