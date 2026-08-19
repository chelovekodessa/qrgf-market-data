#!/usr/bin/env python3
"""Replay-safe single-writer publisher for QRGF V4 state and Registry."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from typing import Any, Mapping

QUALITY_POLICY_VERSION="4.0.0-structural-v1"
TERMINAL={"pass","conditional","rejected","insufficient_data"}
SENSITIVE={"account","account_id","account_number","balances","positions","allocation","available_funds","buying_power","cash_balance","quote","bid","ask","raw_connector_response","licensed_payload","raw_payload"}

def canonical(v:Any)->bytes:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def sem(v:Any)->str:return hashlib.sha256(canonical(v)).hexdigest()
def sha(path:Path)->str:return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path:Path)->dict[str,Any]:
    v=json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(v,dict):raise ValueError(f"{path} must contain object")
    return v
def write(path:Path,v:Mapping[str,Any]):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(dict(v),ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
def immutable(path:Path,v:Mapping[str,Any]):
    if path.exists():
        if sem(load(path))!=sem(v):raise ValueError(f"immutable collision: {path}")
    else:write(path,v)
def reject_sensitive(v:Any,path=""):
    if isinstance(v,Mapping):
        for k,x in v.items():
            d=f"{path}.{k}" if path else str(k)
            if str(k).lower() in SENSITIVE:raise ValueError(f"sensitive field: {d}")
            reject_sensitive(x,d)
    elif isinstance(v,list):
        for i,x in enumerate(v):reject_sensitive(x,f"{path}[{i}]")
def scope_digest(key:str)->str:return hashlib.sha256(key.encode()).hexdigest()
def pointer_path(root:Path,key:str)->Path:return root/"data/v4/registry/scopes"/f"{scope_digest(key)}.json"
def passport_path(root:Path,key:str,h:str)->Path:return root/"data/v4/passports"/scope_digest(key)/f"{h}.json"
def passport_rel(key:str,h:str)->str:return f"data/v4/passports/{scope_digest(key)}/{h}.json"
def receipt_path(root:Path,proposal_sha:str)->Path:return root/"data/v4/registry/receipts"/f"{proposal_sha}.json"

def verify_release(root:Path,path:Path)->str:
    rel=load(path); expected={
        "promote_v4_state.py":root/"screening/engine/promote_v4_state.py",
        "test_promote_v4_state.py":root/"screening/engine/test_promote_v4_state.py",
        "promote-v4-state.yml":root/".github/workflows/promote-v4-state.yml",
    }
    if rel.get("schema_version")!="1.0.0" or rel.get("release_version")!="2.0.0" or set(rel.get("producer_hashes") or {})!=set(expected):raise ValueError("invalid V4.1 producer release")
    for name,p in expected.items():
        if not p.is_file() or sha(p)!=(rel["producer_hashes"] or {}).get(name):raise ValueError(f"v4 producer hash mismatch: {name}")
    return sha(path)

MASTER_SIZE=500
MASTER_KIND="qrgf_v41_master_core500"
SOURCE_KIND="qrgf_v41_quality_candidate_source"
CERT_KIND="qrgf_v41_master_core500_selector_certificate"
PHASES=("CANARY","PILOT","CORE500","COMPLETE")
SELECTOR_ORDERING_MODEL="bootstrap_priority_score_then_coverage_market_cap_liquidity_identity-v1"
FORBIDDEN_SOURCE_FIELDS={"price","current_price","last","close","bid","ask","quote","reference_52w_high","distance_to_high_pct","drawdown","drawdown_pct","drawdown_52w_pct","return_5d_pct","return_1m_pct","return_3m_pct","return_6m_pct","return_12m_pct","recovery_setup_score","l2_setup_score","research_priority_score","rsi","atr","momentum","historical_volatility_pct","setup_prior_growth","setup_pullback_geometry","setup_liquidity"}

def require_hash(value:Any,label:str)->str:
    text=str(value or "")
    if len(text)!=64 or any(x not in "0123456789abcdef" for x in text):raise ValueError(f"{label} must be lowercase SHA-256")
    return text
def forbidden_paths(value:Any,path="")->list[str]:
    out=[]
    if isinstance(value,Mapping):
        for key,item in value.items():
            dotted=f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_SOURCE_FIELDS:out.append(dotted)
            out.extend(forbidden_paths(item,dotted))
    elif isinstance(value,list):
        for index,item in enumerate(value):out.extend(forbidden_paths(item,f"{path}[{index}]"))
    return out
def validate_source(value:Mapping[str,Any])->dict[str,Any]:
    v=dict(value);body={k:x for k,x in v.items() if k!="source_sha256"}
    if v.get("schema_version")!="2.0.0" or v.get("kind")!=SOURCE_KIND or v.get("architecture_version")!="4.1.0" or v.get("source_sha256")!=sem(body):raise ValueError("invalid V4.1 quality candidate source")
    if v.get("current_recovery_used") is not False or list(v.get("forbidden_recovery_fields") or [])!=[]:raise ValueError("V4.1 source recovery declaration invalid")
    ident=v.get("candidate_source_identity") or {}
    if not str(ident.get("kind") or ""):raise ValueError("V4.1 source identity kind missing")
    require_hash(ident.get("snapshot_sha256"),"source snapshot hash")
    rows=v.get("candidates") or [];lanes=v.get("lane_counts") or {}
    if not isinstance(rows,list) or len(rows)<MASTER_SIZE or int(v.get("quality_candidate_union_size") or -1)!=len(rows) or int(v.get("eligible_universe_size") or 0)<len(rows):raise ValueError("invalid V4.1 source breadth")
    observed={}
    for row in rows:
        bad=forbidden_paths(row)
        if bad:raise ValueError(f"V4.1 source contains recovery/current-price fields: {bad}")
        row_lanes=row.get("quality_candidate_lanes") if isinstance(row,Mapping) else None
        if not isinstance(row_lanes,list) or not row_lanes:raise ValueError("V4.1 source row lacks quality lane")
        for lane in set(str(x) for x in row_lanes):observed[lane]=observed.get(lane,0)+1
    if {str(k):int(x) for k,x in lanes.items()}!=dict(sorted(observed.items())):raise ValueError("V4.1 source lane counts mismatch")
    return v
def validate_master(value:Mapping[str,Any])->dict[str,Any]:
    v=dict(value);body={k:x for k,x in v.items() if k!="master_sha256"}
    if v.get("schema_version")!="2.0.0" or v.get("kind")!=MASTER_KIND or v.get("architecture_version")!="4.1.0" or v.get("master_sha256")!=sem(body):raise ValueError("invalid V4.1 MASTER CORE500")
    content={k:x for k,x in v.items() if k not in {"master_content_sha256","selector_certificate_sha256","master_sha256"}}
    if v.get("master_content_sha256")!=sem(content):raise ValueError("V4.1 MASTER content hash mismatch")
    if int(v.get("requested_size") or -1)!=MASTER_SIZE or int(v.get("selected_scope_count") or -1)!=MASTER_SIZE or v.get("current_recovery_used") is not False or v.get("core500_is_research_bootstrap_not_whitelist") is not True:raise ValueError("V4.1 MASTER must be exactly 500 and recovery independent")
    scopes=v.get("scopes") or [];keys=[str(x.get("research_scope_key") or "") for x in scopes]
    if len(scopes)!=MASTER_SIZE or any(not x for x in keys) or len(set(keys))!=MASTER_SIZE or [int(x.get("rank") or -1) for x in scopes]!=list(range(1,MASTER_SIZE+1)):raise ValueError("V4.1 MASTER scope identity invalid")
    canary=list(v.get("canary_scope_keys") or []);pilot=list(v.get("pilot_scope_keys") or [])
    if len(canary)!=15 or len(pilot)!=50 or canary!=keys[:15] or pilot!=keys[:50] or not set(canary).issubset(pilot):raise ValueError("V4.1 MASTER phase subsets invalid")
    require_hash(v.get("candidate_source_sha256"),"MASTER source hash");require_hash(v.get("selector_config_sha256"),"MASTER selector config hash");require_hash(v.get("selector_certificate_sha256"),"MASTER certificate hash")
    return v
def validate_certificate(value:Mapping[str,Any],master:Mapping[str,Any])->dict[str,Any]:
    m=validate_master(master);v=dict(value);body={k:x for k,x in v.items() if k!="certificate_sha256"}
    if v.get("schema_version")!="2.0.0" or v.get("kind")!=CERT_KIND or v.get("architecture_version")!="4.1.0" or v.get("certificate_sha256")!=sem(body):raise ValueError("invalid V4.1 selector certificate")
    if v.get("certificate_sha256")!=m["selector_certificate_sha256"] or v.get("master_content_sha256")!=m["master_content_sha256"] or v.get("candidate_source_sha256")!=m["candidate_source_sha256"] or v.get("selector_config_sha256")!=m["selector_config_sha256"] or v.get("market_session_id")!=m["market_session_id"]:raise ValueError("V4.1 selector certificate/MASTER mismatch")
    if int(v.get("requested_size") or -1)!=MASTER_SIZE or int(v.get("selected_scope_count") or -1)!=MASTER_SIZE or v.get("current_recovery_used") is not False or list(v.get("forbidden_recovery_fields") or [])!=[] or v.get("regression_expectations_satisfied") is not True:raise ValueError("V4.1 selector certificate invariant failed")
    if v.get("selector_model_version")!=m.get("selection_model_version") or v.get("deterministic_ordering_model")!=SELECTOR_ORDERING_MODEL:raise ValueError("V4.1 selector certificate model invariant failed")
    if v.get("cohort_scope_keys_sha256")!=sem([x["research_scope_key"] for x in m["scopes"]]):raise ValueError("V4.1 selector certificate scope hash mismatch")
    ident=v.get("candidate_source_identity") or {}
    if not str(ident.get("kind") or ""):raise ValueError("V4.1 certificate source identity kind missing")
    require_hash(ident.get("snapshot_sha256"),"certificate source snapshot hash")
    coverage=v.get("fact_coverage") or {}
    if not isinstance(coverage,Mapping) or not 0<float(coverage.get("minimum_pct") or -1)<=100 or int(coverage.get("eligible_scope_count") or -1)<MASTER_SIZE:raise ValueError("V4.1 certificate fact coverage invalid")
    dedup=v.get("issuer_dedup") or {};stats=dedup.get("stats") if isinstance(dedup,Mapping) else None
    keys=("raw_quality_candidate_count","bootstrap_score_eligible_count","unique_research_scope_count","issuer_dedup_removed_rows")
    if dedup.get("enabled") is not True or not isinstance(stats,Mapping) or any(k not in stats for k in keys):raise ValueError("V4.1 certificate issuer dedup evidence invalid")
    raw,eligible,unique,removed=(int(stats[k]) for k in keys)
    if raw<eligible or eligible<unique or unique<MASTER_SIZE or removed!=eligible-unique:raise ValueError("V4.1 certificate issuer dedup stats inconsistent")
    cutoff=v.get("cutoff_diagnostics") or {}
    if not isinstance(cutoff,Mapping) or int(cutoff.get("cutoff_rank") or -1)!=MASTER_SIZE or any(k not in cutoff for k in ("cutoff_score","next_excluded_score")) or any(int(cutoff.get(k) if cutoff.get(k) is not None else -1)!=int(stats[k]) for k in keys):raise ValueError("V4.1 certificate cutoff diagnostics invalid")
    if not isinstance(v.get("regression_expectations"),list):raise ValueError("V4.1 certificate regression expectations invalid")
    return v
def validate_master_bundle(value:Mapping[str,Any])->dict[str,Any]:
    v=dict(value)
    if v.get("schema_version")!="2.0.0" or v.get("kind")!="qrgf_v41_master_core500_bundle":raise ValueError("invalid V4.1 MASTER bundle")
    source=validate_source(v.get("candidate_source") or {});master=validate_master(v.get("master") or {});certificate=validate_certificate(v.get("selector_certificate") or {},master)
    if source["source_sha256"]!=master["candidate_source_sha256"] or source["source_sha256"]!=certificate["candidate_source_sha256"] or source.get("market_session_id")!=master.get("market_session_id") or source.get("market_session_id")!=certificate.get("market_session_id") or source.get("candidate_source_identity")!=certificate.get("candidate_source_identity") or source.get("lane_counts")!=certificate.get("lane_counts") or source.get("eligible_universe_size")!=certificate.get("eligible_universe_size") or source.get("quality_candidate_union_size")!=certificate.get("quality_candidate_union_size") or int(certificate["issuer_dedup"]["stats"]["raw_quality_candidate_count"])!=len(source["candidates"]):raise ValueError("V4.1 MASTER source binding mismatch")
    return {**v,"candidate_source":source,"master":master,"selector_certificate":certificate}
def validate_master_proposal(value:Mapping[str,Any])->dict[str,Any]:
    v=dict(value);body={k:x for k,x in v.items() if k!="proposal_sha256"}
    if v.get("schema_version")!="2.0.0" or v.get("kind")!="qrgf_v41_master_core500_publish_proposal" or v.get("proposal_sha256")!=sem(body):raise ValueError("invalid V4.1 MASTER publish proposal")
    validate_master_bundle(v.get("bundle") or {});return v

def validate_passport_proposal(p:Mapping[str,Any])->dict[str,Any]:
    v=dict(p);body={k:x for k,x in v.items() if k!="proposal_sha256"}
    required={"schema_version","kind","research_scope_key","issuer_id","security_overlay","quality_policy_version","event_scan_through","summary","passport_sha256","passport_payload","proposal_sha256"}
    if set(v)!=required or v.get("schema_version")!="1.0.0" or v.get("kind")!="qrgf_v4_passport_update_proposal" or v.get("proposal_sha256")!=sem(body):raise ValueError("invalid v4 Passport proposal")
    if v.get("quality_policy_version")!=QUALITY_POLICY_VERSION:raise ValueError("v4 quality policy mismatch")
    key=f"{v.get('issuer_id')}|{v.get('security_overlay')}"
    if v.get("research_scope_key")!=key:raise ValueError("v4 proposal scope mismatch")
    payload=v.get("passport_payload") or {}; summary=v.get("summary") or {}
    if v.get("passport_sha256")!=sem(payload):raise ValueError("v4 Passport content hash mismatch")
    if payload.get("kind")!="qrgf_quality_passport" or payload.get("issuer_id")!=v.get("issuer_id") or payload.get("quality_policy_version")!=QUALITY_POLICY_VERSION:raise ValueError("v4 Passport payload identity mismatch")
    if payload.get("summary")!=summary:raise ValueError("v4 Passport summary mismatch")
    reject_sensitive(v);return v

def validate_freshness(p:Mapping[str,Any])->dict[str,Any]:
    v=dict(p);body={k:x for k,x in v.items() if k!="proposal_sha256"}
    required={"schema_version","kind","research_scope_key","issuer_id","security_overlay","passport_hash","quality_policy_version","event_scan_through","freshness_status","delta_status","proposal_sha256"}
    if set(v)!=required or v.get("schema_version")!="1.0.0" or v.get("kind")!="qrgf_v4_freshness_update_proposal" or v.get("proposal_sha256")!=sem(body):raise ValueError("invalid v4 freshness proposal")
    if v.get("quality_policy_version")!=QUALITY_POLICY_VERSION or v.get("research_scope_key")!=f"{v.get('issuer_id')}|{v.get('security_overlay')}":raise ValueError("v4 freshness identity mismatch")
    expected={"no_structural_change":"fresh","needs_refresh":"needs_refresh","invalidated":"invalidated"}.get(v.get("delta_status"))
    if expected!=v.get("freshness_status"):raise ValueError("v4 freshness status mismatch")
    return v

def validate_batch(p:Mapping[str,Any])->dict[str,Any]:
    v=dict(p);body={k:x for k,x in v.items() if k!="batch_sha256"}
    if set(v)!={"schema_version","kind","items","batch_sha256"} or v.get("schema_version")!="1.0.0" or v.get("kind")!="qrgf_v4_registry_batch" or v.get("batch_sha256")!=sem(body):raise ValueError("invalid v4 Registry batch")
    items=v.get("items") or []
    if not 1<=len(items)<=4:raise ValueError("v4 Registry batch size invalid")
    seen=set()
    for x in items:
        item=validate_passport_proposal(x) if x.get("kind")=="qrgf_v4_passport_update_proposal" else validate_freshness(x)
        if item["research_scope_key"] in seen:raise ValueError("duplicate scope in v4 Registry batch")
        seen.add(item["research_scope_key"])
    return v

def validate_entry(e:Mapping[str,Any])->dict[str,Any]:
    v=dict(e);body={k:x for k,x in v.items() if k!="entry_sha256"}
    if v.get("schema_version")!="1.0.0" or v.get("kind")!="qrgf_v4_registry_scope_entry" or v.get("entry_sha256")!=sem(body):raise ValueError("invalid v4 Registry entry")
    key=f"{v.get('issuer_id')}|{v.get('security_overlay')}"
    if v.get("research_scope_key")!=key or v.get("passport_path")!=passport_rel(key,str(v.get("passport_hash") or "")):raise ValueError("v4 Registry entry identity mismatch")
    if v.get("quality_policy_version")!=QUALITY_POLICY_VERSION:raise ValueError("v4 Registry entry policy mismatch")
    return v

def validate_receipt_record(r:Mapping[str,Any])->dict[str,Any]:
    v=dict(r); required={"schema_version","kind","proposal_sha256","research_scope_key","passport_hash","entry_sha256","published_at","producer_release_sha256","receipt_sha256"}
    body={k:x for k,x in v.items() if k!="receipt_sha256"}
    if set(v)!=required or v.get("schema_version")!="1.0.0" or v.get("kind")!="qrgf_v4_registry_write_receipt" or v.get("receipt_sha256")!=sem(body):raise ValueError("invalid v4 Registry receipt")
    return v

def validate_passport_file(root:Path,key:str,ph:str)->dict[str,Any]:
    pp=passport_path(root,key,ph)
    if not pp.exists():raise ValueError(f"v4 receipt references missing Passport: {pp}")
    payload=load(pp)
    if sem(payload)!=ph or payload.get("issuer_id")!=key.rsplit("|",1)[0] or payload.get("quality_policy_version")!=QUALITY_POLICY_VERSION:raise ValueError("invalid v4 Passport file")
    return payload

def proposal_version(p:Mapping[str,Any])->tuple[str,str]:
    s=p.get("summary") if isinstance(p.get("summary"),Mapping) else {}
    return str(p.get("event_scan_through") or ""),str(s.get("as_of") or "")
def entry_version(e:Mapping[str,Any])->tuple[str,str]:return str(e.get("event_scan_through") or ""),str(e.get("quality_as_of") or "")
def dominates(a:tuple[str,str],b:tuple[str,str])->bool:return a[0]>=b[0] and a[1]>=b[1] and a!=b

def validate_current_entry_receipt(root:Path,e:Mapping[str,Any])->dict[str,Any]:
    v=validate_entry(e);ps=str(v.get("last_proposal_sha256") or "");rp=receipt_path(root,ps)
    if not ps or not rp.exists():raise ValueError("v4 current Registry pointer is missing its receipt")
    r=validate_receipt_record(load(rp))
    if r.get("proposal_sha256")!=ps or r.get("research_scope_key")!=v.get("research_scope_key") or r.get("passport_hash")!=v.get("passport_hash") or r.get("entry_sha256")!=v.get("entry_sha256"):raise ValueError("v4 current Registry receipt mismatch")
    validate_passport_file(root,str(v["research_scope_key"]),str(v["passport_hash"]));return r

def validate_existing_receipt(root:Path,p:Mapping[str,Any])->dict[str,Any]|None:
    ps=str(p["proposal_sha256"]);rp=receipt_path(root,ps)
    if not rp.exists():return None
    r=validate_receipt_record(load(rp));key=str(p["research_scope_key"]);ph=str(p.get("passport_sha256") or p.get("passport_hash") or "")
    if r.get("proposal_sha256")!=ps or r.get("research_scope_key")!=key or r.get("passport_hash")!=ph:raise ValueError("v4 replay receipt/proposal mismatch")
    validate_passport_file(root,key,ph);ptr=pointer_path(root,key)
    if not ptr.exists():raise ValueError("v4 receipted proposal has no current Registry pointer")
    e=validate_entry(load(ptr))
    if e.get("last_proposal_sha256")==ps:
        if r.get("entry_sha256")!=e.get("entry_sha256"):raise ValueError("v4 replay receipt/current entry mismatch")
        return {"status":"already_applied","entry":e,"receipt":r}
    if p.get("kind")=="qrgf_v4_passport_update_proposal":
        if not dominates(entry_version(e),proposal_version(p)):raise ValueError("v4 receipted historical Passport proposal is not dominated by current pointer")
    else:
        if str(e.get("event_scan_through") or "")<=str(p.get("event_scan_through") or ""):raise ValueError("v4 receipted historical freshness proposal is not older than current pointer")
    return {"status":"already_applied_historical","entry":e,"receipt":r}

def make_receipt(p:Mapping[str,Any],e:Mapping[str,Any],published_at:str,release_sha:str)->dict[str,Any]:
    body={"schema_version":"1.0.0","kind":"qrgf_v4_registry_write_receipt","proposal_sha256":p["proposal_sha256"],"research_scope_key":p["research_scope_key"],"passport_hash":e["passport_hash"],"entry_sha256":e["entry_sha256"],"published_at":published_at,"producer_release_sha256":release_sha}
    return {**body,"receipt_sha256":sem(body)}

def entry_matches_passport_proposal(e:Mapping[str,Any],p:Mapping[str,Any])->bool:
    s=p.get("summary") if isinstance(p.get("summary"),Mapping) else {}
    return e.get("research_scope_key")==p.get("research_scope_key") and e.get("passport_hash")==p.get("passport_sha256") and str(e.get("event_scan_through") or "")==str(p.get("event_scan_through") or "") and str(e.get("quality_as_of") or "")==str(s.get("as_of") or "") and e.get("quality_status")==s.get("quality_status") and e.get("quality_score")==s.get("quality_score") and e.get("quality_coverage_pct")==s.get("quality_coverage_pct") and bool(e.get("quality_eligible"))==(s.get("quality_eligible") is True)

def validate_master_pointer(value:Mapping[str,Any])->dict[str,Any]:
    v=dict(value);body={k:x for k,x in v.items() if k!="pointer_sha256"}
    if v.get("schema_version")!="2.0.0" or v.get("kind")!="qrgf_v41_master_core500_pointer" or v.get("pointer_sha256")!=sem(body):raise ValueError("invalid V4.1 MASTER pointer")
    if not str(v.get("master_path") or "").startswith("data/v4/master-core500/masters/") or not str(v.get("certificate_path") or "").startswith("data/v4/master-core500/certificates/") or not str(v.get("source_path") or "").startswith("data/v4/master-core500/sources/"):raise ValueError("invalid V4.1 MASTER pointer path")
    if int(v.get("selected_scope_count") or -1)!=MASTER_SIZE:raise ValueError("invalid V4.1 MASTER pointer count")
    return v
def publish_master(root:Path,p:Mapping[str,Any],release_sha:str,published_at:str)->dict[str,Any]:
    v=validate_master_proposal(p);bundle=v["bundle"];source=bundle["candidate_source"];master=bundle["master"];certificate=bundle["selector_certificate"]
    master_sha=master["master_sha256"];source_path=f"data/v4/master-core500/sources/{source['source_sha256']}.json";master_path=f"data/v4/master-core500/masters/{master_sha}/master.json";cert_path=f"data/v4/master-core500/certificates/{certificate['certificate_sha256']}.json";latest=root/"data/v4/master-core500/latest.json"
    if latest.exists():
        old=validate_master_pointer(load(latest))
        if old.get("master_sha256")!=master_sha:raise ValueError("a V4.1 MASTER CORE500 is already authoritative; replacement is forbidden")
        if old.get("last_proposal_sha256")!=v.get("proposal_sha256"):raise ValueError("same V4.1 MASTER has conflicting proposal")
        immutable(root/source_path,source);immutable(root/master_path,master);immutable(root/cert_path,certificate);return {"status":"already_applied","pointer":old}
    immutable(root/source_path,source);immutable(root/master_path,master);immutable(root/cert_path,certificate)
    body={"schema_version":"2.0.0","kind":"qrgf_v41_master_core500_pointer","source_path":source_path,"master_path":master_path,"certificate_path":cert_path,"source_sha256":source["source_sha256"],"master_sha256":master_sha,"master_content_sha256":master["master_content_sha256"],"selector_certificate_sha256":certificate["certificate_sha256"],"market_session_id":master["market_session_id"],"selected_scope_count":MASTER_SIZE,"last_proposal_sha256":v["proposal_sha256"],"published_at":published_at,"producer_release_sha256":release_sha}
    pointer={**body,"pointer_sha256":sem(body)};write(latest,pointer);return {"status":"applied","pointer":pointer}

def publish_passport(root:Path,p:Mapping[str,Any],release_sha:str,published_at:str)->dict[str,Any]:
    v=validate_passport_proposal(p);replay=validate_existing_receipt(root,v)
    if replay is not None:return replay
    key=v["research_scope_key"];ph=v["passport_sha256"];immutable(passport_path(root,key,ph),v["passport_payload"]);s=v["summary"]
    body={"schema_version":"1.0.0","kind":"qrgf_v4_registry_scope_entry","research_scope_key":key,"issuer_id":v["issuer_id"],"security_overlay":v["security_overlay"],"passport_hash":ph,"passport_path":passport_rel(key,ph),"freshness_status":"fresh" if v.get("event_scan_through") else "needs_refresh","event_scan_through":v.get("event_scan_through"),"quality_policy_version":v["quality_policy_version"],"quality_status":s.get("quality_status"),"quality_score":s.get("quality_score"),"quality_coverage_pct":s.get("quality_coverage_pct"),"quality_eligible":s.get("quality_eligible") is True,"quality_as_of":s.get("as_of"),"last_proposal_sha256":v["proposal_sha256"],"producer_release_sha256":release_sha}
    candidate={**body,"entry_sha256":sem(body)};ptr=pointer_path(root,key);status="applied"
    if ptr.exists():
        old=validate_entry(load(ptr))
        if old.get("last_proposal_sha256")==v["proposal_sha256"]:
            if not entry_matches_passport_proposal(old,v):raise ValueError("v4 partial Passport state does not match proposal")
            entry=old;status="receipt_recovered"
        else:
            validate_current_entry_receipt(root,old);ov,nv=entry_version(old),proposal_version(v)
            if ov==nv:raise ValueError("same logical Registry version has conflicting Passport proposal")
            if dominates(ov,nv):raise ValueError("historical Passport proposal is missing its receipt; refusing to invent history")
            if not dominates(nv,ov):raise ValueError("incomparable or regressing Passport proposal version")
            entry=candidate;write(ptr,entry)
    else:entry=candidate;write(ptr,entry)
    receipt=make_receipt(v,entry,published_at,release_sha);immutable(receipt_path(root,v["proposal_sha256"]),receipt);return {"status":status,"entry":entry,"receipt":receipt}

def publish_freshness(root:Path,p:Mapping[str,Any],release_sha:str,published_at:str)->dict[str,Any]:
    v=validate_freshness(p);replay=validate_existing_receipt(root,v)
    if replay is not None:return replay
    key=v["research_scope_key"];ptr=pointer_path(root,key)
    if not ptr.exists():raise ValueError("freshness update without Registry pointer")
    old=validate_entry(load(ptr))
    if old.get("passport_hash")!=v.get("passport_hash"):raise ValueError("freshness proposal references stale Passport")
    ce,ne=str(old.get("event_scan_through") or ""),str(v.get("event_scan_through") or "");status="applied"
    if old.get("last_proposal_sha256")==v["proposal_sha256"]:
        if ce!=ne or old.get("freshness_status")!=v.get("freshness_status"):raise ValueError("v4 partial freshness state does not match proposal")
        entry=old;status="receipt_recovered"
    else:
        validate_current_entry_receipt(root,old)
        if ne<ce:raise ValueError("stale v4 freshness proposal")
        if ne==ce:raise ValueError("same freshness watermark has conflicting proposal")
        body={k:x for k,x in old.items() if k!="entry_sha256"};body.update({"freshness_status":v["freshness_status"],"event_scan_through":ne,"last_proposal_sha256":v["proposal_sha256"],"producer_release_sha256":release_sha});entry={**body,"entry_sha256":sem(body)};write(ptr,entry)
    receipt=make_receipt(v,entry,published_at,release_sha);immutable(receipt_path(root,v["proposal_sha256"]),receipt);return {"status":status,"entry":entry,"receipt":receipt}

def expand_registry_files(rdir:Path)->list[dict[str,Any]]:
    out=[]
    if rdir.is_dir():
        for path in sorted(rdir.glob("*.json")):
            v=load(path);kind=v.get("kind")
            if kind=="qrgf_v4_registry_batch":out.extend(validate_batch(v)["items"])
            elif kind=="qrgf_v4_passport_update_proposal":out.append(validate_passport_proposal(v))
            elif kind=="qrgf_v4_freshness_update_proposal":out.append(validate_freshness(v))
            else:raise ValueError("unsupported v4 Registry proposal")
    return out

def validate_pending_uniqueness(root:Path,items:list[dict[str,Any]]):
    pending={}
    for item in items:
        if receipt_path(root,str(item["proposal_sha256"])).exists():
            validate_existing_receipt(root,item);continue
        key=str(item["research_scope_key"]);ps=str(item["proposal_sha256"])
        if key in pending and pending[key]!=ps:raise ValueError(f"multiple unreceipted proposals for one scope in a single promotion run: {key}")
        pending[key]=ps

def load_master_bundle(root:Path)->tuple[dict[str,Any],dict[str,Any]]|None:
    latest=root/"data/v4/master-core500/latest.json"
    if not latest.exists():return None
    pointer=validate_master_pointer(load(latest));bundle={"schema_version":"2.0.0","kind":"qrgf_v41_master_core500_bundle","candidate_source":load(root/pointer["source_path"]),"master":load(root/pointer["master_path"]),"selector_certificate":load(root/pointer["certificate_path"])}
    bundle=validate_master_bundle(bundle);master=bundle["master"]
    if pointer.get("master_sha256")!=master["master_sha256"] or pointer.get("master_content_sha256")!=master["master_content_sha256"] or pointer.get("selector_certificate_sha256")!=master["selector_certificate_sha256"]:raise ValueError("V4.1 MASTER pointer artifact mismatch")
    return pointer,bundle
def durable_records(root:Path,master:Mapping[str,Any])->dict[str,dict[str,Any]]:
    out={}
    for scope in master["scopes"]:
        key=str(scope["research_scope_key"]);ptr=pointer_path(root,key)
        if not ptr.exists():continue
        try:
            entry=validate_entry(load(ptr));passport=validate_passport_file(root,key,str(entry["passport_hash"]));receipt=validate_current_entry_receipt(root,entry)
            status=str(entry.get("quality_status") or "")
            expected_listing="adr" if entry.get("security_overlay")=="adr" else "none";policy_ok=entry.get("quality_policy_version")==QUALITY_POLICY_VERSION;overlay_ok=passport.get("issuer_id")==entry.get("issuer_id") and str(passport.get("listing_overlay") or "none").lower()==expected_listing
            durable=str(entry.get("freshness_status") or "")=="fresh" and str(entry.get("event_scan_through") or "")>=str(master["market_session_id"]) and status in TERMINAL and policy_ok and overlay_ok
            if durable:out[key]={"quality_status":status,"event_scan_through":entry["event_scan_through"],"freshness_status":"fresh","receipt_sha256":receipt["receipt_sha256"],"passport_hash":entry["passport_hash"],"entry_sha256":entry["entry_sha256"],"durable_readback_verified":True,"policy_compatible":policy_ok,"overlay_compatible":overlay_ok}
        except Exception:continue
    return out
def snapshot(master:Mapping[str,Any],records:Mapping[str,Any])->dict[str,Any]:
    body={"schema_version":"2.0.0","kind":"qrgf_v41_registry_durable_snapshot","master_sha256":master["master_sha256"],"market_session_id":master["market_session_id"],"records":{k:records[k] for k in sorted(records)}}
    return {**body,"snapshot_sha256":sem(body)}
def validate_gate(value:Mapping[str,Any],master:Mapping[str,Any],kind:str)->dict[str,Any]:
    v=dict(value);body={k:x for k,x in v.items() if k!="gate_sha256"}
    if v.get("schema_version")!="2.0.0" or v.get("kind")!=kind or v.get("gate_sha256")!=sem(body) or v.get("master_sha256")!=master["master_sha256"] or v.get("master_content_sha256")!=master["master_content_sha256"]:raise ValueError("invalid V4.1 campaign gate")
    require_hash(v.get("registry_snapshot_sha256"),"campaign gate snapshot hash")
    if kind=="qrgf_v41_runtime_reconstruction_gate":
        if int(v.get("canary_scope_count") or -1)!=15 or int(v.get("canary_durable_count") or -1)!=15 or v.get("runtime_reconstruction_passed") is not True:raise ValueError("invalid V4.1 runtime reconstruction gate")
    if kind=="qrgf_v41_pilot_registry_gate":
        loss=v.get("registry_loss_count")
        if int(v.get("pilot_scope_count") or -1)!=50 or int(v.get("pilot_durable_count") or -1)!=50 or (int(loss) if loss is not None else -1)!=0 or v.get("reuse_check_passed") is not True or v.get("pilot_gate_passed") is not True:raise ValueError("invalid V4.1 PILOT Registry gate")
    return v
def publish_campaign_gates(root:Path,master:Mapping[str,Any])->dict[str,int]:
    proposals=root/"data/v4/campaign/proposals";applied=skipped=0
    targets={"qrgf_v41_runtime_reconstruction_gate":"runtime-reconstruction.json","qrgf_v41_pilot_registry_gate":"pilot-registry.json"}
    for path in sorted(proposals.glob("*.json")) if proposals.is_dir() else []:
        value=load(path);kind=str(value.get("kind") or "")
        if kind not in targets:raise ValueError("unsupported V4.1 campaign gate proposal")
        gate=validate_gate(value,master,kind);target=root/"data/v4/campaigns"/master["master_sha256"]/"gates"/targets[kind]
        if target.exists():
            if sem(load(target))!=sem(gate):raise ValueError("immutable V4.1 campaign gate collision")
            skipped+=1
        else:immutable(target,gate);applied+=1
    return {"applied":applied,"skipped":skipped}
def _gate_at(root:Path,master:Mapping[str,Any],name:str,kind:str)->dict[str,Any]|None:
    path=root/"data/v4/campaigns"/master["master_sha256"]/"gates"/name
    return validate_gate(load(path),master,kind) if path.exists() else None
def validate_campaign_state(value:Mapping[str,Any],master:Mapping[str,Any])->dict[str,Any]:
    v=dict(value);body={k:x for k,x in v.items() if k!="state_sha256"}
    if v.get("schema_version")!="2.0.0" or v.get("kind")!="qrgf_v41_campaign_state" or v.get("state_sha256")!=sem(body) or v.get("master_sha256")!=master["master_sha256"] or v.get("phase") not in PHASES:raise ValueError("invalid V4.1 campaign state")
    if int(v.get("master_scope_count") or -1)!=MASTER_SIZE or not 0<=int(v.get("master_durable_count") if v.get("master_durable_count") is not None else -1)<=MASTER_SIZE:raise ValueError("invalid V4.1 campaign state counts")
    if v["phase"]!="COMPLETE" and v.get("daily_broad_allowed") is not False:raise ValueError("daily broad before COMPLETE")
    if v["phase"]=="COMPLETE" and not (v.get("daily_broad_allowed") is True and int(v.get("master_durable_count"))==MASTER_SIZE and v.get("runtime_reconstruction_gate_passed") is True and v.get("pilot_registry_loss_gate_passed") is True):raise ValueError("invalid V4.1 COMPLETE state")
    if v["phase"]=="CORE500" and v.get("pilot_registry_loss_gate_passed") is not True:raise ValueError("CORE500 without PILOT gate")
    if v["phase"] in {"PILOT","CORE500","COMPLETE"} and v.get("runtime_reconstruction_gate_passed") is not True:raise ValueError("phase advanced without runtime reconstruction gate")
    return v
def rebuild_campaign_state(root:Path,release_sha:str,published_at:str)->dict[str,Any]|None:
    loaded=load_master_bundle(root)
    if loaded is None:return None
    _,bundle=loaded;master=bundle["master"];records=durable_records(root,master);snap=snapshot(master,records);snap_path=root/"data/v4/campaigns"/master["master_sha256"]/"snapshots"/f"{snap['snapshot_sha256']}.json";immutable(snap_path,snap)
    canary=list(master["canary_scope_keys"]);pilot=list(master["pilot_scope_keys"]);keys=[str(x["research_scope_key"]) for x in master["scopes"]]
    runtime=_gate_at(root,master,"runtime-reconstruction.json","qrgf_v41_runtime_reconstruction_gate");pilot_gate=_gate_at(root,master,"pilot-registry.json","qrgf_v41_pilot_registry_gate")
    canary_count=sum(k in records for k in canary);pilot_count=sum(k in records for k in pilot);total=sum(k in records for k in keys);runtime_ok=bool(runtime and canary_count==15);pilot_ok=bool(pilot_gate and pilot_count==50)
    # Counts never bypass the ordered gates: 500 durable records without the
    # immutable PILOT zero-loss/reuse attestation remain blocked in PILOT.
    phase="COMPLETE" if runtime_ok and pilot_ok and total==MASTER_SIZE else "CORE500" if runtime_ok and pilot_ok else "PILOT" if runtime_ok else "CANARY"
    phase_keys=canary if phase=="CANARY" else pilot if phase=="PILOT" else keys;by_key={str(x["research_scope_key"]):x for x in master["scopes"]};next_scopes=[{k:by_key[key].get(k) for k in ("rank","ticker","contract_id","issuer_id","security_overlay","research_scope_key","bootstrap_best_lane","bootstrap_priority_score")} for key in phase_keys if key not in records]
    input_body={"master_sha256":master["master_sha256"],"selector_certificate_sha256":master["selector_certificate_sha256"],"registry_snapshot_sha256":snap["snapshot_sha256"],"runtime_gate_sha256":runtime.get("gate_sha256") if runtime else None,"pilot_gate_sha256":pilot_gate.get("gate_sha256") if pilot_gate else None}
    body={"schema_version":"2.0.0","kind":"qrgf_v41_campaign_state","state_machine_version":"4.1.0-master500-phases-v1","master_sha256":master["master_sha256"],"master_content_sha256":master["master_content_sha256"],"selector_certificate_sha256":master["selector_certificate_sha256"],"market_session_id":master["market_session_id"],"campaign_input_sha256":sem(input_body),"registry_snapshot_sha256":snap["snapshot_sha256"],"phase":phase,"canary_scope_count":15,"canary_durable_count":canary_count,"pilot_scope_count":50,"pilot_durable_count":pilot_count,"master_scope_count":MASTER_SIZE,"master_durable_count":total,"quality_resolved_count":sum(x["quality_status"] in {"pass","conditional","rejected"} for x in records.values()),"durable_incomplete_count":sum(x["quality_status"]=="insufficient_data" for x in records.values()),"canary_durable_complete":canary_count==15,"runtime_reconstruction_gate_passed":runtime_ok,"pilot_registry_loss_gate_passed":pilot_ok,"core500_complete":total==MASTER_SIZE,"daily_broad_allowed":phase=="COMPLETE","next_scope_count":len(next_scopes),"next_scopes":next_scopes[:12],"generated_at":published_at,"producer_release_sha256":release_sha}
    state={**body,"state_sha256":sem(body)};state_path=root/"data/v4/campaigns"/master["master_sha256"]/"state.json";latest=root/"data/v4/campaign/latest.json"
    if latest.exists():
        old_pointer=load(latest);old_state=validate_campaign_state(load(root/old_pointer["state_path"]),master)
        if PHASES.index(phase)<PHASES.index(old_state["phase"]):raise ValueError("V4.1 campaign backward transition is forbidden")
        if PHASES.index(phase)>PHASES.index(old_state["phase"])+1:raise ValueError("V4.1 campaign phase skip is forbidden")
    write(state_path,state);pointer_body={"schema_version":"2.0.0","kind":"qrgf_v41_campaign_pointer","state_path":state_path.relative_to(root).as_posix(),"state_sha256":state["state_sha256"],"master_sha256":master["master_sha256"],"phase":phase,"daily_broad_allowed":state["daily_broad_allowed"],"published_at":published_at,"producer_release_sha256":release_sha};write(latest,{**pointer_body,"pointer_sha256":sem(pointer_body)})
    return state
def legacy_journal_history(root:Path)->dict[str,Any]:
    path=root/"data/v4/migrations/v410/proposal-journal-history.json"
    if not path.exists():return {"present":False,"verified":False,"records":0}
    value=load(path);body={k:x for k,x in value.items() if k!="history_sha256"}
    if value.get("schema_version")!="1.0.0" or value.get("kind")!="qrgf_v41_legacy_proposal_journal_history" or value.get("history_sha256")!=sem(body) or value.get("git_history_is_authoritative_archive") is not True or value.get("future_journal_deletion_forbidden") is not True:raise ValueError("invalid V4.1 legacy proposal journal history")
    records=value.get("records") or []
    for record in records:
        proposal_sha=require_hash(record.get("proposal_sha256"),"legacy proposal hash");receipt=receipt_path(root,proposal_sha)
        if not receipt.exists() or validate_receipt_record(load(receipt)).get("proposal_sha256")!=proposal_sha:raise ValueError("legacy proposal history receipt mismatch")
    return {"present":True,"verified":True,"records":len(records),"history_sha256":value["history_sha256"]}
def migration_report(root:Path)->dict[str,Any]:
    legacy=root/"data/v4/bootstrap/latest.json";detail={"present":legacy.exists(),"classification":"historical_validation_artifact_only"};legacy_keys=[]
    if legacy.exists():
        pointer=load(legacy);path=root/str(pointer.get("cohort_path") or "")
        if path.exists():
            cohort=load(path);body={k:x for k,x in cohort.items() if k!="cohort_sha256"};detail.update({"cohort_sha256":cohort.get("cohort_sha256"),"requested_size":cohort.get("requested_size"),"selected_scope_count":cohort.get("selected_scope_count"),"historical_hash_valid":cohort.get("cohort_sha256")==sem(body)});legacy_keys=[str(x.get("research_scope_key") or "") for x in cohort.get("scopes") or []]
    scopes=list((root/"data/v4/registry/scopes").glob("*.json")) if (root/"data/v4/registry/scopes").is_dir() else []
    history=legacy_journal_history(root)
    body={"schema_version":"2.0.0","kind":"qrgf_v41_migration_report","source_architecture_version":"4.0.6","target_architecture_version":"4.1.0","legacy_bootstrap":detail,"registry_scope_count":len(scopes),"legacy_scopes_preserved_count":len(legacy_keys),"legacy_proposal_journal_history":history,"legacy_is_not_authoritative_master":True,"registry_knowledge_outside_master_preserved":True}
    report={**body,"report_sha256":sem(body)};report_path=root/"data/v4/migrations/v410/reports"/f"{report['report_sha256']}.json";immutable(report_path,report)
    pointer_body={"schema_version":"2.0.0","kind":"qrgf_v41_migration_pointer","report_path":report_path.relative_to(root).as_posix(),"report_sha256":report["report_sha256"]};write(root/"data/v4/migrations/v410/latest.json",{**pointer_body,"pointer_sha256":sem(pointer_body)})
    return report

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--repo-root",type=Path,default=Path("."));ap.add_argument("--release-manifest",type=Path,default=Path("screening/config/v4-state-producer-release.json"));ap.add_argument("--published-at",required=True);args=ap.parse_args();root=args.repo_root.resolve();release_sha=verify_release(root,args.release_manifest)
    master_applied=master_skipped=0;cdir=root/"data/v4/master-core500/proposals"
    if cdir.is_dir():
        for path in sorted(cdir.glob("*.json")):
            res=publish_master(root,load(path),release_sha,args.published_at);master_applied+=res["status"]=="applied";master_skipped+=res["status"]=="already_applied"
    items=expand_registry_files(root/"data/v4/registry/proposals");validate_pending_uniqueness(root,items);registry_applied=registry_skipped=0
    for item in items:
        res=publish_passport(root,item,release_sha,args.published_at) if item.get("kind")=="qrgf_v4_passport_update_proposal" else publish_freshness(root,item,release_sha,args.published_at)
        registry_skipped+=str(res["status"]).startswith("already_applied");registry_applied+=res["status"] in {"applied","receipt_recovered"}
    loaded=load_master_bundle(root);gates={"applied":0,"skipped":0};state=None
    if loaded is not None:
        gates=publish_campaign_gates(root,loaded[1]["master"]);state=rebuild_campaign_state(root,release_sha,args.published_at)
    report=migration_report(root)
    print(json.dumps({"master_applied":master_applied,"master_skipped":master_skipped,"registry_items":len(items),"registry_applied":registry_applied,"registry_skipped":registry_skipped,"campaign_gates":gates,"campaign_state":state,"migration_report_sha256":report["report_sha256"],"producer_release_sha256":release_sha},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
