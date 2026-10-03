#!/usr/bin/env python3
"""Rescore existing SpreadsheetBench V2 outputs without model calls."""
from __future__ import annotations
import argparse, importlib.util, json, shutil, tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / 'benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py'
DATA = ROOT / 'benchmarks/data/spreadsheetbench-v2'

def load_eval():
    spec = importlib.util.spec_from_file_location('sb2_eval', EVAL)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def score_one(mod, row, task_map):
    tid = row.get('task_id','')
    if '/' not in tid: return tid, None, 'bad_task_id'
    cat, iid = tid.split('/', 1)
    out = Path(row.get('output',''))
    if not out.is_file():
        # Common layout fallback.
        root = Path(row.get('_run_root',''))
        out = root / 'tasks' / f'{cat}__{iid}' / 'output.xlsx'
    if not out.is_file(): return tid, None, 'missing_output'
    task = task_map.get(tid)
    if task is None: return tid, None, 'missing_dataset_task'
    with tempfile.TemporaryDirectory(prefix='sb2-rescore-') as td:
        td = Path(td); proc = td / f'{iid}_output.xlsx'; shutil.copy2(out, proc)
        score, missing = mod.process_single_item(task, str(DATA / cat), str(td), {}, cat)
        return tid, score, ('scored' if not missing else 'missing')

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--root', type=Path, required=True); ap.add_argument('--workers',type=int,default=6); args=ap.parse_args()
    mod=load_eval(); task_map={}
    for cat in ('Debugging','Financial_Model','Template'):
        for t in json.loads((DATA/cat/'dataset.json').read_text()): task_map[f'{cat}/{t["id"]}']=t
    for result in sorted(args.root.glob('spreadsheetbench-v2-*/results.json')):
        payload=json.loads(result.read_text()); rows=payload.get('results',[]) if isinstance(payload,dict) else payload
        for r in rows:
            r['_run_root']=str(result.parent)
        jobs=[]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for r in rows:
                s=r.get('official_score') or {}
                if isinstance(s,dict) and 'accuracy' in s: continue
                jobs.append(ex.submit(score_one,mod,r,task_map))
            for fut in as_completed(jobs):
                tid, score, status=fut.result()
                for r in rows:
                    if r.get('task_id')==tid:
                        if score is not None: r['official_score']=score; r['rescore_20260927']=True
                        r['_rescore_status']=status; break
        out=result.with_name('results_rescored_20260927.json'); out.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
        scored=sum(isinstance(r.get('official_score'),dict) and 'accuracy' in r['official_score'] for r in rows)
        print(result.parent.name, 'rows',len(rows),'scored',scored,'rescored',sum(r.get('rescore_20260927') is True for r in rows),'->',out)

if __name__=='__main__': main()
