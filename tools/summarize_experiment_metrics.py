#!/usr/bin/env python3
"""Summarize per-model benchmark execution and tool metrics."""
import json, sys
from pathlib import Path
from collections import Counter, defaultdict

root = Path(sys.argv[1])
for model_dir in sorted(p for p in root.iterdir() if p.is_dir()):
    rows = []
    for p in model_dir.glob("*/results.json"):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        rows.extend(data if isinstance(data, list) else [data])
    status = Counter(r.get("status") for r in rows)
    errors = Counter(r.get("error_type") for r in rows if r.get("status") == "error")
    totals = Counter()
    tool_names = Counter()
    tool_errors = 0
    parallel_batches = 0
    for r in rows:
        agent = r.get("agent") or {}
        usage = agent.get("usage") or {}
        totals["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        totals["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        totals["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
        totals["turns"] += int(agent.get("turns", 0) or 0)
        totals["tool_calls"] += int(agent.get("tool_calls", 0) or 0)
        totals["terminal_submissions"] += int(agent.get("terminal_submissions", 0) or 0)
        for t in agent.get("tool_trace") or []:
            name = t.get("name") or t.get("tool") or t.get("tool_name") or "unknown"
            tool_names[name] += 1
            if t.get("ok") is False or t.get("error") or t.get("status") == "error":
                tool_errors += 1
        events = (agent.get("events") or [])
        parallel_batches += sum(1 for e in events if e.get("type") == "agent.parallel_tool_batch.accepted")
    print(json.dumps({"model": model_dir.name, "rows": len(rows), "status": status,
                      "errors_by_type": errors, "metrics": totals,
                      "tool_calls_by_name": tool_names, "tool_errors": tool_errors,
                      "parallel_tool_batches": parallel_batches}, ensure_ascii=False, default=dict))
