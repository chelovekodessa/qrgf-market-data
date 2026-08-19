#!/usr/bin/env python3
"""Publish V4.1 paginated market sessions only after MASTER COMPLETE."""
from __future__ import annotations
import argparse,csv,hashlib,json,math
from pathlib import Path
from typing import Any,Mapping
import promote_v4_state as state

PAGE_SIZE=250
ORDERING_MODEL="l2_setup_confidence_prior_growth_liquidity_identity-v1"
def canonical(v:Any)->bytes:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def sem(v:Any)->str:return hashlib.sha256(canonical(v)).hexdigest()
def sha(path:Path)->str:return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path:Path)->dict[str,Any]:
    v=json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(v,dict):raise ValueError(f"{path} must contain object")
    return v
def write(path:Path,v:Mapping[str,Any]):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(dict(v),ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
def num(v:Any)->float|None:
    try:x=float(v)
    except (TypeError,ValueError):return None
    return x if math.isfinite(x) else None
def digest(key:str)->str:return hashlib.sha256(key.encode()).hexdigest()
def verify_release(root:Path,path:Path)->str:
    rel=load(path);mapping={"build_v4_market_view.py":root/"screening/engine/build_v4_market_view.py","promote_v4_state.py":root/"screening/engine/promote_v4_state.py","test_build_v41_market_view.py":root/"screening/engine/test_build_v41_market_view.py","update-v4-market.yml":root/".github/workflows/update-v4-market.yml"};h=rel.get("producer_hashes") or {}
    if rel.get("schema_version")!="1.0.0" or rel.get("release_version")!="2.0.0" or set(h)!=set(mapping):raise ValueError("invalid V4.1 market producer release")
    for name,p in mapping.items():
        if not p.is_file() or sha(p)!=h[name]:raise ValueError(f"V4.1 market producer hash mismatch: {name}")
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
def blocked(root:Path,*,reason:str,release_sha:str,master_sha:str|None=None,campaign_phase:str|None=None)->dict[str,Any]:
    body={"schema_version":"2.0.0","kind":"qrgf_v41_market_pointer","ordinary_daily_broad_allowed":False,"reason":reason,"master_sha256":master_sha,"campaign_phase":campaign_phase,"producer_release_sha256":release_sha}
    pointer={**body,"pointer_sha256":sem(body)};write(root/"data/v4/market/v41/latest.json",pointer);return pointer
def load_authority(root:Path)->tuple[dict[str,Any],dict[str,Any],dict[str,Any]]|None:
    loaded=state.load_master_bundle(root)
    if loaded is None:return None
    _,bundle=loaded;master=bundle["master"];latest=root/"data/v4/campaign/latest.json"
    if not latest.exists():return bundle,master,{}
    pointer=load(latest);body={k:v for k,v in pointer.items() if k!="pointer_sha256"}
    if pointer.get("schema_version")!="2.0.0" or pointer.get("kind")!="qrgf_v41_campaign_pointer" or pointer.get("pointer_sha256")!=sem(body):raise ValueError("invalid V4.1 campaign pointer")
    campaign_state=state.validate_campaign_state(load(root/pointer["state_path"]),master)
    if pointer.get("master_sha256")!=master["master_sha256"] or pointer.get("state_sha256")!=campaign_state["state_sha256"]:raise ValueError("V4.1 market authority mismatch")
    return bundle,master,campaign_state
def registry_summary(root:Path,scope:Mapping[str,Any],market_session:str)->dict[str,Any]:
    key=str(scope["research_scope_key"]);path=root/"data/v4/registry/scopes"/f"{digest(key)}.json"
    if not path.exists():return {"registry_status":"missing","quality_reusable":False}
    try:
        entry=state.validate_entry(load(path));state.validate_passport_file(root,key,str(entry["passport_hash"]));state.validate_current_entry_receipt(root,entry)
        reusable=entry.get("freshness_status")=="fresh" and str(entry.get("event_scan_through") or "")>=str(market_session) and entry.get("quality_status") in {"pass","conditional","rejected"}
        return {"registry_status":entry.get("freshness_status"),"quality_reusable":bool(reusable),"quality_status":entry.get("quality_status"),"quality_score":entry.get("quality_score"),"quality_coverage_pct":entry.get("quality_coverage_pct"),"quality_eligible":entry.get("quality_eligible"),"event_scan_through":entry.get("event_scan_through"),"passport_hash":entry.get("passport_hash")}
    except Exception:return {"registry_status":"invalid","quality_reusable":False}
def projection(row:Mapping[str,Any])->dict[str,Any]:
    keep=("ticker","company","contract_id","security_type","instrument_status","exchange","sector","industry","current_price","reference_52w_high","market_cap","avg_dollar_volume","return_1m_pct","return_3m_pct","return_6m_pct","return_12m_pct","drawdown_pct","historical_volatility_pct","trading_history_days","momentum_history_status","data_integrity_status","as_of","l2_status","l2_setup_score","l2_confidence_pct","setup_prior_growth","setup_pullback_geometry","setup_liquidity","setup_data_completeness")
    return {k:row.get(k) for k in keep}
def challenger_scope(row:Mapping[str,Any])->str:
    explicit=str(row.get("research_scope_key") or "")
    if explicit:return explicit
    issuer=str(row.get("issuer_id") or "")
    if issuer:return f"{issuer}|{str(row.get('security_overlay') or 'common_equity')}"
    return f"security:{str(row.get('contract_id') or row.get('ticker') or '').upper()}"
def challenger_key(row:Mapping[str,Any])->tuple[Any,...]:
    setup=num(row.get("l2_setup_score"));conf=num(row.get("l2_confidence_pct"));growth=num(row.get("setup_prior_growth"));liq=num(row.get("avg_dollar_volume"));return (-(setup if setup is not None else -1),-(conf if conf is not None else -1),-(growth if growth is not None else -1),-(liq if liq is not None else -1),str(row.get("ticker") or ""),str(row.get("contract_id") or ""),challenger_scope(row))
def make_page(session_id:str,master_sha:str,index:int,start:int,rows:list[dict[str,Any]])->dict[str,Any]:
    body={"schema_version":"2.0.0","kind":"qrgf_v41_challenger_page","market_session_id":session_id,"master_sha256":master_sha,"ordering_model":ORDERING_MODEL,"page_index":index,"cursor_start":start,"cursor_end_exclusive":start+len(rows),"row_count":len(rows),"page_size":PAGE_SIZE,"transport_is_not_quality_whitelist":True,"exhaustive_full_market_top30_claim_authorized":False,"rows":rows}
    return {**body,"page_sha256":sem(body)}
def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--repo-root",type=Path,default=Path("."));ap.add_argument("--release-manifest",type=Path,default=Path("screening/config/v4-market-producer-release.json"));args=ap.parse_args();root=args.repo_root.resolve();release_sha=verify_release(root,args.release_manifest);authority=load_authority(root)
    if authority is None:
        result=blocked(root,reason="MASTER_CORE500_NOT_INITIALIZED",release_sha=release_sha);print(json.dumps({"status":"blocked","reason":result["reason"],"pointer_sha256":result["pointer_sha256"]},sort_keys=True));return 0
    bundle,master,campaign_state=authority
    if not campaign_state or campaign_state.get("phase")!="COMPLETE" or campaign_state.get("daily_broad_allowed") is not True:
        result=blocked(root,reason="MASTER_CORE500_NOT_COMPLETE",release_sha=release_sha,master_sha=master["master_sha256"],campaign_phase=campaign_state.get("phase") if campaign_state else None);print(json.dumps({"status":"blocked","reason":result["reason"],"phase":result["campaign_phase"],"pointer_sha256":result["pointer_sha256"]},sort_keys=True));return 0
    system,rows=load_radar(root);market=str(system["market_session_id"]);by_ticker={}
    for scope in master["scopes"]:
        for ticker in scope.get("member_tickers") or [scope.get("ticker")]:
            if ticker:by_ticker[str(ticker).upper()]=scope
    core=[];grouped={}
    for raw in rows:
        ticker=str(raw.get("ticker") or "").upper();scope=by_ticker.get(ticker)
        if scope:
            item=projection(raw);item.update({"issuer_id":scope.get("issuer_id"),"security_overlay":scope.get("security_overlay"),"research_scope_key":scope.get("research_scope_key"),"master_rank":scope.get("rank"),"bootstrap_priority_score":scope.get("bootstrap_priority_score"),**registry_summary(root,scope,market)});core.append(item);continue
        if raw.get("instrument_status")!="eligible" or raw.get("l2_status") not in {"pass","conditional","recheck"}:continue
        key=challenger_scope(raw);previous=grouped.get(key)
        if previous is None or challenger_key(raw)<challenger_key(previous):grouped[key]=raw
    core.sort(key=lambda x:(int(x.get("master_rank") or 999999),str(x.get("ticker") or "")));ordered=sorted(grouped.values(),key=challenger_key);challengers=[]
    for ordinal,raw in enumerate(ordered):
        item=projection(raw);item.update({"research_scope_key":challenger_scope(raw),"transport_ordinal":ordinal});challengers.append(item)
    seed={"source_market_session_id":market,"source_snapshot_id":system.get("snapshot_id"),"master_sha256":master["master_sha256"],"challenger_scope_keys":[x["research_scope_key"] for x in challengers]};session_id=f"{market}-{sem(seed)[:16]}";session=root/"data/v4/market/v41/sessions"/session_id;pages=[]
    for index,start in enumerate(range(0,len(challengers),PAGE_SIZE)):
        page=make_page(session_id,master["master_sha256"],index,start,challengers[start:start+PAGE_SIZE]);path=session/"challengers"/f"page-{index:04d}.json";write(path,page);pages.append({"page_index":index,"path":path.relative_to(session).as_posix(),"page_sha256":page["page_sha256"],"cursor_start":start,"cursor_end_exclusive":start+len(page["rows"]),"row_count":len(page["rows"])})
    core_body={"schema_version":"2.0.0","kind":"qrgf_v41_core_market_view","market_session_id":session_id,"source_market_session_id":market,"master_sha256":master["master_sha256"],"campaign_state_sha256":campaign_state["state_sha256"],"rows":core};core_view={**core_body,"view_sha256":sem(core_body)};write(session/"core.json",core_view)
    manifest_body={"schema_version":"2.0.0","kind":"qrgf_v41_market_session_manifest","market_session_id":session_id,"source_market_session_id":market,"source_snapshot_id":system.get("snapshot_id"),"master_sha256":master["master_sha256"],"campaign_state_sha256":campaign_state["state_sha256"],"ordinary_daily_broad_allowed":True,"ordering_model":ORDERING_MODEL,"page_size":PAGE_SIZE,"total_eligible_challengers":len(challengers),"page_count":len(pages),"pages":pages,"transport_is_not_quality_whitelist":True,"exhaustive_full_market_top30_claim_authorized":False};manifest={**manifest_body,"manifest_sha256":sem(manifest_body)};write(session/"manifest.json",manifest)
    pointer_body={"schema_version":"2.0.0","kind":"qrgf_v41_market_pointer","ordinary_daily_broad_allowed":True,"market_session_id":session_id,"source_market_session_id":market,"master_sha256":master["master_sha256"],"campaign_state_sha256":campaign_state["state_sha256"],"manifest_path":(session/"manifest.json").relative_to(root).as_posix(),"manifest_sha256":manifest["manifest_sha256"],"core_path":(session/"core.json").relative_to(root).as_posix(),"core_sha256":sha(session/"core.json"),"core_security_rows":len(core),"total_eligible_challengers":len(challengers),"page_size":PAGE_SIZE,"page_count":len(pages),"producer_release_sha256":release_sha};pointer={**pointer_body,"pointer_sha256":sem(pointer_body)};write(root/"data/v4/market/v41/latest.json",pointer)
    print(json.dumps({"status":"published","market_session_id":session_id,"core_rows":len(core),"eligible_noncore":len(challengers),"page_count":len(pages),"pointer_sha256":pointer["pointer_sha256"]},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
