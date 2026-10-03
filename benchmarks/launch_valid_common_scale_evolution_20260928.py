#!/usr/bin/env python3
from __future__ import annotations
import concurrent.futures as futures
import json, subprocess
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
PROFILE_ROOT=REPO/'benchmarks/results/fin15k-plugin-evolution-20260920-deepseek-formal-v2-500-profiled-v7-20260922'
VALID_ROOT=REPO/'benchmarks/results/fin15k-valid-common-20260928'
OUT=REPO/'benchmarks/results/fin15k-valid-common-evolution-20260928'
MECH={'h-only':'general-only','d-only':'domain-only','joint':'coevolution'}
MODEL='dashscope/glm-5.2'; BASE='http://10.130.138.46:8010/v1'; KEY='/tmp/spreadsheet-harness-litellm.key'

def read(p): return json.loads(Path(p).read_text())
def compact(rows):
    return [{k:r.get(k) for k in ('task_id','category','outcome','score','failure_reasons','selected_plugins','invoked','trace_weight')} for r in rows]

def one(size, mech):
    arm='basic' if mech=='h-only' else 'financial'
    profile_root=VALID_ROOT/f'size-{size}-{arm}'
    profile=read(profile_root/'plugin-profile/plugin-profile.json')
    # Read selected task ledger from the profile output; preserve only valid rows.
    ledger=[json.loads(x) for x in (profile_root/'plugin-profile/plugin-task-ledger.jsonl').read_text().splitlines()]
    fails=[r for r in ledger if r.get('outcome')=='fail']
    passes=[r for r in ledger if r.get('outcome')=='pass']
    evidence={'schema_version':'valid-common-plugin-evidence-v1','input_trace_count':size,'failure_count':len(fails),'success_count':len(passes),'failure_prototypes':compact(fails[:24]),'no_regression_anchors':compact(passes[:12]),'redaction':'Valid common Fin-1.5K traces only; no SpreadsheetBench evidence.'}
    workspace=PROFILE_ROOT/f'workspaces/fin15k-500-{MECH[mech]}'
    request=read(workspace/'rounds/000001/proposal/request.json')
    state=read(workspace/'state.json')
    request['candidate_limit']=3
    request['plugin_profile']=profile
    request['evidence_packet']=evidence
    request['base_revision_sha256']=state['current_revision_sha256']
    request['base_revision']['revision_sha256']=state['current_revision_sha256']
    request['profile_guidance']={'mechanism':mech,'source':f'valid common Fin-1.5K traces, size={size}','heldout_feedback_allowed':False,'instruction':f'Generate exactly 3 materially different candidates grounded only in these {size} valid plugin-linked traces. Preserve Template safety. For implementation mutations return a real unified diff; do not return composition-only fallback.'}
    d=OUT/f'proposals/size-{size}/{mech}'; d.mkdir(parents=True,exist_ok=True)
    req=d/'request.json'; resp=d/'response.json'; req.write_text(json.dumps(request,ensure_ascii=False,indent=2))
    cmd=[str(REPO/'.venv/bin/python'),str(REPO/'tools/propose_evolution_candidate_harness.py'),str(req),str(resp),'--base-url',BASE,'--api-key-file',KEY,'--model',MODEL,'--timeout','1800','--max-attempts','4']
    log=(d/'stderr.log').open('w')
    p=subprocess.run(cmd,cwd=REPO,stdout=subprocess.PIPE,stderr=log,timeout=4200,check=False,text=True)
    if resp.exists():
        try: count=len(read(resp).get('candidates') or [])
        except Exception: count=0
    else: count=0
    return {'size':size,'mechanism':mech,'returncode':p.returncode,'candidate_count':count,'response':str(resp),'request':str(req)}

def main():
    jobs=[(500,m) for m in MECH]
    with futures.ThreadPoolExecutor(max_workers=6) as pool:
        rows=list(pool.map(lambda x: one(*x),jobs))
    (OUT/'launch-summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(rows,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
