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
    if rel.get("schema_version")!="1.0.0" or rel.get("release_version")!="1.1.0" or set(rel.get("producer_hashes") or {})!=set(expected):raise ValueError("invalid v4 producer release")
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

def validate_core_pointer(v:Mapping[str,Any])->dict[str,Any]:
    p=dict(v);body={k:x for k,x in p.items() if k!="pointer_sha256"}
    if p.get("schema_version")!="1.0.0" or p.get("kind")!="qrgf_v4_core500_pointer" or p.get("pointer_sha256")!=sem(body):raise ValueError("invalid v4 Core500 pointer")
    if not str(p.get("cohort_path") or "").startswith("data/v4/bootstrap/cohorts/"):raise ValueError("invalid v4 Core500 cohort path")
    return p

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

def publish_core(root:Path,p:Mapping[str,Any],release_sha:str)->dict[str,Any]:
    v=validate_core_proposal(p);c=v["cohort"];ch=c["cohort_sha256"];rel=f"data/v4/bootstrap/cohorts/{ch}/cohort.json";latest=root/"data/v4/bootstrap/latest.json"
    if latest.exists():
        old=validate_core_pointer(load(latest));old_cohort=validate_cohort(load(root/old["cohort_path"]))
        if old.get("cohort_sha256")==ch:
            if old.get("last_proposal_sha256")!=v.get("proposal_sha256"):raise ValueError("same Core500 cohort has conflicting proposal")
            immutable(root/rel,c);return {"status":"already_applied","pointer":old}
        progress_path=root/"data/v4/bootstrap/progress"/f"{old['cohort_sha256']}.json"
        if not progress_path.exists() or load(progress_path).get("campaign_complete") is not True:raise ValueError("cannot replace an unfinished Core500 cohort")
        if str(c.get("market_session_id") or "")<=str(old_cohort.get("market_session_id") or ""):raise ValueError("Core500 cohort cannot move backward or fork one market session")
    immutable(root/rel,c)
    body={"schema_version":"1.0.0","kind":"qrgf_v4_core500_pointer","cohort_path":rel,"cohort_sha256":ch,"market_session_id":c["market_session_id"],"selected_scope_count":c["selected_scope_count"],"last_proposal_sha256":v["proposal_sha256"],"producer_release_sha256":release_sha}
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

def rebuild_progress(root:Path,release_sha:str)->dict[str,Any]|None:
    latest=root/"data/v4/bootstrap/latest.json"
    if not latest.exists():return None
    cp=validate_core_pointer(load(latest));cohort=validate_cohort(load(root/cp["cohort_path"]));reviewed=resolved=incomplete=0;pending=[]
    for scope in cohort["scopes"]:
        key=scope["research_scope_key"];ptr=pointer_path(root,key);durable=False;status=""
        if ptr.exists():
            try:
                e=validate_entry(load(ptr))
                if str(e.get("event_scan_through") or "")>=str(cohort["market_session_id"]):
                    validate_passport_file(root,key,str(e["passport_hash"]));validate_current_entry_receipt(root,e)
                    status=str(e.get("quality_status") or "");durable=status in TERMINAL
            except Exception:
                durable=False;status=""
        if durable:
            reviewed+=1
            if status in {"pass","conditional","rejected"}:resolved+=1
            else:incomplete+=1
        else:pending.append({k:scope.get(k) for k in ("rank","ticker","contract_id","issuer_id","security_overlay","research_scope_key","bootstrap_best_lane","bootstrap_priority_score")})
    body={"schema_version":"1.0.0","kind":"qrgf_v4_core500_progress","cohort_sha256":cohort["cohort_sha256"],"market_session_id":cohort["market_session_id"],"selected_scope_count":len(cohort["scopes"]),"durable_reviewed_count":reviewed,"quality_resolved_count":resolved,"durable_incomplete_count":incomplete,"pending_count":len(pending),"next_pending_scopes":pending[:12],"campaign_complete":not pending,"producer_release_sha256":release_sha}
    progress={**body,"progress_sha256":sem(body)};write(root/"data/v4/bootstrap/progress"/f"{cohort['cohort_sha256']}.json",progress);return progress

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--repo-root",type=Path,default=Path("."));ap.add_argument("--release-manifest",type=Path,default=Path("screening/config/v4-state-producer-release.json"));ap.add_argument("--published-at",required=True);args=ap.parse_args();root=args.repo_root.resolve();release_sha=verify_release(root,args.release_manifest)
    core_applied=core_skipped=0;cdir=root/"data/v4/bootstrap/proposals"
    if cdir.is_dir():
        for path in sorted(cdir.glob("*.json")):
            res=publish_core(root,load(path),release_sha);core_applied+=res["status"]=="applied";core_skipped+=res["status"]=="already_applied"
    items=expand_registry_files(root/"data/v4/registry/proposals");validate_pending_uniqueness(root,items);registry_applied=registry_skipped=0
    for item in items:
        res=publish_passport(root,item,release_sha,args.published_at) if item.get("kind")=="qrgf_v4_passport_update_proposal" else publish_freshness(root,item,release_sha,args.published_at)
        registry_skipped+=str(res["status"]).startswith("already_applied");registry_applied+=res["status"] in {"applied","receipt_recovered"}
    progress=rebuild_progress(root,release_sha)
    print(json.dumps({"core_applied":core_applied,"core_skipped":core_skipped,"registry_items":len(items),"registry_applied":registry_applied,"registry_skipped":registry_skipped,"progress":progress,"producer_release_sha256":release_sha},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
