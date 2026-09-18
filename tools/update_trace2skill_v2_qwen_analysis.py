#!/usr/bin/env python3
"""Update analyse/analyse.md after the formal Qwen V2 resume and scoring."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path


START = "<!-- TRACE2SKILL_V2_QWEN_FINAL_START -->"
END = "<!-- TRACE2SKILL_V2_QWEN_FINAL_END -->"


def pct(value: float | None) -> str:
    return "待评" if value is None else f"{100 * value:.2f}%"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--preservation", type=Path, required=True)
    args = parser.parse_args()

    status = json.loads(args.status.read_text(encoding="utf-8"))
    evaluation = json.loads(args.evaluation.read_text(encoding="utf-8"))
    preservation = json.loads(args.preservation.read_text(encoding="utf-8"))
    summary = evaluation["summary"]
    categories = summary["by_category"]
    successful = int(status["successful_instances"])
    failed = int(status["failed_instances"])

    names = (
        ("DebuggingV2", "Debugging"),
        ("TemplateV2", "Template"),
        ("FinancialV2", "Financial_Model"),
    )
    table_rows = []
    for display, key in names:
        row = categories[key]
        exact = int(row["exact_tasks"])
        tasks = int(row["tasks"])
        table_rows.append(
            f"| {display} | {tasks} | {row['outputs']} | "
            f"{exact}/{tasks} = {pct(row['accuracy'])} | "
            f"{pct(row['modification_accuracy'])} | "
            f"{pct(row['regression_accuracy'])} | "
            f"{pct(row['cell_accuracy'])} | V2 cell-based official comparator |"
        )
    visual = categories["Visualization"]
    table_rows.append(
        f"| VisualizationV2 | {visual['tasks']} | {visual['valid_xlsx_outputs']} | "
        "待 Windows/VLM 官方评分 | 不适用 | 不适用 | 不适用 | "
        "Windows Excel/WPS COM + `glm-4.6v` |"
    )

    block = "\n".join(
        [
            START,
            f"### V2 Qwen 补跑后结果（{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}）",
            "",
            f"- 最终执行状态：有效 XLSX `{successful}/321`，无有效产物 `{failed}/321`。",
            f"- 已有 241 个产物保护检查：`{preservation['unchanged']}/"
            f"{preservation['baseline_files']}` SHA-256 未变化；"
            f"变化 `{preservation['changed']}`、丢失 `{preservation['missing']}`。",
            "- 以下前三类使用独立 LibreOffice 重算副本和 pinned SpreadsheetBench V2 "
            "cell-based comparator；Visualization 原始 OOXML 未经 LibreOffice 改写。",
            "",
            "| V2 子集 | 任务数 | 有效输出 | Exact / task accuracy | Modification | Regression | Macro cell | 评分协议 |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
            *table_rows,
            "",
            f"三个 cell-based 子集合计：Exact `{summary['exact_tasks']}/297 = "
            f"{pct(summary['accuracy'])}`、Modification "
            f"`{pct(summary['modification_accuracy'])}`、Regression "
            f"`{pct(summary['regression_accuracy'])}`、Macro cell "
            f"`{pct(summary['cell_accuracy'])}`、Micro cell "
            f"`{pct(summary['micro_cell_accuracy'])}`。",
            "",
            "结果文件：`benchmarks/results/trace2skill-v2-formal-qwen3-coder-480b-20260914/"
            "eval_official_results_v2_final.json`。VisualizationV2 只有在独立官方 Windows/VLM "
            "evaluator 完成后才能填写最终分数，不能把待评分任务记为 0。",
            END,
        ]
    )

    text = args.analysis.read_text(encoding="utf-8")
    if START in text and END in text:
        text = re.sub(
            re.escape(START) + r".*?" + re.escape(END),
            block,
            text,
            flags=re.DOTALL,
        )
    else:
        anchor = "\n## V2 DeepSeek 阶段性结果与收尾状态"
        if anchor not in text:
            raise RuntimeError("cannot find V2 DeepSeek section anchor")
        text = text.replace(anchor, "\n\n" + block + anchor, 1)

    table_line = (
        "| V2 Qwen | `tmp/trace2skill_spreadsheetbench_v2_full` | "
        "`dashscope/qwen3-coder-480b-a35b-instruct` | "
        "`benchmarks/results/trace2skill-v2-formal-qwen3-coder-480b-20260914` | "
        f"{successful}/321 个有效 XLSX | {successful}/321 个有效 XLSX | "
        f"targeted resume 已结束；无有效产物 {failed} 个；正确 V2 evaluator 已重评 | "
        f"DebuggingV2 {pct(categories['Debugging']['accuracy'])}，"
        f"TemplateV2 {pct(categories['Template']['accuracy'])}，"
        f"FinancialV2 {pct(categories['Financial_Model']['accuracy'])}；"
        f"VisualizationV2 {visual['valid_xlsx_outputs']}/{visual['tasks']} 有效产物、"
        "待官方视觉评分 |"
    )
    text, count = re.subn(r"^\| V2 Qwen \|.*$", table_line, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError("cannot find unique V2 Qwen summary row")
    args.analysis.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
