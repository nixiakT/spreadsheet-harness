#!/usr/bin/env python3
"""Select arbitrary common valid Basic/Financial Fin-1.5K cases."""
from __future__ import annotations
import argparse, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def valid_cases(root: Path, arm: str) -> dict[int, dict]:
    out={}
    for d in (root/arm).glob('*'):
        if not d.is_dir(): continue
        try: idx=int(d.name.split('-',1)[0])
        except: continue
        for run in sorted(d.glob('run*')):
            s=run/'summary.json'; ts=list(run.glob(f'runs/*/*/{arm}/trajectory.jsonl'))
            if not s.exists() or len(ts)!=1: continue
            try: sd=json.loads(s.read_text()); rows=[json.loads(x) for x in ts[0].read_text().splitlines()]
            except: continue
            if not sd.get('study_complete') or any(r.get('event')=='model.failed' for r in rows): continue
            ev=[r for r in rows if r.get('event')=='spreadsheetbench_v2.evaluated']
            if not ev: continue
            out[idx]={'dir':d,'run':run,'trajectory':ts[0],'summary':s,'task_id':ev[-1].get('payload',{}).get('task_id')}
            break
    return out

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--source-root',type=Path,default=ROOT/'benchmarks/results/fin15k-latest-dual-20260928'); ap.add_argument('--output-root',type=Path,default=ROOT/'benchmarks/results/fin15k-valid-common-20260928'); ap.add_argument('--size',type=int,choices=(50,200,500),required=True); ap.add_argument('--arm',choices=('spreadsheet-harness-basic','spreadsheet-harness-financial'),required=True)
    a=ap.parse_args(); src=a.source_root.resolve(); out=a.output_root.resolve(); b=valid_cases(src,'spreadsheet-harness-basic'); f=valid_cases(src,'spreadsheet-harness-financial'); common=sorted(set(b)&set(f))
    if len(common)<a.size: raise SystemExit(f'only {len(common)} common valid cases, need {a.size}')
    selected=common[:a.size]
    try:
        manifest=json.loads((src/'manifest.json').read_text())
    except json.JSONDecodeError:
        # The dual runner's manifest may be left with appended progress data;
        # the frozen 500-case split has the same validated case identities.
        frozen = ROOT/'benchmarks/results/fin15k-scaling-coevolution-20260919/split-manifest.json'
        manifest={'cases': json.loads(frozen.read_text())['evidence']}
    cases={int(c['evidence_index']):c for c in manifest['cases']}
    root=out/f'size-{a.size}-{a.arm.removeprefix("spreadsheet-harness-")}'; (root/'baseline-trajectories').mkdir(parents=True,exist_ok=True)
    rows=[]
    for pos,idx in enumerate(selected):
        item=cases[idx]; info=(b if a.arm=='spreadsheet-harness-basic' else f)[idx]
        target=root/'baseline-trajectories'/f'{pos:04d}-{item["id"]}'; target.mkdir(parents=True,exist_ok=True)
        cell={'schema_version':'fin15k-baseline-cell-v1','status':'complete','task':{'id':item['id'],'task_id':item['task_id'],'category':'Financial_Model','complexity':item['complexity'],'source_workbook':item['source_workbook'],'input_path':item['input_path']},'trajectory':str(info['trajectory'].resolve()),'summary':str(info['summary'].resolve())}
        (target/'cell.json').write_text(json.dumps(cell,ensure_ascii=False,indent=2)+'\n')
        rows.append({'selected_index':pos,'original_index':idx,'task_id':item['task_id'],'source_workbook':item['source_workbook'],'arm':a.arm,'trajectory':str(info['trajectory'].resolve()),'summary':str(info['summary'].resolve())})
    (root/'selected-manifest.json').write_text(json.dumps({'schema_version':'fin15k-valid-common-selection-v1','size':a.size,'common_valid_count':len(common),'selected':rows},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'size':a.size,'common_valid_count':len(common),'selected_root':str(root),'arm':a.arm},ensure_ascii=False))

if __name__=='__main__': main()
