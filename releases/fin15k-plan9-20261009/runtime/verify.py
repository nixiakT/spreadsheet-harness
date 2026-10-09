#!/usr/bin/env python3
"""Offline verification of all nine candidate artifacts, never a model eval.

Each subprocess imports only its own artifact and executes the real strict
before_task hook chain on an artificial workbook. --write saves a new audit;
it never overwrites one, edits candidate sources or records a benchmark gain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from spreadsheet_harness.continuous_evolution import (
    CandidateProposal,
    RevisionStore,
    _sha256_json,
    _tree_manifest,
    validate_candidate_plan,
)
from spreadsheet_harness.errors import HarnessError

_CHILD = r'''
import hashlib, json, sys
from pathlib import Path
from openpyxl import Workbook
from spreadsheet_harness import arms, financial_model_repairs, plugin_runtime
from spreadsheet_harness.plugins import CompositionSpec, default_plugin_registry, execution_plan
from spreadsheet_harness.session import WorkbookSession

candidate=Path(sys.argv[1]).resolve()
scratch=Path(sys.argv[2])
scale=int(sys.argv[3])
scope=sys.argv[4]
artifact=candidate/'artifact'
for module in (arms,financial_model_repairs,plugin_runtime):
    assert Path(module.__file__).resolve().is_relative_to(artifact/'src'), module.__file__
if scope in {'harness','joint'}:
    assert arms._CANDIDATE_TRACE_SCALE==scale
    assert 'without changing any bytes' in arms._candidate_success_review_prompt('Complete Revenue','preview','plan')
    assert arms._candidate_review_gap_issues('verification: true\nissues: []\n')==()
else:
    assert not hasattr(arms,'_CANDIDATE_TRACE_SCALE')
if scope in {'domain','joint'}:
    assert financial_model_repairs._FIN15K_CANDIDATE_SIZE==scale
    assert financial_model_repairs._candidate_label_signature('A')!=financial_model_repairs._candidate_label_signature('A%')
else:
    assert not hasattr(financial_model_repairs,'_FIN15K_CANDIDATE_SIZE')

input_path=scratch/'artificial.xlsx'
book=Workbook()
sheet=book.active
sheet.title='Forecast'
sheet.append(['Metric',None,2024,2025,2026,'Terminal Year'])
sheet.append(['Revenue',None,100,110,120])
sheet.append(['Expenses',None,-20,-25,-30])
sheet.append(['Net Profit',None,'=C2+C3',None,'=E2+E3'])
sheet['A7']='A'
sheet['A8']='A%'
sheet['C8']=0.25
sheet['C8'].number_format='0%'
other=book.create_sheet('Cashflow')
other.append(['Metric',None,None,2024,2025,2026,'Terminal Year'])
other.append(['Net Profit'])
book.save(input_path)
book.close()

if scope in {'domain','joint'}:
    changes=financial_model_repairs.restore_incomplete_financial_formula_bands(input_path,source_path=input_path)
    assert changes==[{'sheet':'Forecast','target':'D4','formula':'=D2+D3'}], changes
    if scale>=200:
        assert financial_model_repairs._instruction_sheet_clauses('On Forecast, calculate Revenue for 2024-2026.')

doc=json.loads((candidate/'composition.json').read_text())
plan=execution_plan(default_plugin_registry().resolve(CompositionSpec.create(doc['name'],doc['plugins'],doc.get('overrides'))))
session=WorkbookSession.create(input_path,scratch/'session')
before=hashlib.sha256(Path(session.workbook_path).read_bytes()).hexdigest()
instruction='On Forecast, inspect A and A% and complete Net Profit for 2024-2026. On Cashflow, link Net Profit from Forecast for 2024-2026.'
records=plugin_runtime.execute_candidate_hooks(plan,'before_task',session,instruction,'Financial_Model',require_isolation=True)
expected=['observe-task-boundaries','observe-financial-targets'] if scope=='joint' else ['observe-task-boundaries'] if scope=='harness' else ['observe-financial-targets']
assert [record['plugin'] for record in records]==expected
observations=[]
context_bytes=0
for record in records:
    source=artifact/'src/spreadsheet_harness/generated_plugins'/(record['plugin'].replace('-','_')+'.py')
    assert record['implementation_sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
    result=record['result']
    assert result['ok'] is True and result['context']
    context_bytes+=len(json.dumps(result,ensure_ascii=False).encode('utf-8'))
    observations.extend(json.loads(item) for item in result['context'])
assert any(item.get('sheet')=='Forecast' for item in observations)
assert context_bytes<=(8000 if scale==50 else 12000)
assert hashlib.sha256(Path(session.workbook_path).read_bytes()).hexdigest()==before
print(json.dumps({'scope':scope,'evidence_size':scale,'module_paths':{module.__name__:module.__file__ for module in (arms,financial_model_repairs,plugin_runtime)},'hook_plugins':expected,'hook_implementation_sha256':[record['implementation_sha256'] for record in records],'context_bytes':context_bytes,'workbook_unchanged':True,'strict_isolation':True},ensure_ascii=False))
'''


def verify(bundle: Path, *, write: bool = False) -> dict[str, Any]:
    manifest = json.loads((bundle / "candidate-manifest.json").read_text(encoding="utf-8"))
    rows = manifest.get("candidates", [])
    if len(rows) != 9:
        raise HarnessError("The frozen candidate matrix must have exactly nine artifacts")
    if write and (bundle / "offline-validation.json").exists():
        raise HarnessError("Offline audit already exists; cannot overwrite a prior validation")
    store = RevisionStore(bundle / "materialization")
    records: list[dict[str, Any]] = []
    for row in rows:
        path = Path(row["candidate_dir"])
        directory = (path if path.is_absolute() else bundle / path).resolve()
        if not directory.is_relative_to(bundle):
            raise HarnessError("Candidate path is outside the declared bundle")
        revision = json.loads((directory / "revision.json").read_text(encoding="utf-8"))
        store.verify_materialized_candidate(directory, revision)
        mechanism, size = row["mechanism"], row["evidence_size"]
        request_path = bundle / f"size-{size}/{mechanism}/request.json"
        response_path = Path(row["response"])
        response = json.loads((response_path if response_path.is_absolute() else bundle / response_path).read_text(encoding="utf-8"))
        proposal = CandidateProposal.from_document(response["candidates"][0])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        validate_candidate_plan(proposal, request)
        if _sha256_json(response["candidates"][0]) != row["proposal_sha256"]:
            raise HarnessError("Frozen response differs from the proposal manifest")
        stored = json.loads((directory / "proposal-digest.json").read_text(encoding="utf-8"))
        if stored["sha256"] != _sha256_json(proposal.to_dict()):
            raise HarnessError("Frozen response differs from the materialized full plan")
        if _sha256_json(request) != row["request_sha256"]:
            raise HarnessError("Frozen request differs from the manifest")
        if revision["revision_sha256"] != row["revision_sha256"]:
            raise HarnessError("Candidate revision differs from the manifest")
        before = _tree_manifest(directory / "artifact")
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(directory / "artifact/src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory(prefix="fin15k-candidate-offline-") as scratch:
            completed = subprocess.run(
                [sys.executable, "-c", _CHILD, str(directory), scratch, str(size), row["scope"]],
                cwd=directory / "artifact", env=environment, text=True, capture_output=True,
                check=False, timeout=120,
            )
        if completed.returncode != 0:
            raise HarnessError(
                f"Offline candidate execution failed: {row['candidate_id']}\n{completed.stderr[-6000:]}"
            )
        proof = json.loads(completed.stdout)
        if before != _tree_manifest(directory / "artifact"):
            raise HarnessError("Offline check modified a frozen candidate source")
        records.append({
            "candidate_id": row["candidate_id"], "revision_sha256": row["revision_sha256"],
            "status": "offline-checks-passed-not-scored", **proof,
        })
    report = {
        "schema_version": "fin15k-plugin-plan9-offline-validation-v1",
        "candidate_manifest_sha256": hashlib.sha256((bundle / "candidate-manifest.json").read_bytes()).hexdigest(),
        "verifier_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "status": "offline-checks-passed-not-scored", "candidate_count": len(records),
        "model_calls": 0, "official_evaluation_calls": 0,
        "measured_improvement": None, "records": records,
    }
    if write:
        with (bundle / "offline-validation.json").open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    report = verify(args.bundle.expanduser().resolve(), write=args.write)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
