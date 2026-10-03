from pathlib import Path
import concurrent.futures, os, subprocess

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'benchmarks/results/v2-deepseek-50-exact-20260928'
PYTHON=ROOT/'.venv/bin/python'; RUNNER=ROOT/'benchmarks/run_v2_target_category_parallel_20260925.py'
CASES={
 'Debugging': '01_01 01_02 01_03 01_04 01_05 01_06 01_07 01_08 01_09 01_10 02_01 02_02 02_03 02_04 02_05 02_06 02_07 02_08 02_09 02_10 03_01 03_02 03_03 03_04 03_05 03_06 03_07 03_08 03_09 03_10 04_01 04_02 04_03 04_04 04_05 04_06 04_07 04_08 04_09 04_10 05_01 05_02 05_03 05_04 05_05 05_06 05_07 05_08 05_09 05_10',
 'Template': '01_01 01_02 01_03 01_04 01_05 01_06 01_07 01_08 01_09 02_01 02_02 02_03 02_04 02_05 02_06 03_01 03_02 03_03 03_04 04_03 04_04 05_01 05_02 06_01 06_02 06_03 06_04 06_05 06_06 06_07 06_08 06_09 06_11 06_12 06_13 06_14 06_15 06_16 06_17 06_18 06_19 06_20 06_21 06_22 06_23 06_24 06_25 07_01 07_02 07_03',
}

def run(spec):
    cat,arm=spec; log=OUT/'logs'/f'{arm}__{cat}.log'; log.parent.mkdir(parents=True,exist_ok=True)
    cmd=[str(PYTHON),str(RUNNER),'--category',cat,'--arm',arm,'--model','dashscope/deepseek-v4-flash-0731','--model-slug','deepseek-v4-flash-0731','--output',str(OUT),'--parallelism','20','--task-ids',*CASES[cat].split(),'--base-url','http://47.96.153.159:8010/v1','--api-key-file','/tmp/spreadsheet-harness-litellm.key']
    env=os.environ.copy(); env['PYTHONPATH']=str(ROOT/'src'); env['SHEET_HARNESS_DEBUGGING_COVERAGE_TRIAL']='1'; env['SHEET_HARNESS_DEBUGGING_EXACT_TRIAL']='1'; env['SHEET_HARNESS_TEMPLATE_EXACT_TRIAL']='1'
    with log.open('w') as f: return subprocess.run(cmd,cwd=ROOT,env=env,stdout=f,stderr=subprocess.STDOUT).returncode

OUT.mkdir(parents=True,exist_ok=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(run,[(cat,arm) for cat in CASES for arm in ('spreadsheet-harness-basic','spreadsheet-harness-financial')]))
