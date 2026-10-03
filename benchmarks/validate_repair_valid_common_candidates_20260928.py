#!/usr/bin/env python3
from __future__ import annotations
import concurrent.futures as futures, json, subprocess
from pathlib import Path
from spreadsheet_harness.continuous_evolution import CandidateProposal, ContinuousEvolutionConfig, EvolutionRoute, RevisionStore, _run_adapter_command
from spreadsheet_harness.plugins import default_plugin_registry

REPO=Path(__file__).resolve().parents[1]
OLD=REPO/'benchmarks/results/fin15k-plugin-evolution-20260920-deepseek-formal-v2-500-profiled-v7-20260922'
ROOT=REPO/'benchmarks/results/fin15k-valid-common-evolution-20260928'
OUT=ROOT/'validated'
MECH={'h-only':'general-only','d-only':'domain-only','joint':'coevolution'}

def read(p): return json.loads(Path(p).read_text())

def run(size, mech):
    scope=MECH[mech]; workspace=OLD/f'workspaces/fin15k-500-{scope}'; config=ContinuousEvolutionConfig.load(OLD/f'configs/fin15k-500-{scope}.json'); store=RevisionStore(workspace); state=store.load_state(); incumbent=str(state['current_revision_sha256']); base=read(workspace/'rounds/000001/proposal/request.json')
    profile_arm='basic' if mech=='h-only' else 'financial'
    profile=read(ROOT/f'profiles/size-{size}-{profile_arm}/plugin-profile.json')
    ledger=[json.loads(x) for x in (ROOT/f'profiles/size-{size}-{profile_arm}/plugin-task-ledger.jsonl').read_text().splitlines()]
    failures=[r for r in ledger if r.get('outcome')=='fail']; passes=[r for r in ledger if r.get('outcome')=='pass']
    req=dict(base); req['candidate_limit']=1; req['base_revision_sha256']=incumbent; req['base_revision']['revision_sha256']=incumbent; req['plugin_profile']=profile; req['evidence_packet']={'schema_version':'valid-common-evidence-v1','input_trace_count':size,'failure_prototypes':failures[:24],'no_regression_anchors':passes[:12]}; req['profile_guidance']={'mechanism':mech,'source':f'valid common Fin-1.5K traces size {size}','heldout_feedback_allowed':False,'instruction':('Return exactly one candidate. For H-only/D-only return one valid contract-bound mutation. For Joint return a JSON mutations list containing exactly one mutation for each route target. Do not return prose-only or composition-only output. Implementation edits must include a concrete unified diff with exact file markers.')}
    d=OUT/f'proposals/size-{size}/{mech}'; d.mkdir(parents=True,exist_ok=True); reqp=d/'request.json'; resp=d/'response.json'; reqp.write_text(json.dumps(req,ensure_ascii=False,indent=2));
    try:
        proposer=[str(REPO/'.venv/bin/python'),str(REPO/'tools/propose_method_candidate.py'),'{request}','{response}','--base-url','http://10.130.138.46:8010/v1','--api-key-file','/tmp/spreadsheet-harness-litellm.key','--model','dashscope/glm-5.2','--timeout','3600','--max-tokens','12000']
        document=_run_adapter_command(proposer,request=req,directory=d,timeout=config.command_timeout_seconds)
        raw=(document.get('candidates') or [None])[0]
        if not isinstance(raw,dict): raise RuntimeError('GLM returned no candidate')
        raw=dict(raw); raw['candidate_id']=f'valid-{size}-{mech}-c01'; proposal=CandidateProposal.from_document(raw); route=EvolutionRoute.from_dict(req['route']); candidate_dir,revision=store.materialize_candidate(proposal=proposal,route=route,incumbent_revision=incumbent,registry=default_plugin_registry(),static_checks=config.static_checks,timeout=config.command_timeout_seconds)
        result={'size':size,'mechanism':mech,'status':'valid','candidate_id':proposal.candidate_id,'candidate_dir':str(candidate_dir.resolve()),'revision_sha256':revision['revision_sha256'],'surface':proposal.mutations[0].surface if proposal.mutations else proposal.surface}
    except Exception as e:
        result={'size':size,'mechanism':mech,'status':'invalid','error':str(e)}
    (d/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n'); return result

def main():
    jobs=[(s,m) for s in (50,200) for m in MECH]
    with futures.ThreadPoolExecutor(max_workers=6) as p: rows=list(p.map(lambda x:run(*x),jobs))
    OUT/'validation-summary.json'
    (OUT/'validation-summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n'); print(json.dumps(rows,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
