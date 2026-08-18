#!/usr/bin/env python3
"""Independent verifier for a committed QRGF v3.1 frontier snapshot."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

NUMERIC_FIELDS=(
    "current_price","reference_52w_high","market_cap","avg_dollar_volume","return_1m_pct","return_3m_pct","return_6m_pct","return_12m_pct",
    "drawdown_pct","historical_volatility_pct","trading_history_days","l2_setup_score","l2_confidence_pct","l2_quality_prior_score","l2_room_to_target_score",
    "setup_prior_growth","setup_pullback_geometry","setup_liquidity","setup_data_completeness",
)
CLASS_SUFFIXES=(
    re.compile(r"\s*[-–—]\s*class\s+[a-z0-9-]+\s+(?:common\s+|capital\s+)?(?:stock|shares?)\s*$",re.I),
    re.compile(r"\s*[-–—]\s*class\s+[a-z0-9-]+\s+ordinary\s+shares?\s*$",re.I),
    re.compile(r"\s+class\s+[a-z0-9-]+\s+(?:common\s+|capital\s+)?(?:stock|shares?)\s*$",re.I),
)

def canonical(value: Any) -> bytes: return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
def semantic_hash(value: Any) -> str: return hashlib.sha256(canonical(value)).hexdigest()
def sha256_bytes(value: bytes) -> str: return hashlib.sha256(value).hexdigest()
def load_json(path: Path) -> dict[str,Any]:
    value=json.loads(path.read_text(encoding="utf-8-sig"));
    if not isinstance(value,dict): raise ValueError(f"{path} must be an object")
    return value

def num(value: Any) -> float|None:
    if value in (None,"") or isinstance(value,bool): return None
    try: parsed=float(value)
    except (TypeError,ValueError): return None
    return parsed if parsed==parsed else None

def clamp(value: float) -> float: return max(0.0,min(100.0,float(value)))
def git_show(repo_root: Path, commit: str, path: str) -> bytes:
    result=subprocess.run(["git","show",f"{commit}:{path}"],cwd=repo_root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if result.returncode!=0: raise ValueError(f"cannot read {path} from source Radar commit {commit}: {result.stderr.decode(errors='replace')[:200]}")
    return result.stdout

def parse_csv(data: bytes) -> list[dict[str,Any]]:
    out=[]
    for raw in csv.DictReader(io.StringIO(data.decode("utf-8-sig"))):
        row=dict(raw)
        for field in NUMERIC_FIELDS:
            if field in row and row[field] not in (None,""):
                parsed=num(row[field]); row[field]=parsed if parsed is not None else row[field]
        out.append(row)
    return out

def clean_company(value: Any) -> str:
    text=str(value or "").strip()
    for pattern in CLASS_SUFFIXES:
        stripped=pattern.sub("",text).strip(" -–—")
        if stripped!=text: return re.sub(r"[^A-Z0-9]+"," ",stripped.upper()).strip()
    return ""

def derive_issuer(row: Mapping[str,Any]) -> str:
    explicit=str(row.get("issuer_id") or "").strip()
    if explicit:
        return explicit if explicit.startswith(("ISSUER:","CIK:","NAMECLASS:","SECURITY:")) else f"ISSUER:{explicit.upper()}"
    cik=str(row.get("issuer_cik") or row.get("cik") or "").strip().lstrip("0")
    if cik.isdigit(): return f"CIK:{int(cik):010d}"
    company=clean_company(row.get("company") or row.get("company_name"))
    if company: return f"NAMECLASS:{company}"
    return f"SECURITY:{str(row.get('ticker') or '').upper()}:{str(row.get('contract_id') or '')}"
def overlay(value: Any) -> str:
    text=str(value or "").lower(); return "adr" if text=="adr" else "etf" if text=="etf" else "common_equity"

def verify(args: argparse.Namespace) -> dict[str,Any]:
    root=args.repo_root.resolve(); snapshot=(root/args.snapshot_dir).resolve(); cert=load_json(snapshot/"market-certificate.json"); manifest=load_json(snapshot/"frontier-manifest.json"); model=load_json(root/"screening/config/v31-frontier-model.json"); release=load_json(root/"screening/config/v31-frontier-producer-release.json")
    cert_body={k:v for k,v in cert.items() if k!="certificate_sha256"};
    if cert.get("certificate_sha256")!=semantic_hash(cert_body): raise ValueError("certificate self hash mismatch")
    man_body={k:v for k,v in manifest.items() if k!="manifest_semantic_sha256"};
    if manifest.get("manifest_semantic_sha256")!=semantic_hash(man_body): raise ValueError("frontier manifest self hash mismatch")
    model_sha=semantic_hash(model)
    if manifest.get("frontier_model_sha256")!=model_sha or cert.get("frontier",{}).get("frontier_model_sha256")!=model_sha or release.get("frontier_model_sha256")!=model_sha: raise ValueError("frontier model lineage mismatch")
    release_sha=sha256_bytes((root/"screening/config/v31-frontier-producer-release.json").read_bytes())
    if cert.get("frontier_producer_release",{}).get("manifest_sha256")!=release_sha: raise ValueError("frontier producer release hash mismatch")
    radar_commit=str(cert.get("source_radar",{}).get("publication_commit_sha") or "")
    if len(radar_commit)!=40: raise ValueError("source Radar commit invalid")
    system=json.loads(git_show(root,radar_commit,"data/v3/latest.json")); sys_body={k:v for k,v in system.items() if k!="system_snapshot_sha256"}
    if system.get("system_snapshot_sha256")!=semantic_hash(sys_body) or system.get("system_snapshot_sha256")!=cert["source_radar"]["system_snapshot_sha256"]: raise ValueError("source Radar system hash mismatch")
    radar_release=git_show(root,radar_commit,"screening/config/v3-producer-release.json")
    if sha256_bytes(radar_release)!=release.get("source_radar_producer_release_sha256") or sha256_bytes(radar_release)!=cert["source_radar"]["producer_release_sha256"]: raise ValueError("source Radar release hash mismatch")
    radar_manifest=json.loads(git_show(root,radar_commit,str(system["radar_manifest_path"]))); radar_body={k:v for k,v in radar_manifest.items() if k!="manifest_semantic_sha256"}
    if radar_manifest.get("manifest_semantic_sha256")!=semantic_hash(radar_body) or radar_manifest.get("manifest_semantic_sha256")!=cert["source_radar"]["manifest_semantic_sha256"]: raise ValueError("source Radar manifest hash mismatch")
    rows=[]
    base=str(Path(str(system["radar_manifest_path"])).parent.as_posix())
    for decl in radar_manifest.get("pages") or []:
        data=git_show(root,radar_commit,f"{base}/{decl['name']}")
        if sha256_bytes(data)!=decl.get("sha256"): raise ValueError(f"source Radar page hash mismatch: {decl['name']}")
        parsed=parse_csv(data)
        if len(parsed)!=int(decl.get("rows") or -1): raise ValueError(f"source Radar page count mismatch: {decl['name']}")
        rows.extend(parsed)
    if len(rows)!=int(radar_manifest.get("rows") or -1): raise ValueError("source Radar row-count proof mismatch")
    declared_rows_hash=str(radar_manifest.get("rows_semantic_sha256") or "")
    if len(declared_rows_hash)!=64 or any(ch not in "0123456789abcdef" for ch in declared_rows_hash): raise ValueError("source Radar declared rows semantic hash invalid")
    if declared_rows_hash!=cert["source_radar"]["rows_semantic_sha256"]: raise ValueError("source Radar declared semantic lineage mismatch")
    page_proof=[{"name":str(decl["name"]),"rows":int(decl["rows"]),"sha256":str(decl["sha256"])} for decl in radar_manifest.get("pages") or []]
    if semantic_hash(page_proof)!=cert["source_radar"].get("page_set_sha256"): raise ValueError("source Radar ordered page-set root mismatch")
    if len(rows)!=int(cert["source_radar"]["universe_rows"]) or len(rows)!=int(cert["source_radar"]["market_scanned_rows"]): raise ValueError("certificate full-market count mismatch")
    weights=model["weights"]; quality_max=float(model["quality_upper_bound"]); market_max=float(model["missing_market_component_upper_bound"]); decimals=int(model.get("score_round_decimals") or 4)
    def upper(row: Mapping[str,Any]) -> float:
        setup=num(row.get("l2_setup_score")); conf=num(row.get("l2_confidence_pct")); setup=market_max if setup is None else setup; conf=market_max if conf is None else conf
        return round((quality_max*float(weights["structural_quality"])+clamp(setup)*float(weights["recovery_setup"])+clamp(conf)*float(weights["evidence_confidence"]))/100.0,decimals)
    expected_groups: dict[str,list[tuple[str,str,float]]]={}
    for row in rows:
        if str(row.get("l2_status") or "").lower()=="rejected": continue
        issuer=derive_issuer(row); key=f"{issuer}|{overlay(row.get('security_type'))}"; expected_groups.setdefault(key,[]).append((str(row.get("ticker") or "").upper(),str(row.get("contract_id") or ""),upper(row)))
    observed_scopes=[]; expected_rank=1; previous_upper=None; security_count=0
    for expected_page_index,decl in enumerate(manifest.get("pages") or [],1):
        expected_name=f"page-{expected_page_index:04d}.json"
        expected_path=(snapshot/"pages"/expected_name).relative_to(root).as_posix()
        if int(decl.get("page_index") or -1)!=expected_page_index or str(decl.get("name") or "")!=expected_name or str(decl.get("path") or "")!=expected_path:
            raise ValueError("frontier page declaration path/index mismatch")
        path=root/expected_path; data=path.read_bytes()
        if sha256_bytes(data)!=decl.get("sha256"): raise ValueError(f"frontier page byte hash mismatch: {decl['name']}")
        page=json.loads(data); page_body={k:v for k,v in page.items() if k!="page_semantic_sha256"}
        if page.get("page_semantic_sha256")!=semantic_hash(page_body) or page.get("page_semantic_sha256")!=decl.get("page_semantic_sha256"): raise ValueError(f"frontier page semantic hash mismatch: {decl['name']}")
        if int(page.get("scope_start_rank") or -1)!=expected_rank: raise ValueError("frontier rank gap")
        for scope in page.get("scopes") or []:
            if int(scope.get("frontier_rank") or -1)!=expected_rank: raise ValueError("frontier scope rank mismatch")
            key=str(scope.get("research_scope_key") or ""); issuer=str(scope.get("issuer_id") or ""); ov=str(scope.get("security_overlay") or "")
            if key!=f"{issuer}|{ov}": raise ValueError("frontier scope identity mismatch")
            securities=scope.get("securities") or []
            local=[]
            for sec in securities:
                if derive_issuer(sec)!=issuer or f"{issuer}|{overlay(sec.get('security_type'))}"!=key: raise ValueError("frontier security identity mismatch")
                u=upper(sec)
                if abs(float(sec.get("security_upper_bound"))-u)>1e-6: raise ValueError("frontier security upper-bound mismatch")
                local.append((str(sec.get("ticker") or "").upper(),str(sec.get("contract_id") or ""),u)); security_count+=1
            if not local or abs(float(scope.get("scope_upper_bound"))-max(v[2] for v in local))>1e-6: raise ValueError("frontier scope upper-bound mismatch")
            if previous_upper is not None and float(scope["scope_upper_bound"])>previous_upper+1e-9: raise ValueError("frontier global order is not non-increasing")
            previous_upper=float(scope["scope_upper_bound"]); expected_rank+=1; observed_scopes.append(scope)
            exp=sorted(expected_groups.get(key) or [])
            if sorted(local)!=exp: raise ValueError(f"frontier scope does not exactly cover source Radar securities: {key}")
        if int(page.get("scope_end_rank") or -1)!=expected_rank-1: raise ValueError("frontier page end-rank mismatch")
    if set(expected_groups)!={str(scope["research_scope_key"]) for scope in observed_scopes}: raise ValueError("frontier scope set is not complete")
    if len(observed_scopes)!=int(manifest.get("scope_count") or -1) or security_count!=int(manifest.get("security_count") or -1): raise ValueError("frontier manifest counts mismatch")
    if semantic_hash(observed_scopes)!=manifest.get("frontier_semantic_sha256") or manifest.get("frontier_semantic_sha256")!=cert["frontier"]["frontier_semantic_sha256"]: raise ValueError("frontier semantic root mismatch")
    if manifest.get("manifest_semantic_sha256")!=cert["frontier"]["manifest_semantic_sha256"]: raise ValueError("certificate/manifest binding mismatch")
    return {"verified":True,"snapshot_id":cert["snapshot_id"],"market_session_id":cert["market_session_id"],"universe_rows":len(rows),"frontier_scope_count":len(observed_scopes),"frontier_security_count":security_count,"source_radar_commit":radar_commit,"certificate_sha256":cert["certificate_sha256"],"frontier_manifest_sha256":manifest["manifest_semantic_sha256"]}

def main() -> int:
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--repo-root",type=Path,default=Path(".")); p.add_argument("--snapshot-dir",required=True); args=p.parse_args(); print(json.dumps(verify(args),ensure_ascii=False,indent=2,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
