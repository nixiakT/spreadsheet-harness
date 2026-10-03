import json, os, subprocess, concurrent.futures
from pathlib import Path
repo=Path('/data/zju-160/tongzeyuan/spreadsheet-harness')
src=repo/'benchmarks/results/glm51-spreadsheet-rl-native-v1-v2-full-20260929'
root=repo/'benchmarks/results/glm51-spreadsheet-rl-native-provider-retry-20261001-p4c'
root.joinpath('v1/tasks').mkdir(parents=True,exist_ok=True); root.joinpath('v2/tasks').mkdir(parents=True,exist_ok=True)
jobs=[]
for kind in ('v1','v2'):
  for p in sorted((src/kind/'tasks').glob('*/results.json')):
    try: data=json.loads(p.read_text())
    except Exception: continue
    for item in (data if isinstance(data,list) else [data]):
      e=(str(item.get('error_type') or '')+' '+str(item.get('error') or '')+' '+str(item.get('outcome_kind') or '')).lower()
      if str(item.get('error_type') or '')=='ProviderError' or any(x in e for x in ('ratelimit','rate limit','no deployments available','deployment','quota','429','timeout')):
        t=str(item.get('task_id') or p.parent.name.replace('__','/')); jobs.append((kind,t,t.split('/',1)[0] if kind=='v2' else ''))
seen=set(); jobs=[j for j in jobs if not ((j[0],j[1]) in seen or seen.add((j[0],j[1])))]
(root/'task-plan.tsv').write_text(''.join('\t'.join(j)+'\n' for j in jobs)); print('planned',len(jobs),flush=True)
env=os.environ.copy()
for k in ('OPENAI_API_KEY','HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy'): env.pop(k,None)
env['NO_PROXY']='127.0.0.1,localhost'
def run(j):
  kind,task,cat=j; slug=task.replace('/','__').replace(' ','_'); out=root/kind/'tasks'/slug; log=root/kind/'tasks'/(slug+'.log')
  if (out/'results.json').is_file(): return 'existing'
  if kind=='v1': cmd=['.venv/bin/python','-m','spreadsheet_harness.cli','benchmark','v1-compare','--dataset','benchmarks/data/spreadsheetbench_912_v0.1']
  else: cmd=['.venv/bin/python','-m','spreadsheet_harness.cli','benchmark','v2-compare','--dataset','benchmarks/data/spreadsheetbench-v2','--official-evaluator','benchmarks/vendor/spreadsheetbench2-official-83d415c/evaluation/evaluation.py','--category',cat]
  cmd += ['--task-id',task,'--arm','spreadsheet-rl-native','--output',str(out),'--max-model-calls','50','--max-turns-per-arm','50','--max-total-tokens','10000000','--max-output-tokens','32768','--task-timeout','21600','--request-timeout','600','--litellm-timeout','600','--request-retries','5','--request-interval-seconds','1.1','--arm-order-seed','20261001','--base-url','http://10.130.138.46:8010/v1','--api-key-file','/tmp/spreadsheet-harness-litellm.key','--model','dashscope/glm-5.1','--api-protocol','chat-completions','--reasoning-effort','medium','--seed','41','--temperature','0','--top-p','1','--enable-thinking']
  with log.open('w') as f: p=subprocess.run(cmd,cwd=repo,env=env,stdout=f,stderr=subprocess.STDOUT)
  return 'done' if p.returncode==0 else f'rc{p.returncode}'
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
  for i,r in enumerate(ex.map(run,jobs),1):
    if i%4==0: print(i,'/',len(jobs),r,flush=True)
print('finished',flush=True)
