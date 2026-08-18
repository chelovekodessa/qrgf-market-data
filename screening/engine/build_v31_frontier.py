#!/usr/bin/env python3
"""Build an immutable, compact QRGF v3.1 research frontier from a fully verified v3 Radar.

The output is transport-oriented only: every non-rejected Radar security remains represented,
unknown market components receive mathematically safe upper bounds, and no fixed Top-N cutoff
is introduced before Structural Quality research.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
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


def canonical(value: Any) -> bytes:
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")

def semantic_hash(value: Any) -> str: return hashlib.sha256(canonical(value)).hexdigest()
def sha256_bytes(value: bytes) -> str: return hashlib.sha256(value).hexdigest()
def sha256_file(path: Path) -> str: return sha256_bytes(path.read_bytes())
def load_json(path: Path) -> dict[str,Any]:
    value=json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value,dict): raise ValueError(f"{path} must contain an object")
    return value

def num(value: Any) -> float|None:
    if value in (None,"") or isinstance(value,bool): return None
    try: parsed=float(value)
    except (TypeError,ValueError): return None
    return parsed if parsed==parsed else None

def clamp(value: float) -> float: return max(0.0,min(100.0,float(value)))

def parse_page(data: bytes) -> list[dict[str,Any]]:
    rows=[]
    for raw in csv.DictReader(io.StringIO(data.decode("utf-8-sig"))):
        row=dict(raw)
        for field in NUMERIC_FIELDS:
            if field in row and row[field] not in (None,""):
                parsed=num(row[field]); row[field]=parsed if parsed is not None else row[field]
        rows.append(row)
    return rows

def clean_company(value: Any) -> str:
    text=str(value or "").strip()
    for pattern in CLASS_SUFFIXES:
        stripped=pattern.sub("",text).strip(" -–—")
        if stripped!=text: return re.sub(r"[^A-Z0-9]+"," ",stripped.upper()).strip()
    return ""

def issuer_id(row: Mapping[str,Any]) -> str:
    explicit=str(row.get("issuer_id") or "").strip()
    if explicit:
        return explicit if explicit.startswith(("ISSUER:","CIK:","NAMECLASS:","SECURITY:")) else f"ISSUER:{explicit.upper()}"
    cik=str(row.get("issuer_cik") or row.get("cik") or "").strip().lstrip("0")
    if cik.isdigit(): return f"CIK:{int(cik):010d}"
    company=clean_company(row.get("company") or row.get("company_name"))
    if company: return f"NAMECLASS:{company}"
    ticker=str(row.get("ticker") or "").upper(); contract=str(row.get("contract_id") or "")
    if not ticker or not contract: raise ValueError("Radar row identity missing")
    return f"SECURITY:{ticker}:{contract}"

def overlay(security_type: Any) -> str:
    value=str(security_type or "").lower()
    return "adr" if value=="adr" else "etf" if value=="etf" else "common_equity"

def verify_release(path: Path, repo_root: Path) -> tuple[dict[str,Any],str]:
    release=load_json(path)
    if release.get("schema_version")!="1.0.0" or release.get("release_version")!="1.0.0": raise ValueError("unexpected v3.1 frontier producer release")
    mapping={
        "build_v31_frontier.py":repo_root/"screening/engine/build_v31_frontier.py",
        "verify_v31_publication.py":repo_root/"screening/engine/verify_v31_publication.py",
        "v31-frontier-model.json":repo_root/"screening/config/v31-frontier-model.json",
        "update-v31-frontier.yml":repo_root/".github/workflows/update-v31-frontier.yml",
    }
    hashes=release.get("producer_hashes") or {}
    if set(hashes)!=set(mapping): raise ValueError("v3.1 frontier producer release file set mismatch")
    for name,file in mapping.items():
        if not file.is_file() or sha256_file(file)!=hashes[name]: raise ValueError(f"v3.1 producer hash mismatch: {name}")
    return release,sha256_file(path)

def load_verified_radar(repo_root: Path, expected_radar_release_sha: str) -> tuple[dict[str,Any],dict[str,Any],list[dict[str,Any]],dict[str,int]]:
    system=load_json(repo_root/"data/v3/latest.json")
    if system.get("schema_version")!="1.0.0" or system.get("kind")!="qrgf_v3_system_snapshot" or system.get("complete") is not True: raise ValueError("invalid source v3 system snapshot")
    body={k:v for k,v in system.items() if k!="system_snapshot_sha256"}
    if system.get("system_snapshot_sha256")!=semantic_hash(body): raise ValueError("source v3 system self hash mismatch")
    if system.get("producer_release_sha256")!=expected_radar_release_sha: raise ValueError("source v3 Radar producer release is not approved")
    release_path=repo_root/"screening/config/v3-producer-release.json"
    if sha256_file(release_path)!=expected_radar_release_sha: raise ValueError("source v3 Radar producer release file hash mismatch")
    manifest=load_json(repo_root/str(system["radar_manifest_path"]))
    man_body={k:v for k,v in manifest.items() if k!="manifest_semantic_sha256"}
    if manifest.get("manifest_semantic_sha256")!=semantic_hash(man_body) or manifest.get("manifest_semantic_sha256")!=system.get("radar_manifest_sha256"): raise ValueError("source v3 Radar manifest hash mismatch")
    if manifest.get("snapshot_id")!=system.get("snapshot_id") or manifest.get("market_session_id")!=system.get("market_session_id"): raise ValueError("source v3 Radar identity mismatch")
    page_root=(repo_root/str(system["radar_manifest_path"])).parent
    rows=[]; legacy_rankable=0; unified_rankable=0; nonrejected=0; rejected=0
    for decl in manifest.get("pages") or []:
        path=page_root/str(decl["name"]); data=path.read_bytes()
        if sha256_bytes(data)!=decl.get("sha256"): raise ValueError(f"source v3 Radar page hash mismatch: {decl.get('name')}")
        parsed=parse_page(data)
        if len(parsed)!=int(decl.get("rows") or -1): raise ValueError(f"source v3 Radar page row count mismatch: {decl.get('name')}")
        for row in parsed:
            status=str(row.get("l2_status") or "").lower(); setup=num(row.get("l2_setup_score")); confidence=num(row.get("l2_confidence_pct"))
            if status=="rejected": rejected+=1
            else:
                nonrejected+=1
                if setup is not None: legacy_rankable+=1
                if setup is not None and confidence is not None: unified_rankable+=1
        rows.extend(parsed)
    if len(rows)!=int(manifest.get("rows") or -1): raise ValueError("source v3 Radar total rows mismatch")
    if semantic_hash(rows)!=manifest.get("rows_semantic_sha256"): raise ValueError("source v3 Radar rows semantic hash mismatch")
    coverage=system.get("coverage") or {}
    if int(coverage.get("universe_rows") or -1)!=len(rows) or int(coverage.get("market_scanned_rows") or -1)!=len(rows): raise ValueError("source v3 system coverage mismatch")
    if int(coverage.get("rankable_market_rows") or -1)!=legacy_rankable: raise ValueError("source v3 legacy rankable count mismatch")
    return system,manifest,rows,{"legacy_rankable":legacy_rankable,"unified_rankable":unified_rankable,"nonrejected":nonrejected,"rejected":rejected,"market_incomplete_nonrejected":nonrejected-unified_rankable}

def _repo_path(root: Path, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (root/value).resolve()

def build(args: argparse.Namespace) -> dict[str,Any]:
    root=args.repo_root.resolve()
    release_path=_repo_path(root,args.release_manifest); model_path=_repo_path(root,args.frontier_model); output_root=_repo_path(root,args.output_root)
    release,release_sha=verify_release(release_path,root); model=load_json(model_path); model_sha=semantic_hash(model)
    if release.get("frontier_model_sha256")!=model_sha: raise ValueError("frontier producer release/model hash mismatch")
    expected_radar=str(release.get("source_radar_producer_release_sha256") or "")
    system,radar_manifest,radar,counts=load_verified_radar(root,expected_radar)
    weights=model["weights"]; quality_max=float(model["quality_upper_bound"]); market_max=float(model["missing_market_component_upper_bound"]); decimals=int(model.get("score_round_decimals") or 4)
    def score(setup: float,confidence: float) -> float:
        return round((quality_max*float(weights["structural_quality"])+clamp(setup)*float(weights["recovery_setup"])+clamp(confidence)*float(weights["evidence_confidence"]))/100.0,decimals)
    groups: dict[str,list[dict[str,Any]]]={}
    for raw in radar:
        if str(raw.get("l2_status") or "").lower()=="rejected": continue
        issuer=issuer_id(raw); ov=overlay(raw.get("security_type")); key=f"{issuer}|{ov}"
        setup=num(raw.get("l2_setup_score")); confidence=num(raw.get("l2_confidence_pct")); upper=score(setup if setup is not None else market_max,confidence if confidence is not None else market_max)
        seed={
            "ticker":str(raw.get("ticker") or "").upper(),"company":raw.get("company"),"contract_id":str(raw.get("contract_id") or ""),"issuer_id":issuer,"issuer_cik":raw.get("issuer_cik"),"security_type":raw.get("security_type"),"instrument_status":raw.get("instrument_status"),"exchange":raw.get("exchange"),"sector":raw.get("sector"),"industry":raw.get("industry"),
            "current_price":num(raw.get("current_price")),"reference_52w_high":num(raw.get("reference_52w_high")),"market_cap":num(raw.get("market_cap")),"avg_dollar_volume":num(raw.get("avg_dollar_volume")),"return_1m_pct":num(raw.get("return_1m_pct")),"return_3m_pct":num(raw.get("return_3m_pct")),"return_6m_pct":num(raw.get("return_6m_pct")),"return_12m_pct":num(raw.get("return_12m_pct")),"drawdown_pct":num(raw.get("drawdown_pct")),"historical_volatility_pct":num(raw.get("historical_volatility_pct")),"trading_history_days":num(raw.get("trading_history_days")),"momentum_history_status":raw.get("momentum_history_status"),"data_integrity_status":raw.get("data_integrity_status"),"as_of":raw.get("as_of"),
            "l2_status":raw.get("l2_status"),"l2_setup_score":setup,"l2_confidence_pct":confidence,"l2_quality_prior_score":num(raw.get("l2_quality_prior_score")),"l2_room_to_target_score":num(raw.get("l2_room_to_target_score")),"setup_prior_growth":num(raw.get("setup_prior_growth")),"setup_pullback_geometry":num(raw.get("setup_pullback_geometry")),"setup_liquidity":num(raw.get("setup_liquidity")),"setup_data_completeness":num(raw.get("setup_data_completeness")),"setup_model_version":raw.get("setup_model_version"),"l2_rules_hash":raw.get("l2_rules_hash"),"security_upper_bound":upper,
        }
        if not seed["ticker"] or not seed["contract_id"]: raise ValueError("frontier security identity missing")
        groups.setdefault(key,[]).append(seed)
    scopes=[]
    for key,members in groups.items():
        members.sort(key=lambda r:(-float(r["security_upper_bound"]),str(r["ticker"]),str(r["contract_id"])))
        issuer=members[0]["issuer_id"]; ov=key.rsplit("|",1)[-1]
        scopes.append({"research_scope_key":key,"issuer_id":issuer,"selection_entity_id":issuer,"security_overlay":ov,"scope_upper_bound":max(float(row["security_upper_bound"]) for row in members),"market_data_incomplete":any(row.get("l2_setup_score") is None or row.get("l2_confidence_pct") is None for row in members),"securities":members})
    scopes.sort(key=lambda r:(-float(r["scope_upper_bound"]),str(r["research_scope_key"])))
    for rank,scope in enumerate(scopes,1): scope["frontier_rank"]=rank
    frontier_semantic=semantic_hash(scopes)
    seed={"source_radar_snapshot_content_sha256":system["snapshot_content_sha256"],"source_radar_rows_semantic_sha256":radar_manifest["rows_semantic_sha256"],"source_radar_publication_commit_sha":args.radar_publication_commit_sha,"frontier_semantic_sha256":frontier_semantic,"frontier_model_sha256":model_sha,"frontier_producer_release_sha256":release_sha,"created_at":args.created_at}
    snapshot_content=semantic_hash(seed); snapshot_id=snapshot_content[:24]
    snapshot_dir=output_root/"snapshots"/snapshot_id
    if snapshot_dir.exists(): raise ValueError("immutable v3.1 snapshot path already exists")
    pages_dir=snapshot_dir/"pages"; pages_dir.mkdir(parents=True,exist_ok=True)
    page_decls=[]; security_count=0
    page_size=int(args.page_size)
    for index,start in enumerate(range(0,len(scopes),page_size),1):
        chunk=scopes[start:start+page_size]; page_body={"schema_version":"1.0.0","kind":"qrgf_v31_frontier_page","snapshot_id":snapshot_id,"market_session_id":system["market_session_id"],"page_index":index,"scope_start_rank":int(chunk[0]["frontier_rank"]),"scope_end_rank":int(chunk[-1]["frontier_rank"]),"scopes":chunk}; page={**page_body,"page_semantic_sha256":semantic_hash(page_body)}
        path=pages_dir/f"page-{index:04d}.json"; path.write_text(json.dumps(page,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        sc=sum(len(scope["securities"]) for scope in chunk); security_count+=sc
        page_decls.append({"page_index":index,"name":path.name,"path":path.relative_to(root).as_posix(),"scope_start_rank":page["scope_start_rank"],"scope_end_rank":page["scope_end_rank"],"scope_count":len(chunk),"security_count":sc,"max_scope_upper_bound":chunk[0]["scope_upper_bound"],"min_scope_upper_bound":chunk[-1]["scope_upper_bound"],"sha256":sha256_file(path),"page_semantic_sha256":page["page_semantic_sha256"]})
    manifest_body={"schema_version":"1.0.0","kind":"qrgf_v31_frontier_manifest","snapshot_id":snapshot_id,"snapshot_content_sha256":snapshot_content,"market_session_id":system["market_session_id"],"created_at":args.created_at,"frontier_model_sha256":model_sha,"scope_count":len(scopes),"security_count":security_count,"page_size":page_size,"pages":page_decls,"frontier_semantic_sha256":frontier_semantic,"source_radar":{"publication_commit_sha":args.radar_publication_commit_sha,"system_snapshot_sha256":system["system_snapshot_sha256"],"manifest_semantic_sha256":radar_manifest["manifest_semantic_sha256"],"rows_semantic_sha256":radar_manifest["rows_semantic_sha256"],"producer_release_sha256":system["producer_release_sha256"]},"frontier_producer_release":{"release_version":release["release_version"],"manifest_path":release_path.relative_to(root).as_posix(),"manifest_sha256":release_sha}}
    manifest={**manifest_body,"manifest_semantic_sha256":semantic_hash(manifest_body)}; manifest_path=snapshot_dir/"frontier-manifest.json"; manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    certificate_body={"schema_version":"1.0.0","kind":"qrgf_v31_market_certificate","snapshot_id":snapshot_id,"snapshot_content_sha256":snapshot_content,"market_session_id":system["market_session_id"],"created_at":args.created_at,"source_radar":{"publication_commit_sha":args.radar_publication_commit_sha,"market_session_id":system["market_session_id"],"snapshot_id":system["snapshot_id"],"snapshot_content_sha256":system["snapshot_content_sha256"],"system_snapshot_sha256":system["system_snapshot_sha256"],"manifest_semantic_sha256":radar_manifest["manifest_semantic_sha256"],"rows_semantic_sha256":radar_manifest["rows_semantic_sha256"],"producer_release_sha256":system["producer_release_sha256"],"universe_rows":len(radar),"market_scanned_rows":len(radar),"rankable_market_rows":counts["unified_rankable"],"legacy_rankable_market_rows":counts["legacy_rankable"],"rejected_rows":counts["rejected"],"market_data_incomplete_nonrejected_rows":counts["market_incomplete_nonrejected"]},"frontier":{"manifest_path":manifest_path.relative_to(root).as_posix(),"manifest_semantic_sha256":manifest["manifest_semantic_sha256"],"frontier_semantic_sha256":frontier_semantic,"frontier_model_sha256":model_sha,"scope_count":len(scopes),"security_count":security_count,"page_count":len(page_decls),"fixed_candidate_count_cutoff":False,"page_size_is_transport_only":True},"frontier_producer_release":{"release_version":release["release_version"],"manifest_path":release_path.relative_to(root).as_posix(),"manifest_sha256":release_sha}}
    certificate={**certificate_body,"certificate_sha256":semantic_hash(certificate_body)}; certificate_path=snapshot_dir/"market-certificate.json"; certificate_path.write_text(json.dumps(certificate,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    return {"snapshot_id":snapshot_id,"snapshot_dir":snapshot_dir.relative_to(root).as_posix(),"market_session_id":system["market_session_id"],"scope_count":len(scopes),"security_count":security_count,"page_count":len(page_decls),"certificate_path":certificate_path.relative_to(root).as_posix(),"certificate_sha256":certificate["certificate_sha256"],"frontier_manifest_path":manifest_path.relative_to(root).as_posix(),"frontier_manifest_sha256":manifest["manifest_semantic_sha256"],"market_data_incomplete_nonrejected_rows":counts["market_incomplete_nonrejected"]}

def main() -> int:
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--repo-root",type=Path,default=Path(".")); p.add_argument("--output-root",type=Path,default=Path("data/v31")); p.add_argument("--release-manifest",type=Path,default=Path("screening/config/v31-frontier-producer-release.json")); p.add_argument("--frontier-model",type=Path,default=Path("screening/config/v31-frontier-model.json")); p.add_argument("--radar-publication-commit-sha",required=True); p.add_argument("--created-at",required=True); p.add_argument("--page-size",type=int,default=24); args=p.parse_args()
    if args.page_size<1: p.error("page-size must be positive")
    result=build(args); print(json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
