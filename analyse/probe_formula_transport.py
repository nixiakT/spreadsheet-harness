"""Isolated post-hoc ablation: repair only proven transport escapes on copies.

Never changes dataset, production source, original outputs or model service.
This is a diagnostic upper-bound experiment, not a fresh benchmark run.
"""
import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from openpyxl import load_workbook
from spreadsheet_harness.render import recalculate_workbook
from spreadsheet_harness.spreadsheetbench_v1 import load_spreadsheetbench_v1, official_compare_v1
from audit_case1_financial import ROOT, cohorts, case_pass, trajectory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=3)
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix='v1-case1-transport-audit-'))
    print('EXPERIMENT_DIRECTORY', directory, flush=True)
    tasks = {t.task_id: t for t in load_spreadsheetbench_v1(ROOT / 'benchmarks/data/spreadsheetbench_912_v0.1')}
    results = []
    for tid, record in cohorts()['financial'].items():
        if case_pass(record) is not False:
            continue
        events = trajectory(record)
        if not any(e['event'] == 'harness.planner_actions.verified' and
                   any('\\u003' in str(a.get('expected_value', '')) for a in e['payload'].get('actions', [])) for e in events):
            continue
        source = Path(record['run_dir']) / 'case-1/artifacts/output.xlsx'
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        wb = load_workbook(source)
        changes = []
        for ws in wb:
            for row in ws:
                for cell in row:
                    value = cell.value
                    if isinstance(value, str) and value.startswith('=') and ('\\u003c' in value or '\\u003e' in value):
                        fixed = value.replace('\\u003c', '<').replace('\\u003e', '>')
                        changes.append({'sheet': ws.title, 'cell': cell.coordinate, 'before': value, 'after': fixed})
                        cell.value = fixed
        if not changes:
            wb.close()
            continue
        output = directory / f'{tid}-transport-fixed.xlsx'
        wb.save(output)
        wb.close()
        item = {'task_id': tid, 'original': str(source), 'candidate': str(output), 'changes': changes}
        try:
            recalculate_workbook(output, output, timeout_seconds=90)
            item['official_case1_pass'] = official_compare_v1(tasks[tid].cases[0].golden_path, output, tasks[tid].answer_position)
        except Exception as exc:
            item['error'] = str(exc)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
        results.append(item)
        (directory / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps({k:v for k,v in item.items() if k != 'changes'}), 'changes', len(changes), flush=True)
        if len(results) >= args.limit:
            break


if __name__ == '__main__':
    main()
