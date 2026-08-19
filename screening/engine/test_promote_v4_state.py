#!/usr/bin/env python3
"""V4.1 producer integration and crash/replay self-test."""
from __future__ import annotations
import copy,tempfile
from pathlib import Path
import promote_v4_state as p

def check(cond,msg):
    if not cond:raise AssertionError(msg)
def make_passport(ticker,issuer,event="2026-08-18",asof="2026-08-18T10:00:00Z",score=90.0):
    summary={"ticker":ticker,"issuer_id":issuer,"quality_status":"pass","quality_score":score,"quality_coverage_pct":100.0,"quality_eligible":True,"quality_policy_version":p.QUALITY_POLICY_VERSION,"as_of":asof,"reuse_class":"structural_quality"}
    payload={"schema_version":"1.0.0","kind":"qrgf_quality_passport","issuer_id":issuer,"quality_policy_version":p.QUALITY_POLICY_VERSION,"economic_archetype":"established_quality","listing_overlay":"none","as_of":asof,"summary":summary,"structural_facts":{"score":score},"component_details":{},"evidence_lineage":[]}
    body={"schema_version":"1.0.0","kind":"qrgf_v4_passport_update_proposal","research_scope_key":f"{issuer}|common_equity","issuer_id":issuer,"security_overlay":"common_equity","quality_policy_version":p.QUALITY_POLICY_VERSION,"event_scan_through":event,"summary":summary,"passport_sha256":p.sem(payload),"passport_payload":payload}
    return {**body,"proposal_sha256":p.sem(body)}
def bundle():
    rows=[{"ticker":f"T{i:03d}","contract_id":f"US:T{i:03d}","issuer_id":f"ISSUER:{i:03d}","quality_candidate_lanes":["established_quality"],"facts":{"roic":0.2}} for i in range(500)]
    source_body={"schema_version":"2.0.0","kind":p.SOURCE_KIND,"architecture_version":"4.1.0","market_session_id":"2026-08-18","eligible_universe_size":4000,"candidate_source_identity":{"kind":"fixture-quality-union","snapshot_sha256":"f"*64},"quality_candidate_union_size":500,"lane_counts":{"established_quality":500},"current_recovery_used":False,"forbidden_recovery_fields":[],"regression_expectations":[],"candidates":rows}
    source={**source_body,"source_sha256":p.sem(source_body)}
    scopes=[]
    for i,row in enumerate(rows,1):
        scopes.append({"rank":i,"ticker":row["ticker"],"contract_id":row["contract_id"],"issuer_id":row["issuer_id"],"security_overlay":"common_equity","research_scope_key":f"{row['issuer_id']}|common_equity","security_type":"common_equity","sector":"Technology","market_cap":1e11,"avg_dollar_volume":1e9,"member_tickers":[row["ticker"]],"member_contract_ids":[row["contract_id"]],"bootstrap_best_lane":"established_quality","bootstrap_priority_score":90.0,"bootstrap_fact_coverage_pct":100.0})
    content={"schema_version":"2.0.0","kind":p.MASTER_KIND,"architecture_version":"4.1.0","market_session_id":"2026-08-18","selection_model_version":"fixture","requested_size":500,"selected_scope_count":500,"core500_is_research_bootstrap_not_whitelist":True,"current_recovery_used":False,"candidate_source_sha256":source["source_sha256"],"selector_config_sha256":"c"*64,"canary_scope_keys":[x["research_scope_key"] for x in scopes[:15]],"pilot_scope_keys":[x["research_scope_key"] for x in scopes[:50]],"scopes":scopes}
    content_sha=p.sem(content)
    cert_body={"schema_version":"2.0.0","kind":p.CERT_KIND,"architecture_version":"4.1.0","master_content_sha256":content_sha,"market_session_id":"2026-08-18","candidate_source_identity":source["candidate_source_identity"],"candidate_source_sha256":source["source_sha256"],"eligible_universe_size":4000,"quality_candidate_union_size":500,"lane_counts":{"established_quality":500},"selector_model_version":"fixture","selector_config_sha256":"c"*64,"fact_coverage":{"minimum_pct":40,"eligible_scope_count":500},"current_recovery_used":False,"forbidden_recovery_fields":[],"issuer_dedup":{"enabled":True,"stats":{"raw_quality_candidate_count":500,"bootstrap_score_eligible_count":500,"unique_research_scope_count":500,"issuer_dedup_removed_rows":0}},"requested_size":500,"selected_scope_count":500,"cohort_scope_keys_sha256":p.sem([x["research_scope_key"] for x in scopes]),"deterministic_ordering_model":p.SELECTOR_ORDERING_MODEL,"cutoff_diagnostics":{"raw_quality_candidate_count":500,"bootstrap_score_eligible_count":500,"unique_research_scope_count":500,"issuer_dedup_removed_rows":0,"cutoff_rank":500,"cutoff_score":90.0,"next_excluded_score":None},"regression_expectations":[],"regression_expectations_satisfied":True}
    cert={**cert_body,"certificate_sha256":p.sem(cert_body)}
    master_body={**content,"master_content_sha256":content_sha,"selector_certificate_sha256":cert["certificate_sha256"]};master={**master_body,"master_sha256":p.sem(master_body)}
    return {"schema_version":"2.0.0","kind":"qrgf_v41_master_core500_bundle","candidate_source":source,"master":master,"selector_certificate":cert}
def proposal(b):
    body={"schema_version":"2.0.0","kind":"qrgf_v41_master_core500_publish_proposal","bundle":b};return {**body,"proposal_sha256":p.sem(body)}
def gate(master,kind,snapshot="a"*64):
    if kind=="runtime":body={"schema_version":"2.0.0","kind":"qrgf_v41_runtime_reconstruction_gate","master_sha256":master["master_sha256"],"master_content_sha256":master["master_content_sha256"],"registry_snapshot_sha256":snapshot,"canary_scope_count":15,"canary_durable_count":15,"runtime_reconstruction_passed":True,"reconstructed_at":"2026-08-18T12:00:00Z","validator_release_sha256":"b"*64}
    else:body={"schema_version":"2.0.0","kind":"qrgf_v41_pilot_registry_gate","master_sha256":master["master_sha256"],"master_content_sha256":master["master_content_sha256"],"registry_snapshot_sha256":snapshot,"pilot_scope_count":50,"pilot_durable_count":50,"registry_loss_count":0,"reuse_check_passed":True,"pilot_gate_passed":True,"verified_at":"2026-08-18T12:00:00Z","validator_release_sha256":"b"*64}
    return {**body,"gate_sha256":p.sem(body)}
def main():
    rel1="a"*64;rel2="b"*64;t1="2026-08-18T10:00:00Z";t2="2026-08-18T12:00:00Z";a=make_passport("AAA","ISSUER:AAA");b=make_passport("BBB","ISSUER:BBB")
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);first=p.publish_passport(root,a,rel1,t1);again=p.publish_passport(root,a,rel2,t2);check(again["status"]=="already_applied" and again["receipt"]==first["receipt"],"receipt replay changed")
        p.receipt_path(root,a["proposal_sha256"]).unlink();newer=make_passport("AAA","ISSUER:AAA",event="2026-08-19")
        try:p.publish_passport(root,newer,rel2,t2);raise AssertionError("missing current receipt was bypassed")
        except ValueError as exc:check("missing its receipt" in str(exc),str(exc))
        check(p.publish_passport(root,a,rel2,t2)["status"]=="receipt_recovered","exact receipt recovery failed")
    bndl=bundle();master=bndl["master"];check(p.validate_master_bundle(bndl)["master"]["master_sha256"]==master["master_sha256"],"valid MASTER rejected")
    small=copy.deepcopy(master);small["requested_size"]=15;content={k:v for k,v in small.items() if k not in {"master_content_sha256","selector_certificate_sha256","master_sha256"}};small["master_content_sha256"]=p.sem(content);body={k:v for k,v in small.items() if k!="master_sha256"};small["master_sha256"]=p.sem(body)
    try:p.validate_master(small);raise AssertionError("Core15 accepted as MASTER")
    except ValueError as exc:check("exactly 500" in str(exc),str(exc))
    contaminated=copy.deepcopy(bndl);contaminated["candidate_source"]["candidates"][0]["facts"]["current_price"]=100;contaminated["candidate_source"]["source_sha256"]=p.sem({k:v for k,v in contaminated["candidate_source"].items() if k!="source_sha256"})
    try:p.validate_master_bundle(contaminated);raise AssertionError("recovery contaminated source accepted")
    except ValueError as exc:check("recovery/current-price" in str(exc),str(exc))
    aliased=copy.deepcopy(bndl);aliased["candidate_source"]["candidates"][0]["facts"]["last_price"]=100;aliased["candidate_source"]["source_sha256"]=p.sem({k:v for k,v in aliased["candidate_source"].items() if k!="source_sha256"})
    try:p.validate_master_bundle(aliased);raise AssertionError("aliased market source accepted")
    except ValueError as exc:check("recovery/current-price" in str(exc),str(exc))
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);pr=proposal(bndl);first=p.publish_master(root,pr,rel1,t1);check(first["status"]=="applied","MASTER publish failed");check(p.publish_master(root,pr,rel2,t2)["status"]=="already_applied","MASTER replay failed")
        state0=p.rebuild_campaign_state(root,rel1,t1);check(state0["phase"]=="CANARY" and state0["daily_broad_allowed"] is False,"initial phase wrong")
        for scope in master["scopes"][:15]:p.publish_passport(root,make_passport(scope["ticker"],scope["issuer_id"]),rel1,t1)
        state15=p.rebuild_campaign_state(root,rel1,t1);check(state15["phase"]=="CANARY" and state15["core500_complete"] is False,"15/15 became complete")
        runtime=gate(master,"runtime");p.immutable(root/"data/v4/campaigns"/master["master_sha256"]/"gates/runtime-reconstruction.json",runtime);pilot=p.rebuild_campaign_state(root,rel1,t1);check(pilot["phase"]=="PILOT","runtime gate did not enter PILOT")
        for scope in master["scopes"][15:50]:p.publish_passport(root,make_passport(scope["ticker"],scope["issuer_id"]),rel1,t1)
        state50=p.rebuild_campaign_state(root,rel1,t1);check(state50["phase"]=="PILOT","Pilot gate bypassed")
        p.immutable(root/"data/v4/campaigns"/master["master_sha256"]/"gates/pilot-registry.json",gate(master,"pilot"));core=p.rebuild_campaign_state(root,rel1,t1);check(core["phase"]=="CORE500","PILOT gate did not enter CORE500")
        for scope in master["scopes"][50:499]:p.publish_passport(root,make_passport(scope["ticker"],scope["issuer_id"]),rel1,t1)
        state499=p.rebuild_campaign_state(root,rel1,t1);check(state499["phase"]=="CORE500" and state499["daily_broad_allowed"] is False,"499 enabled broad")
        p.publish_passport(root,make_passport(master["scopes"][499]["ticker"],master["scopes"][499]["issuer_id"]),rel1,t1);complete=p.rebuild_campaign_state(root,rel1,t1);check(complete["phase"]=="COMPLETE" and complete["daily_broad_allowed"] is True,"500 did not complete")
        recovered=p.rebuild_campaign_state(root,rel1,t1);check(recovered["phase"]=="COMPLETE" and recovered["master_durable_count"]==500,"runtime reconstruction failed")
    # A full set of durable records is still only PILOT until the separate
    # zero-loss/reuse attestation exists.  This catches the old count-only
    # completion defect directly.
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);p.publish_master(root,proposal(bndl),rel1,t1)
        for scope in master["scopes"]:p.publish_passport(root,make_passport(scope["ticker"],scope["issuer_id"]),rel1,t1)
        p.immutable(root/"data/v4/campaigns"/master["master_sha256"]/"gates/runtime-reconstruction.json",gate(master,"runtime"))
        blocked=p.rebuild_campaign_state(root,rel1,t1)
        check(blocked["phase"]=="PILOT" and blocked["daily_broad_allowed"] is False,"500 durable records bypassed PILOT gate")
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);p.publish_master(root,proposal(bndl),rel1,t1)
        for scope in master["scopes"][:50]:p.publish_passport(root,make_passport(scope["ticker"],scope["issuer_id"]),rel1,t1)
        p.immutable(root/"data/v4/campaigns"/master["master_sha256"]/"gates/pilot-registry.json",gate(master,"pilot"))
        blocked=p.rebuild_campaign_state(root,rel1,t1)
        check(blocked["phase"]=="CANARY" and blocked["daily_broad_allowed"] is False,"PILOT gate bypassed runtime reconstruction")
    print("V4.1 STATE PRODUCER SELFTEST PASS")
    return 0
if __name__=="__main__":raise SystemExit(main())
