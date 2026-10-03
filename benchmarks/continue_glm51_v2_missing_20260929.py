from __future__ import annotations
import concurrent.futures, json, os, subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'benchmarks/results/v2-glm51-public-20260928'
PY=ROOT/'.venv/bin/python'; RUNNER=ROOT/'benchmarks/run_v2_target_category_parallel_20260925.py'

def missing(arm,cat,total):
    root=OUT/'glm-5.1'/arm/cat; ids=[]
    for t in sorted(root.iterdir()):
        if not t.is_dir(): continue
        rs=[]
        for f in t.glob('run*/results.json'):
            try: rs.append((f.stat().st_mtime,json.loads(f.read_text())[0]))
            except: pass
        good=[r for _,r in sorted(rs) if r.get('outcome_kind') in ('scored','scored_after_provider_failure','scored_after_agent_failure') and isinstance((r.get('official_score') or {}).get('accuracy'),(int,float))]
        if not good: ids.append(t.name)
    return ids

def run(spec):
    arm,cat,ids=spec
    if not ids:return {'arm':arm,'category':cat,'count':0,'returncode':0}
    log=OUT/'missing-rerun-logs'/f'{arm}__{cat}.log';log.parent.mkdir(parents=True,exist_ok=True)
    cmd=[str(PY),str(RUNNER),'--category',cat,'--arm',arm,'--model','dashscope/glm-5.1','--model-slug','glm-5.1','--output',str(OUT),'--parallelism','2','--task-ids',*ids,'--base-url','http://47.96.153.159:8010/v1','--api-key-file','/tmp/spreadsheet-harness-litellm.key','--task-timeout','10800','--request-timeout','1800','--litellm-timeout','1800']
    env=os.environ.copy();env['PYTHONPATH']=str(ROOT/'src')
    with log.open('w') as f:rc=subprocess.run(cmd,cwd=ROOT,env=env,stdout=f,stderr=subprocess.STDOUT).returncode
    return {'arm':arm,'category':cat,'count':len(ids),'returncode':rc}

def main():
    specs=[]
    for arm in ('bare','spreadsheet-harness-basic','spreadsheet-harness-financial'):
        for cat in ('Template','Financial_Model','Debugging'):
            ids=missing(arm,cat,0); specs.append((arm,cat,ids)); print(arm,cat,len(ids),ids,flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as p:rows=list(p.map(run,specs))
    (OUT/'missing-rerun-results-20260929.json').write_text(json.dumps(rows,indent=2)+'\n')
    print(json.dumps(rows,indent=2))

if __name__=='__main__':main()
