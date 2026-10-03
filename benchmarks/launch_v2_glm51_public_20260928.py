from pathlib import Path
import concurrent.futures, os, subprocess

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'benchmarks/results/v2-glm51-public-20260928'
PYTHON=ROOT/'.venv/bin/python'; RUNNER=ROOT/'benchmarks/run_v2_target_category_parallel_20260925.py'
SPECS=[('bare','bare'),('basic','spreadsheet-harness-basic'),('financial','spreadsheet-harness-financial')]
CATS=('Template','Financial_Model','Debugging')

def run(spec):
 label,arm,cat=spec; log=OUT/'logs'/f'{label}__{cat}.log';log.parent.mkdir(parents=True,exist_ok=True)
 cmd=[str(PYTHON),str(RUNNER),'--category',cat,'--arm',arm,'--model','dashscope/glm-5.1','--model-slug','glm-5.1', '--output',str(OUT),'--parallelism','16','--base-url','http://47.96.153.159:8010/v1','--api-key-file','/tmp/spreadsheet-harness-litellm.key']
 env=os.environ.copy();env['PYTHONPATH']=str(ROOT/'src')
 with log.open('w') as f: return subprocess.run(cmd,cwd=ROOT,env=env,stdout=f,stderr=subprocess.STDOUT).returncode

OUT.mkdir(parents=True,exist_ok=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:list(pool.map(run,[(label,arm,cat) for label,arm in SPECS for cat in CATS]))
