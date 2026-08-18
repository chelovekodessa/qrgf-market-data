#!/usr/bin/env python3
"""Build compact V4 market view from full V3 Radar + Core500 + V4 Registry.

No SEC access and no model inference. Core500 is never a whitelist: a separate
full-market challenger transport is always emitted for non-core securities.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,math
from pathlib import Path
from typing import Any,Mapping

def canonical(v:Any)->bytes:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def sem(v:Any)->str:return hashlib.sha256(canonical(v)).hexdigest()
def sha(path:Path)->str:return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path:Path)->dict[str,Any]:
    v=json.loads(path.read_text(encoding="utf-8-sig"));
    if not isinstance(v,dict):raise ValueError(f"{path} must contain object")
    return v
def write(path:Path,v:Mapping[str,Any]):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(dict(v),ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
def num(v:Any)->float|None:
    try:x=float(v)
    except (TypeError,ValueError):return None
    return x if math.isfinite(x) else None
def digest(key:str)->str:return hashlib.sha256(key.encode()).hexdigest()
def verify_release(root:Path,path:Path)->str:
    rel=load(path); mapping={"build_v4_market_view.py":root/"screening/engine/build_v4_market_view.py","update-v4-market.yml":root/".github/workflows/update-v4-market.yml"};h=rel.get("producer_hashes") or {}
    if rel.get("schema_version")!="1.0.0" or rel.get("release_version")!="1.0.0" or set(h)!=set(mapping):raise ValueError("invalid v4 market producer release")
    for name,p in mapping.items():
        if not p.is_file() or sha(p)!=h[name]:raise ValueError(f"v4 market producer hash mismatch: {name}")
    return sha(path)
def load_radar(root:Path)->tuple[dict[str,Any],list[dict[str,str]]]:
    system=load(root/"data/v3/latest.json");manifest=load(root/system["radar_manifest_path"])
    if system.get("complete") is not True or manifest.get("complete") is not True:raise ValueError("V3 Radar incomplete")
    base=(root/system["radar_manifest_path"]).parent;rows=[]
    for page in manifest.get("pages") or []:
        path=base/page["name"]
        if sha(path)!=page["sha256"]:raise ValueError(f"Radar page hash mismatch: {page['name']}")
        with path.open(newline="",encoding="utf-8-sig") as f:rows.extend(dict(x) for x in csv.DictReader(f))
    if len(rows)!=int(manifest.get("rows") or -1):raise ValueError("Radar row count mismatch")
    return system,rows
def load_core(root:Path)->tuple[dict[str,Any],dict[str,dict[str,Any]]]:
    latest=root/"data/v4/bootstrap/latest.json"
    if not latest.exists():raise ValueError("V4 Core500 not published")
    ptr=load(latest);cohort=load(root/ptr["cohort_path"])
    body={k:v for k,v in cohort.items() if k!="cohort_sha256"}
    if cohort.get("cohort_sha256")!=sem(body):raise ValueError("Core500 cohort hash mismatch")
    by_ticker={}
    for scope in cohort.get("scopes") or []:
        for ticker in scope.get("member_tickers") or [scope.get("ticker")]:
            if ticker:by_ticker[str(ticker).upper()]=scope
    return cohort,by_ticker
def registry_summary(root:Path,scope:Mapping[str,Any],market_session:str)->dict[str,Any]:
    key=str(scope["research_scope_key"]);path=root/"data/v4/registry/scopes"/f"{digest(key)}.json"
    if not path.exists():return {"registry_status":"missing","quality_reusable":False}
    e=load(path);body={k:v for k,v in e.items() if k!="entry_sha256"}
    if e.get("entry_sha256")!=sem(body) or e.get("research_scope_key")!=key:return {"registry_status":"invalid","quality_reusable":False}
    reusable=e.get("freshness_status")=="fresh" and str(e.get("event_scan_through") or "")>=str(market_session) and e.get("quality_status") in {"pass","conditional","rejected"}
    return {"registry_status":e.get("freshness_status"),"quality_reusable":bool(reusable),"quality_status":e.get("quality_status"),"quality_score":e.get("quality_score"),"quality_coverage_pct":e.get("quality_coverage_pct"),"quality_eligible":e.get("quality_eligible"),"event_scan_through":e.get("event_scan_through"),"passport_hash":e.get("passport_hash")}
def market_projection(row:Mapping[str,Any])->dict[str,Any]:
    keep=("ticker","company","contract_id","security_type","instrument_status","exchange","sector","industry","current_price","reference_52w_high","market_cap","avg_dollar_volume","return_1m_pct","return_3m_pct","return_6m_pct","return_12m_pct","drawdown_pct","historical_volatility_pct","trading_history_days","momentum_history_status","data_integrity_status","as_of","l2_status","l2_setup_score","l2_confidence_pct","setup_prior_growth","setup_pullback_geometry","setup_liquidity","setup_data_completeness")
    return {k:row.get(k) for k in keep}
def challenger_key(row:Mapping[str,Any])->tuple[Any,...]:
    setup=num(row.get("l2_setup_score"));conf=num(row.get("l2_confidence_pct"));growth=num(row.get("setup_prior_growth"));liq=num(row.get("avg_dollar_volume"));ticker=str(row.get("ticker") or "")
    return (-(setup if setup is not None else -1),-(conf if conf is not None else -1),-(growth if growth is not None else -1),-(liq if liq is not None else -1),ticker)
def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--repo-root",type=Path,default=Path("."));ap.add_argument("--release-manifest",type=Path,default=Path("screening/config/v4-market-producer-release.json"));ap.add_argument("--challenger-limit",type=int,default=250);args=ap.parse_args();root=args.repo_root.resolve();release_sha=verify_release(root,args.release_manifest);system,rows=load_radar(root);cohort,core_by_ticker=load_core(root);market=str(system["market_session_id"])
    core=[];chall=[]
    for raw in rows:
        ticker=str(raw.get("ticker") or "").upper();scope=core_by_ticker.get(ticker)
        if scope:
            item=market_projection(raw);item.update({"issuer_id":scope.get("issuer_id"),"security_overlay":scope.get("security_overlay"),"research_scope_key":scope.get("research_scope_key"),"core500_rank":scope.get("rank"),"bootstrap_priority_score":scope.get("bootstrap_priority_score"),**registry_summary(root,scope,market)});core.append(item)
        elif raw.get("instrument_status")=="eligible" and raw.get("l2_status") in {"pass","conditional","recheck"}:
            chall.append(market_projection(raw))
    core.sort(key=lambda x:(int(x.get("core500_rank") or 999999),str(x.get("ticker") or "")))
    chall.sort(key=challenger_key);transport=chall[:max(1,int(args.challenger_limit))]
    core_body={"schema_version":"1.0.0","kind":"qrgf_v4_core_market_view","market_session_id":market,"core500_cohort_sha256":cohort["cohort_sha256"],"rows":core};core_view={**core_body,"view_sha256":sem(core_body)}
    ch_body={"schema_version":"1.0.0","kind":"qrgf_v4_market_challenger_view","market_session_id":market,"core500_cohort_sha256":cohort["cohort_sha256"],"transport_limit":int(args.challenger_limit),"eligible_noncore_count":len(chall),"transport_count":len(transport),"transport_is_not_quality_whitelist":True,"exhaustive_full_market_top30_claim_authorized":False,"rows":transport};ch_view={**ch_body,"view_sha256":sem(ch_body)}
    out=root/"data/v4/market";write(out/"core.json",core_view);write(out/"challengers.json",ch_view)
    pointer_body={"schema_version":"1.0.0","kind":"qrgf_v4_market_pointer","market_session_id":market,"source_v3_snapshot_id":system.get("snapshot_id"),"core500_cohort_sha256":cohort["cohort_sha256"],"core_path":"data/v4/market/core.json","core_sha256":sha(out/"core.json"),"challengers_path":"data/v4/market/challengers.json","challengers_sha256":sha(out/"challengers.json"),"core_security_rows":len(core),"eligible_noncore_count":len(chall),"challenger_transport_count":len(transport),"producer_release_sha256":release_sha};pointer={**pointer_body,"pointer_sha256":sem(pointer_body)};write(out/"latest.json",pointer)
    print(json.dumps({"market_session_id":market,"core_rows":len(core),"eligible_noncore":len(chall),"challenger_transport":len(transport),"pointer_sha256":pointer["pointer_sha256"]},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
