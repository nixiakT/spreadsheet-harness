#!/usr/bin/env python3
from pathlib import Path
import json

root = Path(__file__).resolve().parents[1]
src = root / 'benchmarks/results/paper_reproduction_v2_30_summary_20260905.json'
dst = root / 'benchmarks/results/paper_reproduction_v2_30_report_20260905.md'
data = json.loads(src.read_text())
lines = [
    '# SpreadsheetBench-v2 paper-method reproduction status', '',
    'This report is generated from the live deduplicated summary. Scores are Linux/LibreOffice results and are not interchangeable with official Windows Excel/VLM results.', '',
    '| Method | Unique cases | Completed | Errors/incomplete | Hard pass | Modification accuracy | Regression accuracy | Trace log results/success |',
    '|---|---:|---:|---:|---:|---:|---:|---:|',
]
for name, s in data['methods'].items():
    trace = s.get('trace_log_stats', {})
    trace_cell = (f"{trace.get('task_results_in_logs')}/{trace.get('task_success_in_logs')}" if trace else '-')
    lines.append('| {} | {} | {} | {} | {} | {} | {} | {} |'.format(
        name, s.get('unique_cases'), s.get('completed'), s.get('errors_or_incomplete'),
        s.get('pass_count'), s.get('modification_accuracy_mean'), s.get('regression_accuracy_mean'), trace_cell))
lines += ['', '## Identity and caveats', '',
          '- Spreadsheet-RL thinking uses the released `Spreadsheet-RL-4B` checkpoint; the native tool loop is evaluated on Linux/LibreOffice, so it is an official-checkpoint proxy for the paper Excel reward environment.',
          '- SpreadsheetAgent uses a clean-room paper-vision proxy. The official pipeline requires Windows Excel/COM and the paper VLM/checkpoints.',
          '- SheetCompass uses a clean-room graph/memory proxy because no public implementation/checkpoint was located.',
          '- Trace2Skill uses the public runner and released skills, but lab models are substitutions. The qwen36 original run also exposed a shell `python` environment defect; environment-fixed runs are tracked separately.',
          '- `completed` means a valid evaluator row; Trace2Skill workbooks are rescored with `tools/score_trace2skill_outputs.py`, while its runner logs retain the model-reported success count.', '',
          f'Source JSON: `{src}`', '']
dst.write_text('\n'.join(lines))
print(dst)
