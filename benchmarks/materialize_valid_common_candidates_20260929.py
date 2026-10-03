#!/usr/bin/env python3
from __future__ import annotations
import json, shutil, tempfile
from pathlib import Path
from spreadsheet_harness.continuous_evolution import CandidateProposal, ContinuousEvolutionConfig, EvolutionRoute, RevisionStore
from spreadsheet_harness.plugins import SPREADSHEET_HARNESS_BASIC_COMPOSITION, SPREADSHEET_HARNESS_FINANCIAL_COMPOSITION, default_plugin_registry

REPO=Path(__file__).resolve().parents[1]; SRC=REPO/'benchmarks/results/fin15k-valid-common-evolution-20260928'; OUT=REPO/'benchmarks/results/fin15k-valid-common-materialized-20260929'

def read(p): return json.loads(Path(p).read_text())

def config(composition):
    return ContinuousEvolutionConfig(repository_root=REPO, composition=composition, groups={'harness':tuple(), 'domain':tuple()}, contexts=(), heldout_task_ids=(), initial_evidence=(), evaluation_binding={'source':'valid-common-fin15k','spreadsheetbench_used':False}, policy=__import__('spreadsheet_harness.continuous_evolution',fromlist=['PromotionPolicy']).PromotionPolicy(), first_group='harness', max_rounds=1, max_candidates_per_round=1, proposer_command=(), evaluator_command=(), command_timeout_seconds=1800, static_checks=(), allowed_operators=('revision',), allowed_update_scopes=('harness','domain','joint'))

def one(size, mech, response):
    comp=SPREADSHEET_HARNESS_BASIC_COMPOSITION if mech=='h-only' else SPREADSHEET_HARNESS_FINANCIAL_COMPOSITION
    root=OUT/f'size-{size}-{mech}/workspace'; root.mkdir(parents=True,exist_ok=True); store=RevisionStore(root); cfg=config(comp); state=store.initialize(cfg,default_plugin_registry()); incumbent=state['current_revision_sha256']
    old_req=read(SRC/f'proposals/size-{size}/{mech}/request.json'); route_doc=old_req['route']; route=EvolutionRoute.from_dict(route_doc)
    raw=read(response)['candidates'][0]; raw=dict(raw); raw['base_revision_sha256']=incumbent; raw['candidate_id']=f'valid-common-{size}-{mech}-c01'; proposal=CandidateProposal.from_document(raw)
    try:
        cd,rev=store.materialize_candidate(proposal=proposal,route=route,incumbent_revision=incumbent,registry=default_plugin_registry(),static_checks=cfg.static_checks,timeout=1800)
        result={'status':'valid','size':size,'mechanism':mech,'candidate_id':proposal.candidate_id,'candidate_dir':str(cd.resolve()),'revision_sha256':rev['revision_sha256'],'base_revision_sha256':incumbent}
    except Exception as exc:
        result={'status':'rejected','size':size,'mechanism':mech,'error':str(exc),'base_revision_sha256':incumbent}
    (OUT/f'size-{size}-{mech}').mkdir(parents=True,exist_ok=True); (OUT/f'size-{size}-{mech}/materialization.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n'); return result

def main():
    jobs=[]
    for size in (50,200,500):
        for mech in ('h-only','d-only','joint'):
            p=SRC/f'proposals/size-{size}/{mech}/response.json'
            if not p.exists() and mech=='d-only' and size==50: p=SRC/f'proposals/size-{size}/{mech}/response-v3.json'
            if p.exists(): jobs.append((size,mech,p))
    rows=[one(*j) for j in jobs]; (OUT/'summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n'); print(json.dumps(rows,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
