#!/usr/bin/env python3
"""Single-writer promotion of research-scope keyed v3.1 Structural Quality Passports."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from typing import Any, Mapping

SENSITIVE={"account","account_id","account_number","balances","positions","allocation","available_funds","buying_power","cash_balance","quote","bid","ask","raw_connector_response","licensed_payload"}
def canonical(v:Any)->bytes:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def sem(v:Any)->str:return hashlib.sha256(canonical(v)).hexdigest()
def sha(path:Path)->str:return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path:Path)->dict[str,Any]:
    v=json.loads(path.read_text(encoding="utf-8-sig"));
    if not isinstance(v,dict):raise ValueError(f"{path} must contain an object")
    return v
def reject_sensitive(v:Any,path:str=""):
    if isinstance(v,Mapping):
        for k,x in v.items():
            dotted=f"{path}.{k}" if path else str(k)
            if str(k).lower() in SENSITIVE:raise ValueError(f"sensitive field in v3.1 Registry proposal: {dotted}")
            reject_sensitive(x,dotted)
    elif isinstance(v,list):
        for i,x in enumerate(v):reject_sensitive(x,f"{path}[{i}]")
def verify_release(path:Path,root:Path)->tuple[dict[str,Any],str]:
    rel=load(path); mapping={"promote_v31_registry.py":root/"screening/engine/promote_v31_registry.py","promote-v31-registry.yml":root/".github/workflows/promote-v31-registry.yml"}; hashes=rel.get("producer_hashes") or {}
    if rel.get("schema_version")!="1.0.0" or rel.get("release_version")!="1.0.0" or set(hashes)!=set(mapping):raise ValueError("invalid v3.1 Registry producer release")
    for name,p in mapping.items():
        if not p.is_file() or sha(p)!=hashes[name]:raise ValueError(f"v3.1 Registry producer hash mismatch: {name}")
    return rel,sha(path)
def validate_proposal(p:Mapping[str,Any])->dict[str,Any]:
    v=dict(p); body={k:x for k,x in v.items() if k!="proposal_sha256"}
    if v.get("schema_version")!="1.0.0" or v.get("kind")!="qrgf_v31_passport_update_proposal" or v.get("proposal_sha256")!=sem(body):raise ValueError("invalid v3.1 Passport proposal")
    issuer=str(v.get("issuer_id") or ""); overlay=str(v.get("security_overlay") or ""); key=str(v.get("research_scope_key") or "")
    if not issuer or key!=f"{issuer}|{overlay}":raise ValueError("v3.1 Passport scope identity mismatch")
    if v.get("passport_sha256")!=sem(v.get("passport_payload") or {}):raise ValueError("v3.1 Passport content hash mismatch")
    reject_sensitive(v); return v
def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--repo-root",type=Path,default=Path("."));ap.add_argument("--proposals-dir",type=Path,default=Path("data/v31/registry/proposals"));ap.add_argument("--release-manifest",type=Path,default=Path("screening/config/v31-registry-producer-release.json"));args=ap.parse_args();root=args.repo_root.resolve();release,release_sha=verify_release(args.release_manifest,root)
    proposals=[]
    if args.proposals_dir.is_dir():
        for path in sorted(args.proposals_dir.glob("*.json")):proposals.append(validate_proposal(load(path)))
    proposals.sort(key=lambda p:(str(p.get("event_scan_through") or ""),str((p.get("summary") or {}).get("as_of") or ""),str(p.get("proposal_sha256") or "")))
    applied=0
    for p in proposals:
        key=str(p["research_scope_key"]); digest=hashlib.sha256(key.encode()).hexdigest(); pointer=root/"data/v31/registry/scopes"/f"{digest}.json"; existing=load(pointer) if pointer.is_file() else None
        old_order=(str((existing or {}).get("event_scan_through") or ""),str((existing or {}).get("quality_as_of") or ""),str((existing or {}).get("last_proposal_sha256") or "")); new_order=(str(p.get("event_scan_through") or ""),str((p.get("summary") or {}).get("as_of") or ""),str(p.get("proposal_sha256") or ""))
        if existing and new_order<=old_order:continue
        passport_payload=dict(p["passport_payload"]); passport_hash=str(p["passport_sha256"]); passport_dir=root/"data/v31/passports"/digest; passport_dir.mkdir(parents=True,exist_ok=True); passport_path=passport_dir/f"{passport_hash}.json"
        if passport_path.exists() and sem(load(passport_path))!=passport_hash:raise ValueError("v3.1 immutable Passport collision")
        if not passport_path.exists():passport_path.write_text(json.dumps(passport_payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        summary=dict(p["summary"]); body={"schema_version":"1.0.0","kind":"qrgf_v31_registry_scope_entry","research_scope_key":key,"issuer_id":p["issuer_id"],"security_overlay":p["security_overlay"],"passport_hash":passport_hash,"passport_path":passport_path.relative_to(root).as_posix(),"freshness_status":"fresh" if p.get("event_scan_through") else "needs_refresh","event_scan_through":p.get("event_scan_through"),"quality_policy_version":p.get("quality_policy_version"),"quality_status":summary.get("quality_status"),"quality_score":summary.get("quality_score"),"quality_coverage_pct":summary.get("quality_coverage_pct"),"quality_eligible":summary.get("quality_eligible") is True,"quality_as_of":summary.get("as_of"),"quality":summary,"last_proposal_sha256":p["proposal_sha256"],"registry_producer_release_sha256":release_sha}
        entry={**body,"entry_sha256":sem(body)}; pointer.parent.mkdir(parents=True,exist_ok=True); pointer.write_text(json.dumps(entry,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8");applied+=1
    print(json.dumps({"applied":applied,"producer_release_sha256":release_sha},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
