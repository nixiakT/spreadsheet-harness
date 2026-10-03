#!/usr/bin/env python3
"""Generate the no-plugin-trace control candidates for the 50/200/500 study.

The control keeps the same incumbent revisions, proposer, mechanism routes and
evidence counts as the plugin-trace experiment.  It replaces the normalized
plugin ledger/profile with bounded structural summaries of the original raw
trajectory.jsonl files and explicitly removes plugin attribution fields.
"""
from __future__ import annotations

import argparse, hashlib, json, re
from collections import Counter
from pathlib import Path
from typing import Any

from spreadsheet_harness.continuous_evolution import CandidateProposal, ContinuousEvolutionConfig, EvolutionRoute, RevisionStore
from spreadsheet_harness.plugins import default_plugin_registry

REPO = Path(__file__).resolve().parents[1]
PROFILE_ROOT = REPO / "benchmarks/results/fin15k-plugin-evolution-20260920-deepseek-formal-v2-500-profiled-v7-20260922"
BASELINE_ROOT = REPO / "benchmarks/results/fin15k-plugin-evolution-20260920-deepseek-formal-v2/baseline-trajectories"
MECH = {"h-only": "general-only", "d-only": "domain-only", "joint": "coevolution"}

def read(p: Path) -> Any: return json.loads(p.read_text(encoding="utf-8"))
def write(p: Path, x: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(x, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")

def find_raw(task_id: str) -> Path | None:
    for cell in BASELINE_ROOT.glob("*/cell.json"):
        try:
            d = read(cell)
            if str((d.get("task") or {}).get("task_id")) != task_id: continue
            p = Path(d.get("trajectory", ""))
            if p.is_file(): return p
        except Exception: pass
    return None

def raw_summary(path: Path, row: dict[str, Any]) -> dict[str, Any]:
    events=[]
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try: e=json.loads(line)
            except Exception: continue
            if not isinstance(e, dict): continue
            # Preserve raw execution structure, deliberately excluding every
            # plugin/attribution/profile field and raw model payload.
            item={k:e.get(k) for k in ("kind","phase","status","operation","tool","tool_name","passed","error","event_type") if k in e}
            if item: events.append(item)
            if len(events)>=80: break
    except Exception: pass
    kinds=Counter(str(e.get("kind") or e.get("event_type") or e.get("phase") or "unknown") for e in events)
    phases=Counter(str(e.get("phase") or "unknown") for e in events)
    return {"task_id":row.get("task_id"),"category":row.get("category"),"outcome":row.get("outcome"),
            "failure_reasons":row.get("failure_reasons") or {},"event_count":len(events),
            "event_kind_counts":dict(kinds),"phase_counts":dict(phases),
            "trajectory_sha256":hashlib.sha256(path.read_bytes()).hexdigest()}

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--size",type=int,choices=(50,200,500),required=True); ap.add_argument("--mechanisms",nargs="+",choices=tuple(MECH),default=tuple(MECH)); ap.add_argument("--output-root",type=Path,default=REPO/"benchmarks/results/fin15k-no-plugin-trace-ablation-20261003"); args=ap.parse_args()
    args.output_root = args.output_root.expanduser().resolve()
    ledger=[json.loads(x) for x in (PROFILE_ROOT/"plugin-profile-500/plugin-task-ledger.jsonl").read_text().splitlines()][:args.size]
    traces=[]; missing=[]
    for row in ledger:
        p=find_raw(str(row.get("task_id")))
        if p: traces.append(raw_summary(p,row))
        else: missing.append(row.get("task_id"))
    if missing: raise RuntimeError(f"missing raw trajectories: {missing[:5]} ({len(missing)})")
    packet={"schema_version":"raw-trajectory-ablation-v1","input_trace_count":args.size,"spreadsheetbench_used":False,"plugin_trace_used":False,"redaction":"Original raw trajectory structure only; plugin attribution/profile/selected-plugin fields removed.","traces":traces}
    write(args.output_root/f"evidence-{args.size}.json",packet)
    records=[]; registry=default_plugin_registry()
    for mechanism in args.mechanisms:
        scope=MECH[mechanism]; ws=PROFILE_ROOT/f"workspaces/fin15k-500-{scope}"; store=RevisionStore(ws); state=store.load_state(); incumbent=str(state["current_revision_sha256"])
        request=read(ws/"rounds/000001/proposal/request.json"); request.pop("plugin_profile",None); request.pop("profile_guidance",None); request["candidate_limit"]=1; request["evidence_packet"]=packet
        route_doc=dict(request.get("route") or {}); hashes=sorted(t["trajectory_sha256"] for t in traces); route_doc["evidence_sha256"]=hashes; route_doc["support_count"]=len(hashes)
        muts=[]
        for m in route_doc.get("mutations") or []:
            m=dict(m); m["evidence_sha256"]=hashes; m["support_count"]=len(hashes); muts.append(m)
        if muts: route_doc["mutations"]=muts
        request["route"]=route_doc; request["base_revision_sha256"]=incumbent; request["base_revision"]["revision_sha256"]=incumbent
        request["operator_policy"]={**(request.get("operator_policy") or {}),"instruction":"Use only the supplied original raw trajectory summaries. Do not use plugin trace, plugin profile, selected_plugins, attribution, or SpreadsheetBench evidence. Return exactly one contract-valid candidate."}
        out=(args.output_root/f"proposals/{args.size}/{mechanism}").resolve(); out.mkdir(parents=True,exist_ok=True); write(out/"request.json",request)
        config=ContinuousEvolutionConfig.load(PROFILE_ROOT/f"configs/fin15k-500-{scope}.json")
        from spreadsheet_harness.continuous_evolution import _run_adapter_command
        response=_run_adapter_command(config.proposer_command,request=request,directory=out,timeout=config.command_timeout_seconds)
        props=response.get("candidates") or []
        if not props: raise RuntimeError(f"no candidate returned for {args.size}/{mechanism}")
        raw=dict(props[0]); raw["candidate_id"]=f"raw{args.size}-{mechanism}-c01"; proposal=CandidateProposal.from_document(raw)
        candidate_dir, revision=store.materialize_candidate(proposal=proposal,route=EvolutionRoute.from_dict(route_doc),incumbent_revision=incumbent,registry=registry,static_checks=config.static_checks,timeout=config.command_timeout_seconds)
        records.append({"candidate_id":proposal.candidate_id,"candidate_dir":str(candidate_dir.resolve()),"revision_sha256":revision["revision_sha256"],"mechanism":mechanism,"evidence_scale":args.size,"source":"raw-trajectory-no-plugin-trace-ablation"})
    write(args.output_root/f"candidates-{args.size}.json",records); print(json.dumps(records,ensure_ascii=False,indent=2)); return 0
if __name__=='__main__': raise SystemExit(main())
