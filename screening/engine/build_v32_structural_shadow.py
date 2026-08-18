#!/usr/bin/env python3
"""Build a shadow-only v3.2 Structural Facts / Quality-bound snapshot.

Nothing produced by this script is authorized for production selection. It measures whether
public SEC facts can tighten the v3.1 Quality=100 frontier safely enough to justify v3.2.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import v32_quality_math as qm


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",",":"), allow_nan=False).encode("utf-8")

def sem(value: Any) -> str: return hashlib.sha256(canonical(value)).hexdigest()
def sha_bytes(value: bytes) -> str: return hashlib.sha256(value).hexdigest()
def sha_file(path: Path) -> str: return sha_bytes(path.read_bytes())
def load(path: Path) -> dict[str,Any]:
    value=json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value,dict): raise ValueError(f"{path} must contain an object")
    return value

def git_show(root: Path, commit: str, path: str) -> bytes:
    cp=subprocess.run(["git","show",f"{commit}:{path}"],cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if cp.returncode!=0: raise ValueError(f"cannot read {path} at {commit}: {cp.stderr.decode(errors='replace')[:200]}")
    return cp.stdout

def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode()+data).hexdigest()

def pct(n: int, d: int) -> float:
    return round(100.0*n/d,2) if d else 0.0


class SecClient:
    def __init__(self, model: Mapping[str,Any], fixture_dir: Path|None=None):
        sec=model["sec"]; self.fixture_dir=fixture_dir
        self.ua=os.environ.get("SEC_USER_AGENT") or str(sec["default_user_agent"])
        self.interval=1.0/max(0.5,float(sec.get("requests_per_second") or 7.0)); self.retries=int(sec.get("max_retries") or 4)
        self.last=0.0; self.requests=0; self.errors: dict[str,int]={}
    def _fixture(self, kind: str, cik10: str|None=None) -> dict[str,Any]|None:
        if not self.fixture_dir: return None
        path=self.fixture_dir/("company_tickers_exchange.json" if kind=="ticker_map" else f"{kind}/CIK{cik10}.json")
        if not path.is_file(): return None
        return load(path)
    def get(self,url: str,*,kind: str,cik10: str|None=None) -> tuple[dict[str,Any]|None,str|None]:
        fixture=self._fixture(kind,cik10)
        if fixture is not None: return fixture,None
        for attempt in range(self.retries+1):
            wait=self.interval-(time.monotonic()-self.last)
            if wait>0: time.sleep(wait)
            req=urllib.request.Request(url,headers={"User-Agent":self.ua,"Accept":"application/json","Accept-Encoding":"identity"})
            self.last=time.monotonic(); self.requests+=1
            try:
                with urllib.request.urlopen(req,timeout=30) as response:
                    data=response.read()
                value=json.loads(data)
                return (value if isinstance(value,dict) else None),None if isinstance(value,dict) else "non_object_json"
            except urllib.error.HTTPError as exc:
                code=int(exc.code); key=f"http_{code}"; self.errors[key]=self.errors.get(key,0)+1
                if code in {429,500,502,503,504} and attempt<self.retries:
                    time.sleep(min(20.0,1.5*(2**attempt))); continue
                return None,key
            except Exception as exc:
                key=type(exc).__name__; self.errors[key]=self.errors.get(key,0)+1
                if attempt<self.retries:
                    time.sleep(min(20.0,1.5*(2**attempt))); continue
                return None,key
        return None,"unknown"


def verify_release(path: Path,root: Path) -> tuple[dict[str,Any],str]:
    release=load(path)
    if release.get("schema_version")!="1.0.0" or release.get("release_version")!="1.0.0": raise ValueError("invalid v3.2 shadow producer release")
    mapping={
        "build_v32_structural_shadow.py":root/"screening/engine/build_v32_structural_shadow.py",
        "verify_v32_structural_shadow.py":root/"screening/engine/verify_v32_structural_shadow.py",
        "v32_quality_math.py":root/"screening/engine/v32_quality_math.py",
        "v32-quality-bound-model.json":root/"screening/config/v32-quality-bound-model.json",
        "update-v32-shadow.yml":root/".github/workflows/update-v32-shadow.yml",
    }
    hashes=release.get("producer_hashes") or {}
    if set(hashes)!=set(mapping): raise ValueError("v3.2 shadow producer file set mismatch")
    for name,file in mapping.items():
        if not file.is_file() or sha_file(file)!=hashes[name]: raise ValueError(f"v3.2 shadow producer hash mismatch: {name}")
    return release,sha_file(path)


def load_v31_scopes(root: Path,pointer_path: Path) -> tuple[dict[str,Any],dict[str,Any],dict[str,Any],list[dict[str,Any]]]:
    pointer=load(pointer_path); body={k:v for k,v in pointer.items() if k!="pointer_sha256"}
    if pointer.get("kind")!="qrgf_v31_frontier_pointer" or pointer.get("pointer_sha256")!=sem(body): raise ValueError("invalid v3.1 pointer")
    commit=str(pointer.get("publication_commit_sha") or "")
    if len(commit)!=40: raise ValueError("invalid v3.1 publication commit")
    cert_bytes=git_show(root,commit,str(pointer["certificate_path"])); manifest_bytes=git_show(root,commit,str(pointer["frontier_manifest_path"]))
    cert=json.loads(cert_bytes); manifest=json.loads(manifest_bytes)
    cert_body={k:v for k,v in cert.items() if k!="certificate_sha256"}; man_body={k:v for k,v in manifest.items() if k!="manifest_semantic_sha256"}
    if cert.get("certificate_sha256")!=sem(cert_body) or cert.get("certificate_sha256")!=pointer.get("certificate_sha256"): raise ValueError("v3.1 certificate hash mismatch")
    if manifest.get("manifest_semantic_sha256")!=sem(man_body) or manifest.get("manifest_semantic_sha256")!=pointer.get("frontier_manifest_sha256"): raise ValueError("v3.1 manifest hash mismatch")
    scopes=[]; expected_rank=1
    for decl in manifest.get("pages") or []:
        data=git_show(root,commit,str(decl["path"]))
        if sha_bytes(data)!=str(decl.get("sha256") or ""): raise ValueError(f"v3.1 source page byte hash mismatch: {decl.get('path')}")
        page=json.loads(data); page_body={k:v for k,v in page.items() if k!="page_semantic_sha256"}
        if page.get("page_semantic_sha256")!=sem(page_body) or page.get("page_semantic_sha256")!=decl.get("page_semantic_sha256"): raise ValueError("v3.1 source page semantic hash mismatch")
        for scope in page.get("scopes") or []:
            if int(scope.get("frontier_rank") or -1)!=expected_rank: raise ValueError("v3.1 frontier rank gap")
            scopes.append(dict(scope)); expected_rank+=1
    if len(scopes)!=int(manifest.get("scope_count") or -1): raise ValueError("v3.1 source scope count mismatch")
    return pointer,cert,manifest,scopes


def ticker_map(client: SecClient,model: Mapping[str,Any]) -> tuple[dict[str,str],str]:
    last_error=""
    for url in model["sec"]["ticker_map_urls"]:
        value,error=client.get(str(url),kind="ticker_map")
        if value is None: last_error=error or "unknown"; continue
        out: dict[str,str]={}
        if isinstance(value.get("fields"),list) and isinstance(value.get("data"),list):
            fields=[str(x) for x in value["fields"]]
            for row in value["data"]:
                if not isinstance(row,list) or len(row)!=len(fields): continue
                item=dict(zip(fields,row)); ticker=str(item.get("ticker") or "").upper(); cik=str(item.get("cik") or "").strip()
                if ticker and cik.isdigit(): out[ticker]=f"{int(cik):010d}"
        else:
            for raw in value.values():
                if not isinstance(raw,Mapping): continue
                ticker=str(raw.get("ticker") or "").upper(); cik=str(raw.get("cik_str") or raw.get("cik") or "").strip()
                if ticker and cik.isdigit(): out[ticker]=f"{int(cik):010d}"
        if out: return out,str(url)
    raise ValueError(f"SEC ticker map unavailable: {last_error}")


def _ticker_variants(value: str) -> list[str]:
    ticker=str(value or "").upper().strip(); values=[ticker,ticker.replace(".","-"),ticker.replace("-",".")]
    return list(dict.fromkeys(v for v in values if v))

def resolve_cik(scope: Mapping[str,Any],mapping: Mapping[str,str]) -> tuple[str|None,str]:
    explicit=set()
    issuer=str(scope.get("issuer_id") or "")
    if issuer.startswith("CIK:") and issuer[4:].isdigit(): explicit.add(f"{int(issuer[4:]):010d}")
    mapped=set(explicit)
    for sec in scope.get("securities") or []:
        raw=str(sec.get("issuer_cik") or "").strip().lstrip("0")
        if raw.isdigit(): mapped.add(f"{int(raw):010d}")
        for variant in _ticker_variants(str(sec.get("ticker") or "")):
            if variant in mapping: mapped.add(mapping[variant]); break
    if len(mapped)==1: return next(iter(mapped)),"resolved"
    if len(mapped)>1: return None,"conflict"
    return None,"unresolved"


def _items(cf: Mapping[str,Any],aliases: Iterable[str],forms: set[str],unit_filter: Callable[[str],bool]) -> list[dict[str,Any]]:
    found=[]
    facts=cf.get("facts") if isinstance(cf.get("facts"),Mapping) else {}
    for taxonomy,concepts in facts.items():
        if not isinstance(concepts,Mapping): continue
        for concept in aliases:
            node=concepts.get(concept)
            if not isinstance(node,Mapping): continue
            units=node.get("units") if isinstance(node.get("units"),Mapping) else {}
            for unit,values in units.items():
                if not unit_filter(str(unit)) or not isinstance(values,list): continue
                for raw in values:
                    if not isinstance(raw,Mapping) or str(raw.get("form") or "") not in forms: continue
                    value=qm.num(raw.get("val")); end=str(raw.get("end") or "")
                    if value is None or not end: continue
                    found.append({"value":float(value),"end":end,"start":raw.get("start"),"filed":str(raw.get("filed") or ""),"accn":str(raw.get("accn") or ""),"form":str(raw.get("form") or ""),"fy":raw.get("fy"),"fp":raw.get("fp"),"unit":str(unit),"taxonomy":str(taxonomy),"concept":concept})
    return found


def _dedup_period(items: list[dict[str,Any]]) -> list[dict[str,Any]]:
    best={}
    for item in items:
        key=(item["end"],item["unit"],item["taxonomy"],item["concept"])
        if key not in best or (item.get("filed") or "")>(best[key].get("filed") or ""): best[key]=item
    return list(best.values())

def _series_by_concept(items: list[dict[str,Any]]) -> list[list[dict[str,Any]]]:
    groups={}
    for item in _dedup_period(items): groups.setdefault((item["taxonomy"],item["concept"],item["unit"]),[]).append(item)
    values=[]
    for series in groups.values():
        series.sort(key=lambda x:x["end"]); values.append(series)
    return values

def _annual_series(cf: Mapping[str,Any],aliases: Iterable[str],forms: set[str]) -> list[list[dict[str,Any]]]:
    return _series_by_concept(_items(cf,aliases,forms,lambda unit: unit not in {"shares","pure"}))
def _share_series(cf: Mapping[str,Any],aliases: Iterable[str],forms: set[str]) -> list[list[dict[str,Any]]]:
    return _series_by_concept(_items(cf,aliases,forms,lambda unit: unit=="shares"))


def _candidate_metric(series: list[dict[str,Any]],fn: Callable[[list[dict[str,Any]]],float|None]) -> list[tuple[float,dict[str,Any]]]:
    value=fn(series)
    return [] if value is None else [(float(value),{"taxonomy":series[-1]["taxonomy"],"concept":series[-1]["concept"],"unit":series[-1]["unit"],"period":series[-1]["end"],"filed":series[-1].get("filed"),"accession":series[-1].get("accn")})]
def _latest(series: list[dict[str,Any]]) -> float|None: return series[-1]["value"] if series else None
def _growth1(series: list[dict[str,Any]]) -> float|None:
    if len(series)<2 or series[-2]["value"]==0: return None
    return (series[-1]["value"]/series[-2]["value"]-1.0)*100.0

def _cagr3(series: list[dict[str,Any]]) -> float|None:
    if len(series)<4 or series[-4]["value"]<=0 or series[-1]["value"]<=0: return None
    return ((series[-1]["value"]/series[-4]["value"])**(1.0/3.0)-1.0)*100.0

def _best(candidates: list[tuple[float,dict[str,Any]]],score: Callable[[float],float|None],prefer_high: bool=True) -> tuple[float,dict[str,Any]]|None:
    scored=[]
    for value,meta in candidates:
        s=score(value)
        if s is not None: scored.append((float(s),float(value),meta))
    if not scored: return None
    scored.sort(key=lambda x:(x[0],x[1] if prefer_high else -x[1]),reverse=True)
    _,value,meta=scored[0]; return value,meta


def extract_bound_facts(cf: Mapping[str,Any],forms: set[str]) -> tuple[dict[str,Any],dict[str,Any]]:
    aliases={
        "revenue":["RevenueFromContractWithCustomerExcludingAssessedTax","SalesRevenueNet","Revenues","SalesRevenueGoodsNet","Revenue","RevenueFromContractsWithCustomers"],
        "operating_income":["OperatingIncomeLoss","OperatingProfitLoss"],
        "net_income":["NetIncomeLoss","ProfitLoss"],
        "cash":["CashAndCashEquivalentsAtCarryingValue","CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents","CashAndCashEquivalents"],
        "debt":["LongTermDebtAndFinanceLeaseObligations","LongTermDebtAndCapitalLeaseObligations","LongTermDebt","LongTermDebtNoncurrent"],
        "ocf":["NetCashProvidedByUsedInOperatingActivities","CashFlowsFromUsedInOperatingActivities"],
        "capex":["PaymentsToAcquirePropertyPlantAndEquipment","PurchaseOfPropertyPlantAndEquipment"],
        "shares":["EntityCommonStockSharesOutstanding","CommonStockSharesOutstanding"],
    }
    series={name:(_share_series(cf,names,forms) if name=="shares" else _annual_series(cf,names,forms)) for name,names in aliases.items()}
    facts: dict[str,Any]={}; lineage: dict[str,Any]={}
    rev_candidates=[]
    for s in series["revenue"]:
        rev_candidates += _candidate_metric(s,_growth1)
    chosen=_best(rev_candidates,lambda v: qm._growth(v))
    if chosen: facts["revenue_growth_pct"],lineage["revenue_growth_pct"]=chosen
    cagr=[]
    for s in series["revenue"]: cagr += _candidate_metric(s,_cagr3)
    chosen=_best(cagr,lambda v: qm._growth(v))
    if chosen: facts["revenue_cagr_3y_pct"],lineage["revenue_cagr_3y_pct"]=chosen

    margin_candidates=[]; change_candidates=[]
    for rs in series["revenue"]:
        rmap={x["end"]:x for x in rs}
        for os in series["operating_income"]:
            omap={x["end"]:x for x in os}; common=sorted(set(rmap)&set(omap))
            if not common: continue
            end=common[-1]; rev=rmap[end]["value"]
            if rev:
                value=omap[end]["value"]/rev*100.0; meta={"revenue":rmap[end],"operating_income":omap[end],"period":end}; margin_candidates.append((value,meta))
            if len(common)>=2:
                e0,e1=common[-2],common[-1]; r0,r1=rmap[e0]["value"],rmap[e1]["value"]
                if r0 and r1:
                    v0=omap[e0]["value"]/r0*100.0; v1=omap[e1]["value"]/r1*100.0; change_candidates.append((v1-v0,{"periods":[e0,e1],"revenue":[rmap[e0],rmap[e1]],"operating_income":[omap[e0],omap[e1]]}))
    chosen=_best(margin_candidates,lambda v: qm._margin(v))
    if chosen: facts["operating_margin_pct"],lineage["operating_margin_pct"]=chosen
    chosen=_best(change_candidates,lambda v: qm._margin_change(v))
    if chosen: facts["operating_margin_change_pp_yoy"],lineage["operating_margin_change_pp_yoy"]=chosen

    ni_growth=[]; ni_latest=[]
    for s in series["net_income"]:
        ni_latest += _candidate_metric(s,_latest)
        if len(s)>=2 and s[-2]["value"]>0: ni_growth += _candidate_metric(s,_growth1)
    if ni_latest:
        positive=any(v>0 for v,_ in ni_latest); facts["net_income_positive"]=positive; lineage["net_income_positive"]={"candidate_count":len(ni_latest)}
    chosen=_best(ni_growth,lambda v: qm._growth(v))
    if chosen: facts["earnings_growth_pct"],lineage["earnings_growth_pct"]=chosen

    fcf_candidates=[]
    for rs in series["revenue"]:
        rmap={x["end"]:x for x in rs}
        for os in series["ocf"]:
            omap={x["end"]:x for x in os}
            for cs in series["capex"]:
                cmap={x["end"]:x for x in cs}; common=sorted(set(rmap)&set(omap)&set(cmap))
                if not common: continue
                end=common[-1]; rev=rmap[end]["value"]
                if not rev: continue
                fcf=omap[end]["value"]-abs(cmap[end]["value"]); fcf_candidates.append((fcf/rev*100.0,{"period":end,"revenue":rmap[end],"ocf":omap[end],"capex":cmap[end],"fcf":fcf}))
    chosen=_best(fcf_candidates,lambda v: qm._margin(v))
    if chosen:
        facts["fcf_margin_pct"],lineage["fcf_margin_pct"]=chosen
        facts["fcf_positive"]=chosen[0]>0; lineage["fcf_positive"]=chosen[1]

    cash=[]; debt=[]
    for s in series["cash"]: cash += _candidate_metric(s,_latest)
    for s in series["debt"]: debt += _candidate_metric(s,_latest)
    if cash:
        value,meta=max(cash,key=lambda x:x[0]); facts["cash"]=value; lineage["cash"]=meta
    if debt:
        nonneg=[x for x in debt if x[0]>=0]
        if nonneg:
            value,meta=min(nonneg,key=lambda x:x[0]); facts["debt"]=value; lineage["debt"]=meta

    dilution=[]
    for s in series["shares"]:
        if len(s)>=2 and s[-2]["value"]>0: dilution += _candidate_metric(s,_growth1)
    chosen=_best(dilution,lambda v: qm._dilution(v),prefer_high=False)
    if chosen: facts["dilution_pct_yoy"],lineage["dilution_pct_yoy"]=chosen
    return facts,lineage


def sic_value(submissions: Mapping[str,Any]|None) -> int|None:
    if not submissions: return None
    raw=submissions.get("sic")
    try: return int(str(raw)) if raw not in (None,"") else None
    except ValueError: return None


def build(args: argparse.Namespace) -> dict[str,Any]:
    root=args.repo_root.resolve(); output_root=(root/args.output_root).resolve() if not args.output_root.is_absolute() else args.output_root.resolve()
    model_path=(root/args.model).resolve() if not args.model.is_absolute() else args.model.resolve(); release_path=(root/args.release_manifest).resolve() if not args.release_manifest.is_absolute() else args.release_manifest.resolve()
    model=load(model_path); release,release_sha=verify_release(release_path,root); model_sha=sem(model)
    if release.get("quality_bound_model_sha256")!=model_sha: raise ValueError("v3.2 release/model mismatch")
    pointer_path=(root/args.v31_pointer).resolve() if not args.v31_pointer.is_absolute() else args.v31_pointer.resolve()
    pointer,cert,manifest,scopes=load_v31_scopes(root,pointer_path)
    source_release=str((cert.get("frontier_producer_release") or {}).get("manifest_sha256") or "")
    if source_release!=str(release.get("source_v31_frontier_producer_release_sha256") or ""):
        raise ValueError("v3.2 shadow source v3.1 producer release is not approved")
    client=SecClient(model,args.sec_fixture_dir.resolve() if args.sec_fixture_dir else None); mapping,map_url=ticker_map(client,model)
    resolutions={}; unique_ciks=set()
    for scope in scopes:
        cik,status=resolve_cik(scope,mapping); resolutions[str(scope["research_scope_key"])]=(cik,status)
        if cik and str(scope.get("security_overlay") or "")!="etf": unique_ciks.add(cik)
    cache={}; forms=set(str(x) for x in model["sec"]["annual_forms"])
    for index,cik in enumerate(sorted(unique_ciks),1):
        cf_url=str(model["sec"]["companyfacts_url_template"]).format(cik10=cik); sub_url=str(model["sec"]["submissions_url_template"]).format(cik10=cik)
        cf,cf_err=client.get(cf_url,kind="companyfacts",cik10=cik); sub,sub_err=client.get(sub_url,kind="submissions",cik10=cik)
        facts,lineage=extract_bound_facts(cf or {},forms) if cf else ({},{})
        cache[cik]={"companyfacts_status":"ok" if cf else cf_err,"submissions_status":"ok" if sub else sub_err,"facts":facts,"lineage":lineage,"sic":sic_value(sub),"entity_name":(cf or {}).get("entityName") or (sub or {}).get("name")}
        if index%250==0: print(f"SEC progress {index}/{len(unique_ciks)}",flush=True)
    out=[]
    for scope in scopes:
        key=str(scope["research_scope_key"]); cik,resolution=resolutions[key]; info=cache.get(cik or "",{})
        facts=dict(info.get("facts") or {}); sic=info.get("sic") if isinstance(info.get("sic"),int) else None
        securities=scope.get("securities") or []; source_sector=next((str(s.get("sector") or "") for s in securities if str(s.get("sector") or "")),"")
        security_type=str(securities[0].get("security_type") or scope.get("security_overlay") or "common_equity") if securities else str(scope.get("security_overlay") or "common_equity")
        lanes=qm.possible_lanes(security_type=security_type,source_sector=source_sector,sic=sic,cik_resolved=resolution=="resolved",model=model)
        q_upper,binding,lane_bounds=qm.quality_upper_bound(facts,lanes,model)
        sec_rows=[]
        for sec in securities:
            final_upper=qm.progression_upper_bound(quality_upper=q_upper,setup=sec.get("l2_setup_score"),confidence=sec.get("l2_confidence_pct"),model=model)
            sec_rows.append({"ticker":sec.get("ticker"),"contract_id":sec.get("contract_id"),"l2_setup_score":sec.get("l2_setup_score"),"l2_confidence_pct":sec.get("l2_confidence_pct"),"v31_security_upper_bound":sec.get("security_upper_bound"),"v32_shadow_upper_bound":final_upper})
        scope_upper=max((float(x["v32_shadow_upper_bound"]) for x in sec_rows),default=q_upper)
        out.append({
            "research_scope_key":key,"issuer_id":scope.get("issuer_id"),"security_overlay":scope.get("security_overlay"),"source_frontier_rank":scope.get("frontier_rank"),
            "cik":cik,"cik_resolution_status":resolution,"sic":sic,"entity_name":info.get("entity_name"),"source_sector":source_sector,
            "companyfacts_status":info.get("companyfacts_status") if cik else "not_requested","submissions_status":info.get("submissions_status") if cik else "not_requested",
            "machine_bound_facts":facts,"fact_lineage":info.get("lineage") or {},"machine_fact_count":len(facts),
            "possible_lanes":lanes,"binding_lane":binding,"lane_quality_upper_bounds":lane_bounds,"quality_upper_bound":q_upper,"scope_upper_bound":scope_upper,
            "v31_scope_upper_bound":scope.get("scope_upper_bound"),"securities":sec_rows,
        })
    out.sort(key=lambda x:(-float(x["scope_upper_bound"]),str(x["research_scope_key"])))
    for rank,row in enumerate(out,1): row["shadow_rank"]=rank
    resolved=sum(1 for r in out if r["cik_resolution_status"]=="resolved"); factful=sum(1 for r in out if r["machine_fact_count"]>0); tightened=sum(1 for r in out if float(r["quality_upper_bound"])<99.9999)
    buckets={}
    for threshold in (99,97.5,95,92.5,90,87.5,85,82.5,80,75): buckets[str(threshold)]=sum(1 for r in out if float(r["scope_upper_bound"])>=threshold)
    quality_buckets={}
    for threshold in (100,99,97.5,95,92.5,90,87.5,85,80): quality_buckets[str(threshold)]=sum(1 for r in out if float(r["quality_upper_bound"])>=threshold)
    rank_marks={str(rank):float(out[rank-1]["scope_upper_bound"]) for rank in (30,50,100,250,500,1000) if len(out)>=rank}
    summary={
        "schema_version":"1.0.0","kind":"qrgf_v32_shadow_summary","shadow_only":True,"production_selection_authorized":False,
        "market_session_id":pointer["market_session_id"],"source_v31_snapshot_id":pointer["snapshot_id"],"source_v31_publication_commit_sha":pointer["publication_commit_sha"],
        "scope_count":len(out),"cik_resolved_scope_count":resolved,"cik_resolution_pct":pct(resolved,len(out)),"machine_fact_scope_count":factful,"machine_fact_scope_pct":pct(factful,len(out)),
        "quality_bound_tightened_scope_count":tightened,"quality_bound_tightened_pct":pct(tightened,len(out)),"final_upper_bound_threshold_counts":buckets,"quality_upper_bound_threshold_counts":quality_buckets,"final_upper_bound_at_rank":rank_marks,
        "sec_request_count":client.requests,"sec_errors":client.errors,"sec_ticker_map_source":map_url,
    }
    content_seed={"source_v31_snapshot_id":pointer["snapshot_id"],"source_v31_publication_commit_sha":pointer["publication_commit_sha"],"model_sha256":model_sha,"producer_release_sha256":release_sha,"shadow_semantic_sha256":sem(out),"created_at":args.created_at}
    snapshot_hash=sem(content_seed); snapshot_id=snapshot_hash[:24]; snapshot_dir=output_root/"snapshots"/snapshot_id
    if snapshot_dir.exists(): raise ValueError("immutable v3.2 shadow snapshot already exists")
    pages_dir=snapshot_dir/"pages"; pages_dir.mkdir(parents=True,exist_ok=True); page_decls=[]; page_size=int(args.page_size)
    for index,start in enumerate(range(0,len(out),page_size),1):
        chunk=out[start:start+page_size]; page_body={"schema_version":"1.0.0","kind":"qrgf_v32_shadow_page","snapshot_id":snapshot_id,"market_session_id":pointer["market_session_id"],"page_index":index,"scope_start_rank":chunk[0]["shadow_rank"],"scope_end_rank":chunk[-1]["shadow_rank"],"scopes":chunk}; page={**page_body,"page_semantic_sha256":sem(page_body)}
        path=pages_dir/f"page-{index:04d}.json"; path.write_text(json.dumps(page,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        page_decls.append({"page_index":index,"path":path.relative_to(root).as_posix(),"scope_count":len(chunk),"scope_start_rank":chunk[0]["shadow_rank"],"scope_end_rank":chunk[-1]["shadow_rank"],"max_scope_upper_bound":chunk[0]["scope_upper_bound"],"min_scope_upper_bound":chunk[-1]["scope_upper_bound"],"sha256":sha_file(path),"page_semantic_sha256":page["page_semantic_sha256"]})
    summary_path=snapshot_dir/"summary.json"; summary_body={**summary,"snapshot_id":snapshot_id,"snapshot_content_sha256":snapshot_hash,"quality_bound_model_sha256":model_sha,"producer_release_sha256":release_sha,"shadow_semantic_sha256":sem(out),"page_count":len(page_decls),"pages":page_decls}; summary_value={**summary_body,"summary_sha256":sem(summary_body)}; summary_path.write_text(json.dumps(summary_value,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    cert_body={"schema_version":"1.0.0","kind":"qrgf_v32_shadow_certificate","shadow_only":True,"production_selection_authorized":False,"snapshot_id":snapshot_id,"market_session_id":pointer["market_session_id"],"source_v31":{"snapshot_id":pointer["snapshot_id"],"publication_commit_sha":pointer["publication_commit_sha"],"certificate_sha256":pointer["certificate_sha256"],"frontier_manifest_sha256":pointer["frontier_manifest_sha256"],"scope_count":manifest["scope_count"]},"quality_bound_model_sha256":model_sha,"producer_release_sha256":release_sha,"summary_path":summary_path.relative_to(root).as_posix(),"summary_sha256":summary_value["summary_sha256"],"shadow_semantic_sha256":sem(out),"scope_count":len(out),"created_at":args.created_at}; cert={**cert_body,"certificate_sha256":sem(cert_body)}; cert_path=snapshot_dir/"certificate.json"; cert_path.write_text(json.dumps(cert,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    return {"snapshot_id":snapshot_id,"snapshot_dir":snapshot_dir.relative_to(root).as_posix(),"certificate_path":cert_path.relative_to(root).as_posix(),"certificate_sha256":cert["certificate_sha256"],"summary_path":summary_path.relative_to(root).as_posix(),"summary_sha256":summary_value["summary_sha256"],**{k:summary[k] for k in ("market_session_id","scope_count","cik_resolved_scope_count","machine_fact_scope_count","quality_bound_tightened_scope_count","final_upper_bound_threshold_counts","final_upper_bound_at_rank","sec_request_count","sec_errors")}}


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--repo-root",type=Path,default=Path(".")); ap.add_argument("--v31-pointer",type=Path,default=Path("data/v31/latest.json")); ap.add_argument("--output-root",type=Path,default=Path("data/v32-shadow")); ap.add_argument("--model",type=Path,default=Path("screening/config/v32-quality-bound-model.json")); ap.add_argument("--release-manifest",type=Path,default=Path("screening/config/v32-shadow-producer-release.json")); ap.add_argument("--created-at",required=True); ap.add_argument("--page-size",type=int,default=50); ap.add_argument("--sec-fixture-dir",type=Path); args=ap.parse_args()
    print(json.dumps(build(args),ensure_ascii=False,indent=2,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
