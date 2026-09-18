"""Read-only untouched-input baseline, paired with historical Case 1 outcomes."""
import collections
import json
from audit_case1_financial import ROOT, cohorts, case_pass
from spreadsheet_harness.spreadsheetbench_v1 import load_spreadsheetbench_v1, official_compare_v1


def main():
    tasks = {t.task_id: t for t in load_spreadsheetbench_v1(ROOT / 'benchmarks/data/spreadsheetbench_912_v0.1')}
    groups = cohorts()
    counts = collections.Counter()
    for tid in sorted(groups['financial']):
        task = tasks[tid]
        try:
            passed = official_compare_v1(task.cases[0].golden_path, task.cases[0].input_path, task.answer_position)
        except Exception as exc:
            print(json.dumps({'task_id': tid, 'error': str(exc)}), flush=True)
            continue
        counts['evaluated'] += 1
        counts['noop_pass'] += passed
        if passed:
            result = {'task_id': tid, 'untouched_pass': True}
            for arm in groups:
                outcome = case_pass(groups[arm][tid])
                result[arm] = outcome
                counts[f'{arm}_noop_pass_actual_{outcome}'] += 1
            print(json.dumps(result), flush=True)
    print('SUMMARY', json.dumps(counts), flush=True)


if __name__ == '__main__':
    main()
