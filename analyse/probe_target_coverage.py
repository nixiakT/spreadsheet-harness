"""Case 1 diagnostic: extend an unchanged Financial formula using input extent.

Hand-selected failure cases; this is not a general fix or held-out evaluation.
Only isolated workbook copies are edited, never benchmark artifacts.
"""
import hashlib
import json
import tempfile
import argparse
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.formula.translate import Translator
from spreadsheet_harness.render import recalculate_workbook
from spreadsheet_harness.spreadsheetbench_v1 import load_spreadsheetbench_v1, official_compare_v1
from audit_case1_financial import ROOT, cohorts

SPECS = {
    '37228': ('Sheet1', 'G2', 'F', 'G'),
    '49782': ("Sub CO's", 'B4', 'A', 'B'),
    '50472': ('Sheet1', 'B1', 'A', 'B'),
    '56451': ('Sheet1', 'B2', 'A', 'B:F'),
}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--semantic-guards', action='store_true')
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix='v1-case1-coverage-audit-'))
    print('EXPERIMENT_DIRECTORY', directory, flush=True)
    group = cohorts()['financial']
    tasks = {t.task_id: t for t in load_spreadsheetbench_v1(ROOT / 'benchmarks/data/spreadsheetbench_912_v0.1')}
    results = []
    for tid, (sheet, origin, source_column, target_columns) in SPECS.items():
        source = Path(group[tid]['run_dir']) / 'case-1/artifacts/output.xlsx'
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        book = load_workbook(source)
        ws = book[sheet]
        formula = ws[origin].value
        if args.semantic_guards and tid == '37228':
            formula = '=IF(F2="","",IF(ISNUMBER(FIND(" / ",F2)),IF(E2=1,LEFT(F2,FIND(" / ",F2)-1),MID(F2,FIND(" / ",F2)+3,LEN(F2))),F2))'
        if args.semantic_guards and tid == '50472':
            formula = '=IF(A1="","",VALUE(LEFT(A1,FIND(" ",A1&" ")-1)))'
        row_start = ws[origin].row
        populated = [c.row for c in ws[source_column] if c.row >= row_start and c.value is not None]
        last = max(populated)
        columns = target_columns.split(':')
        first, end = columns[0], columns[-1]
        for row in ws[f'{first}{row_start}:{end}{last}']:
            for cell in row:
                cell.value = Translator(formula, origin=origin).translate_formula(cell.coordinate)
        output = directory / f'{tid}-coverage-fixed.xlsx'
        book.save(output)
        book.close()
        item = {'task_id': tid, 'source': str(source), 'candidate': str(output), 'formula': formula,
                'semantic_guards': args.semantic_guards,
                'range': f'{sheet}!{first}{row_start}:{end}{last}', 'range_source': 'last nonempty input key cell'}
        try:
            recalculate_workbook(output, output, timeout_seconds=90)
            task = tasks[tid]
            item['official_case1_pass'] = official_compare_v1(task.cases[0].golden_path, output, task.answer_position)
        except Exception as exc:
            item['error'] = str(exc)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        results.append(item)
        (directory / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps(item), flush=True)

if __name__ == '__main__':
    main()
