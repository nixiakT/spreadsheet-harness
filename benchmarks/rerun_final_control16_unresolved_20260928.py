from pathlib import Path
import concurrent.futures, json, os, subprocess

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'benchmarks/results/v2-sota-final-control16-20260927'
PYTHON=ROOT/'.venv/bin/python'; RUNNER=ROOT/'benchmarks/run_v2_target_category_parallel_20260925.py'
MODELS={'deepseek-v4-flash-0731':'dashscope/deepseek-v4-flash-0731'}

def collect():
 g={}
 for f in OUT.glob('*/*/*/*/run/results.json'):
  try:r=json.loads(f.read_text())[0]
  except:continue
  if r.get('outcome_kind') not in {'model_execution_failure','not_scored'}:continue
  p=f.relative_to(OUT).parts;g.setdefault(tuple(p[:3]),[]).append(p[3])
 return g

def run(s):
 slug,arm,cat,ids=s;log=OUT/'unresolved-rerun-logs'/f'{slug}__{arm}__{cat}.log';log.parent.mkdir(parents=True,exist_ok=True)
 cmd=[str(PYTHON),str(RUNNER),'--category',cat,'--arm',arm,'--model',MODELS[slug],'--model-slug',slug,'--output',str(OUT),'--parallelism','8','--task-ids',*sorted(set(ids)),'--base-url','http://47.96.153.159:8010/v1','--api-key-file','/tmp/spreadsheet-harness-litellm.key']
 env=os.environ.copy();env['PYTHONPATH']=str(ROOT/'src')
 with log.open('w') as f:res=subprocess.run(cmd,cwd=ROOT,env=env,stdout=f,stderr=subprocess.STDOUT)
 return {'slug':slug,'arm':arm,'category':cat,'count':len(ids),'returncode':res.returncode}

g=collect(); specs=[(a,b,c,d) for (a,b,c),d in sorted(g.items()) if a == 'deepseek-v4-flash-0731']
(OUT/'unresolved-rerun-specs.json').write_text(json.dumps(specs,ensure_ascii=False,indent=2))
with concurrent.futures.ThreadPoolExecutor(max_workers=min(12,max(1,len(specs)))) as p: rows=list(p.map(run,specs))
(OUT/'unresolved-rerun-results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
