#!/usr/bin/env python3
"""Single-writer publisher for QRGF V4 Core500 and Structural Quality Registry."""
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

def verify_release(root:Path,path:Path)->str:
    rel=load(path); expected={"promote_v4_state.py":root/"screening/engine/promote_v4_state.py","promote-v4-state.yml":root/".github/workflows/promote-v4-state.yml"}
    if rel.get("schema_version")!="1.0.0" or rel.get("release_version")!="1.0.0" or set(rel.get("producer_hashes") or {})!=set(expected):raise ValueError("invalid v4 producer release")
    for name,p in expected.items():
        if not p.is_file() or sha(p)!=(rel["producer_hashes"] or {}).get(name):raise ValueError(f"v4 producer hash mismatch: {name}")
    return sha(path)

def validate_cohort(c:Mapping[str,Any])->dict[str,Any]:
    v=dict(c);body={k:x for k,x in v.items() if k!="cohort_sha256"}
    if v.get("schema_version")!="1.0.0" or v.get("kind")!="qrgf_v4_core500_cohort" or v.get("architecture_version")!="4.0.0":raise ValueError("invalid v4 cohort")
    if v.get("current_recovery_used") is not False or v.get("core500_is_research_bootstrap_not_whitelist") is not True:raise ValueError("invalid v4 cohort semantics")
    if v.get("cohort_sha256")!=sem(body):raise ValueError("v4 cohort self hash mismatch")
    scopes=v.get("scopes") or []
    if len(scopes)!=int(v.get("selected_scope_count") or -1) or len(scopes)>500:raise ValueError("v4 cohort count mismatch")
    keys=[str(x.get("research_scope_key") or "") for x in scopes]
    if any(not x for x in keys) or len(set(keys))!=len(keys):raise ValueError("v4 cohort scope identity invalid")
    return v

def validate_core_proposal(p:Mapping[str,Any])->dict[str,Any]:
    v=dict(p);body={k:x for k,x in v.items() if k!="proposal_sha256"}
    if v.get("schema_version")!="1.0.0" or v.get("kind")!="qrgf_v4_core500_publish_proposal" or v.get("proposal_sha256")!=sem(body):raise ValueError("invalid v4 Core500 proposal")
    validate_cohort(v.get("cohort") or {});return v

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

def publish_core(root:Path,p:Mapping[str,Any],release_sha:str)->dict[str,Any]:
    v=validate_core_proposal(p);c=v["cohort"];ch=c["cohort_sha256"];rel=f"data/v4/bootstrap/cohorts/{ch}/cohort.json";immutable(root/rel,c)
    body={"schema_version":"1.0.0","kind":"qrgf_v4_core500_pointer","cohort_path":rel,"cohort_sha256":ch,"market_session_id":c["market_session_id"],"selected_scope_count":c["selected_scope_count"],"last_proposal_sha256":v["proposal_sha256"],"producer_release_sha256":release_sha}
    pointer={**body,"pointer_sha256":sem(body)};write(root/"data/v4/bootstrap/latest.json",pointer);return pointer

def publish_passport(root:Path,p:Mapping[str,Any],release_sha:str,published_at:str)->dict[str,Any]:
    v=validate_passport_proposal(p);key=v["research_scope_key"];ph=v["passport_sha256"];immutable(passport_path(root,key,ph),v["passport_payload"]);s=v["summary"]
    body={"schema_version":"1.0.0","kind":"qrgf_v4_registry_scope_entry","research_scope_key":key,"issuer_id":v["issuer_id"],"security_overlay":v["security_overlay"],"passport_hash":ph,"passport_path":passport_rel(key,ph),"freshness_status":"fresh" if v.get("event_scan_through") else "needs_refresh","event_scan_through":v.get("event_scan_through"),"quality_policy_version":v["quality_policy_version"],"quality_status":s.get("quality_status"),"quality_score":s.get("quality_score"),"quality_coverage_pct":s.get("quality_coverage_pct"),"quality_eligible":s.get("quality_eligible") is True,"quality_as_of":s.get("as_of"),"last_proposal_sha256":v["proposal_sha256"],"producer_release_sha256":release_sha}
    entry={**body,"entry_sha256":sem(body)};ptr=pointer_path(root,key)
    if ptr.exists():
        old=load(ptr); old_key=(str(old.get("event_scan_through") or ""),str(old.get("quality_as_of") or ""),str(old.get("last_proposal_sha256") or ""));new_key=(str(entry.get("event_scan_through") or ""),str(entry.get("quality_as_of") or ""),str(entry.get("last_proposal_sha256") or ""))
        if new_key<old_key:entry=old
        elif new_key==old_key and old!=entry:raise ValueError("same-order v4 Registry update differs")
        elif new_key>old_key:write(ptr,entry)
    else:write(ptr,entry)
    rbody={"schema_version":"1.0.0","kind":"qrgf_v4_registry_write_receipt","proposal_sha256":v["proposal_sha256"],"research_scope_key":key,"passport_hash":entry["passport_hash"],"entry_sha256":entry["entry_sha256"],"published_at":published_at,"producer_release_sha256":release_sha}
    receipt={**rbody,"receipt_sha256":sem(rbody)};immutable(root/"data/v4/registry/receipts"/f"{v['proposal_sha256']}.json",receipt);return receipt

def publish_freshness(root:Path,p:Mapping[str,Any],release_sha:str,published_at:str)->dict[str,Any]:
    v=validate_freshness(p);key=v["research_scope_key"];ptr=pointer_path(root,key)
    if not ptr.exists():raise ValueError("freshness update without Registry pointer")
    old=load(ptr)
    if old.get("passport_hash")!=v.get("passport_hash") or str(v.get("event_scan_through"))<str(old.get("event_scan_through") or ""):raise ValueError("stale v4 freshness proposal")
    body={k:x for k,x in old.items() if k!="entry_sha256"};body.update({"freshness_status":v["freshness_status"],"event_scan_through":v["event_scan_through"],"last_proposal_sha256":v["proposal_sha256"],"producer_release_sha256":release_sha});entry={**body,"entry_sha256":sem(body)};write(ptr,entry)
    rbody={"schema_version":"1.0.0","kind":"qrgf_v4_registry_write_receipt","proposal_sha256":v["proposal_sha256"],"research_scope_key":key,"passport_hash":entry["passport_hash"],"entry_sha256":entry["entry_sha256"],"published_at":published_at,"producer_release_sha256":release_sha};receipt={**rbody,"receipt_sha256":sem(rbody)};immutable(root/"data/v4/registry/receipts"/f"{v['proposal_sha256']}.json",receipt);return receipt

def rebuild_progress(root:Path,release_sha:str)->dict[str,Any]|None:
    latest=root/"data/v4/bootstrap/latest.json"
    if not latest.exists():return None
    pointer=load(latest);cohort=validate_cohort(load(root/pointer["cohort_path"]));reviewed=resolved=incomplete=0;pending=[]
    for scope in cohort["scopes"]:
        key=scope["research_scope_key"];ptr=pointer_path(root,key);durable=False;status=""
        if ptr.exists():
            e=load(ptr);body={k:x for k,x in e.items() if k!="entry_sha256"}
            if e.get("entry_sha256")==sem(body) and e.get("quality_policy_version")==QUALITY_POLICY_VERSION and e.get("research_scope_key")==key and str(e.get("event_scan_through") or "")>=str(cohort["market_session_id"]):
                pp=root/str(e.get("passport_path") or "")
                if pp.exists() and sem(load(pp))==e.get("passport_hash"):
                    status=str(e.get("quality_status") or "");durable=status in TERMINAL
        if durable:
            reviewed+=1
            if status in {"pass","conditional","rejected"}:resolved+=1
            else:incomplete+=1
        else:pending.append({k:scope.get(k) for k in ("rank","ticker","contract_id","issuer_id","security_overlay","research_scope_key","bootstrap_best_lane","bootstrap_priority_score")})
    body={"schema_version":"1.0.0","kind":"qrgf_v4_core500_progress","cohort_sha256":cohort["cohort_sha256"],"market_session_id":cohort["market_session_id"],"selected_scope_count":len(cohort["scopes"]),"durable_reviewed_count":reviewed,"quality_resolved_count":resolved,"durable_incomplete_count":incomplete,"pending_count":len(pending),"next_pending_scopes":pending[:12],"campaign_complete":not pending,"producer_release_sha256":release_sha}
    progress={**body,"progress_sha256":sem(body)};write(root/"data/v4/bootstrap/progress"/f"{cohort['cohort_sha256']}.json",progress);return progress

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--repo-root",type=Path,default=Path("."));ap.add_argument("--release-manifest",type=Path,default=Path("screening/config/v4-state-producer-release.json"));ap.add_argument("--published-at",required=True);args=ap.parse_args();root=args.repo_root.resolve();release_sha=verify_release(root,args.release_manifest)
    core=0;registry=0
    cdir=root/"data/v4/bootstrap/proposals"
    if cdir.is_dir():
        for path in sorted(cdir.glob("*.json")):publish_core(root,load(path),release_sha);core+=1
    rdir=root/"data/v4/registry/proposals"
    if rdir.is_dir():
        for path in sorted(rdir.glob("*.json")):
            v=load(path);kind=v.get("kind")
            items=(validate_batch(v)["items"] if kind=="qrgf_v4_registry_batch" else [v])
            for item in items:
                if item.get("kind")=="qrgf_v4_passport_update_proposal":publish_passport(root,item,release_sha,args.published_at)
                elif item.get("kind")=="qrgf_v4_freshness_update_proposal":publish_freshness(root,item,release_sha,args.published_at)
                else:raise ValueError("unsupported v4 Registry proposal")
                registry+=1
    progress=rebuild_progress(root,release_sha)
    print(json.dumps({"core_proposals":core,"registry_items":registry,"progress":progress,"producer_release_sha256":release_sha},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
