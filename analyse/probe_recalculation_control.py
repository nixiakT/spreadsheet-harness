"""Matched control: round-trip/recalculate original failures without fixes."""
import json
import tempfile
import hashlib
from pathlib import Path
from openpyxl import load_workbook
from spreadsheet_harness.render import recalculate_workbook
from spreadsheet_harness.spreadsheetbench_v1 import load_spreadsheetbench_v1, official_compare_v1
from audit_case1_financial import ROOT, cohorts

def main():
    directory = Path(tempfile.mkdtemp(prefix='v1-case1-recalc-control-'))
    print(directory, flush=True)
    group = cohorts()['financial']
    tasks = {t.task_id:t for t in load_spreadsheetbench_v1(ROOT/'benchmarks/data/spreadsheetbench_912_v0.1')}
    results = []
    for tid in ['52050', '37228', '49782', '50472', '56451']:
        source = Path(group[tid]['run_dir'])/'case-1/artifacts/output.xlsx'
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        output = directory/f'{tid}.xlsx'
        wb = load_workbook(source)
        wb.save(output)
        wb.close()
        recalculate_workbook(output, output, timeout_seconds=90)
        task = tasks[tid]
        passed = official_compare_v1(task.cases[0].golden_path, output, task.answer_position)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        row = {'task_id':tid,'source':str(source),'source_sha256':digest,'control':str(output),'official_case1_pass':passed}
        results.append(row)
        (directory/'results.json').write_text(json.dumps(results, indent=2))
        print(json.dumps(row), flush=True)

if __name__ == '__main__':
    main()
