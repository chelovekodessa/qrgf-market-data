#!/usr/bin/env python3
"""Deterministic crash/replay self-test for the V4 GitHub state producer."""
from __future__ import annotations
import json,tempfile
from pathlib import Path
import promote_v4_state as p

def check(cond,msg):
    if not cond:raise AssertionError(msg)
def write(path:Path,v):p.write(path,v)
def make_passport(ticker:str,issuer:str,event="2026-08-18",asof="2026-08-18T10:00:00Z",score=90.0):
    summary={"ticker":ticker,"issuer_id":issuer,"quality_status":"pass","quality_score":score,"quality_coverage_pct":100.0,"quality_eligible":True,"quality_policy_version":p.QUALITY_POLICY_VERSION,"as_of":asof,"reuse_class":"structural_quality"}
    payload={"schema_version":"1.0.0","kind":"qrgf_quality_passport","issuer_id":issuer,"quality_policy_version":p.QUALITY_POLICY_VERSION,"economic_archetype":"established_quality","listing_overlay":"none","as_of":asof,"summary":summary,"structural_facts":{"score":score},"component_details":{},"evidence_lineage":[]}
    body={"schema_version":"1.0.0","kind":"qrgf_v4_passport_update_proposal","research_scope_key":f"{issuer}|common_equity","issuer_id":issuer,"security_overlay":"common_equity","quality_policy_version":p.QUALITY_POLICY_VERSION,"event_scan_through":event,"summary":summary,"passport_sha256":p.sem(payload),"passport_payload":payload}
    return {**body,"proposal_sha256":p.sem(body)}
def batch(items):
    body={"schema_version":"1.0.0","kind":"qrgf_v4_registry_batch","items":items};return {**body,"batch_sha256":p.sem(body)}
def make_cohort(scopes,market="2026-08-18"):
    rows=[]
    for i,(ticker,issuer) in enumerate(scopes,1):rows.append({"rank":i,"ticker":ticker,"contract_id":f"US:{ticker}","issuer_id":issuer,"security_overlay":"common_equity","research_scope_key":f"{issuer}|common_equity","security_type":"common_equity","sector":"Technology","market_cap":1e11,"avg_dollar_volume":1e9,"member_tickers":[ticker],"member_contract_ids":[f"US:{ticker}"],"bootstrap_best_lane":"established_quality","bootstrap_priority_score":90.0,"bootstrap_fact_coverage_pct":100.0})
    body={"schema_version":"1.0.0","kind":"qrgf_v4_core500_cohort","architecture_version":"4.0.0","market_session_id":market,"selection_model_version":"fixture","requested_size":len(rows),"selected_scope_count":len(rows),"core500_is_research_bootstrap_not_whitelist":True,"current_recovery_used":False,"scopes":rows}
    return {**body,"cohort_sha256":p.sem(body)}
def core_prop(c):
    body={"schema_version":"1.0.0","kind":"qrgf_v4_core500_publish_proposal","cohort":c};return {**body,"proposal_sha256":p.sem(body)}

def main():
    rel1="a"*64;rel2="b"*64;t1="2026-08-18T10:00:00Z";t2="2026-08-18T12:00:00Z";a=make_passport("AAA","ISSUER:AAA");b=make_passport("BBB","ISSUER:BBB")
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);first=p.publish_passport(root,a,rel1,t1);again=p.publish_passport(root,a,rel2,t2)
        check(again["status"]=="already_applied","replay not skipped");check(again["receipt"]==first["receipt"],"receipt changed on replay")
        bb=batch([a,b]);write(root/"data/v4/registry/proposals/batch.json",bb);items=p.expand_registry_files(root/"data/v4/registry/proposals");p.validate_pending_uniqueness(root,items)
        out=[p.publish_passport(root,x,rel2,t2) for x in items];check(sum(x["status"].startswith("already_applied") for x in out)==1,"partial batch replay wrong");check(sum(x["status"]=="applied" for x in out)==1,"partial batch resume wrong")
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);st=p.publish_passport(root,a,rel1,t1);p.receipt_path(root,a["proposal_sha256"]).unlink();newer=make_passport("AAA","ISSUER:AAA",event="2026-08-19")
        try:p.publish_passport(root,newer,rel2,t2);raise AssertionError("missing current receipt was bypassed")
        except ValueError as e:check("missing its receipt" in str(e),str(e))
        recovered=p.publish_passport(root,a,rel2,t2);check(recovered["status"]=="receipt_recovered","exact partial state did not recover")
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);p.publish_passport(root,a,rel1,t1);conflict=make_passport("AAA","ISSUER:AAA",score=89.0)
        try:p.publish_passport(root,conflict,rel2,t2);raise AssertionError("same-version conflict accepted")
        except ValueError as e:check("same logical Registry version" in str(e),str(e))
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);c=make_cohort([("AAA","ISSUER:AAA")]);cp=core_prop(c);p.publish_core(root,cp,rel1);p.publish_passport(root,a,rel1,t1);prog=p.rebuild_progress(root,rel1);check(prog["durable_reviewed_count"]==1,"receipt-backed progress failed")
        p.receipt_path(root,a["proposal_sha256"]).unlink();prog2=p.rebuild_progress(root,rel1);check(prog2["durable_reviewed_count"]==0 and prog2["pending_count"]==1,"progress ignored missing receipt")
        same=p.publish_core(root,cp,rel2);check(same["status"]=="already_applied","Core cohort replay rewrote latest")
        other=core_prop(make_cohort([("BBB","ISSUER:BBB")],market="2026-08-19"))
        try:p.publish_core(root,other,rel2);raise AssertionError("unfinished cohort replaced")
        except ValueError as e:check("unfinished Core500" in str(e),str(e))
    print("V4 STATE PRODUCER SELFTEST PASS")
    return 0
if __name__=="__main__":raise SystemExit(main())
