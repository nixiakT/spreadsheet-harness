#!/usr/bin/env python3
"""Resumable runner for the adaptive-scope v4 controller.

It is deliberately a thin process wrapper around ``ContinuousEvolutionEngine``:
all routing, proposal validation, paired evaluation, and promotion gates stay
in the controller.  Transient adapter/provider errors are recorded and retried
for the same round; no new candidate or split is fabricated during recovery.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import traceback
from pathlib import Path

from spreadsheet_harness.continuous_evolution import (
    ContinuousEvolutionConfig,
    ContinuousEvolutionEngine,
)


def _status(state: dict) -> dict:
    return {
        key: state.get(key)
        for key in (
            "status",
            "attempted_rounds",
            "accepted_rounds",
            "current_revision_sha256",
            "next_group",
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--retry-delay", type=float, default=30.0)
    parser.add_argument("--max-retries-per-round", type=int, default=4)
    args = parser.parse_args()
    if args.max_retries_per_round < 1 or not math.isfinite(args.retry_delay) or args.retry_delay < 0:
        parser.error("retry settings must be finite and positive")
    config = ContinuousEvolutionConfig.load(args.config)
    protocol_path = (args.protocol or args.config.parent.parent / "protocol.json").resolve()
    if protocol_path.is_file():
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        expected = protocol.get("config_sha256")
        if expected and expected != config.sha256:
            raise SystemExit("v4 protocol/config canonical hash mismatch")
        if protocol.get("kernel_manifest_sha256") != config.kernel_manifest_sha256:
            raise SystemExit("v4 protocol/config kernel hash mismatch")
    engine = ContinuousEvolutionEngine(config, args.workspace)
    state = engine.initialize()
    print(json.dumps({"event": "initialized", **_status(state)}), flush=True)
    retries = 0
    while state.get("status") == "active":
        attempted_before = int(state.get("attempted_rounds", 0))
        try:
            state = engine.step()
            retries = 0
            print(json.dumps({"event": "round", **_status(state)}), flush=True)
        except Exception as exc:  # keep the same round resumable
            retries += 1
            record = {
                "event": "retry",
                "round": attempted_before + 1,
                "retry": retries,
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
            }
            print(json.dumps(record, ensure_ascii=False), flush=True)
            traceback.print_exc()
            if retries >= args.max_retries_per_round:
                print(
                    json.dumps(
                        {
                            "event": "paused",
                            "reason": "retries-exhausted",
                            "round": attempted_before + 1,
                        }
                    ),
                    flush=True,
                )
                return 2
            time.sleep(max(1.0, args.retry_delay))
    print(json.dumps({"event": "finished", **_status(state)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
