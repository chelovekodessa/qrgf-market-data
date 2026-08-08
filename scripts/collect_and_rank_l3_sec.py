#!/usr/bin/env python3
from __future__ import annotations
import argparse,html,json,math,re,time,urllib.error,urllib.request
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
import evaluate_l3

UA='qrgf-market-data research/1.0 github.com/chelovekodessa/qrgf-market-data'
HEADERS={'User-Agent':UA,'Accept-Encoding':'gzip, deflate','Host':'www.sec.gov'}
DATA_HEADERS={'User-Agent':UA,'Accept-Encoding':'gzip, deflate'}
ANNUAL_FORMS={'10-K','10-K/A','20-F','20-F/A','40-F','40-F/A'}
PERIODIC_FORMS=ANNUAL_FORMS|{'10-Q','10-Q/A','6-K','6-K/A'}


def nowz():return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')

def get_json(url:str,retries:int=3):
    err=None
    for attempt in range(retries):
        try:
            req=urllib.request.Request(url,headers=DATA_HEADERS if 'data.sec.gov' in url else {'User-Agent':UA})
            with urllib.request.urlopen(req,timeout=45) as r:return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            err=e;time.sleep(0.6*(attempt+1))
    raise err

def get_text(url:str,retries:int=3):
    err=None
    for attempt in range(retries):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'text/html,application/xhtml+xml'})
            with urllib.request.urlopen(req,timeout=45) as r:return r.read().decode('utf-8','replace')
        except Exception as e:
            err=e;time.sleep(0.6*(attempt+1))
    raise err

def throttle():time.sleep(0.17)

def ticker_map():
    raw=get_json('https://www.sec.gov/files/company_tickers.json');out={}
    for v in raw.values():
        t=str(v.get('ticker') or '').upper();c=v.get('cik_str')
        if t and c is not None:out[t]=str(int(c)).zfill(10)
    return out

def facts_for(companyfacts:dict[str,Any],tags:list[str],unit_hint:str|None=None):
    facts=companyfacts.get('facts') or {}
    for taxonomy in ('us-gaap','ifrs-full','dei'):
        tax=facts.get(taxonomy) or {}
        for tag in tags:
            concept=tax.get(tag)
            if not isinstance(concept,dict):continue
            units=concept.get('units') or {}
            unit_names=[]
            if unit_hint and unit_hint in units:unit_names=[unit_hint]
            else:unit_names=list(units)
            rows=[]
            for u in unit_names:
                for item in units.get(u,[]):
                    if isinstance(item,dict) and item.get('val') is not None:rows.append(dict(item,unit=u,tag=tag,taxonomy=taxonomy))
            if rows:return rows
    return []

def annual_rows(companyfacts:dict[str,Any],tags:list[str],unit_hint:str|None=None):
    rows=facts_for(companyfacts,tags,unit_hint);out=[]
    for r in rows:
        if str(r.get('form') or '').upper() not in ANNUAL_FORMS:continue
        start=r.get('start');end=r.get('end')
        if start and end:
            try:
                d1=datetime.fromisoformat(str(start)[:10]);d2=datetime.fromisoformat(str(end)[:10])
                if (d2-d1).days<300:continue
            except Exception:pass
        out.append(r)
    out.sort(key=lambda r:(str(r.get('end') or ''),str(r.get('filed') or ''),str(r.get('accn') or '')),reverse=True)
    unique=[];seen=set()
    for r in out:
        end=str(r.get('end') or '')
        if end in seen:continue
        seen.add(end);unique.append(r)
    return unique

def instant_rows(companyfacts:dict[str,Any],tags:list[str],unit_hint:str|None=None):
    rows=facts_for(companyfacts,tags,unit_hint);rows.sort(key=lambda r:(str(r.get('end') or ''),str(r.get('filed') or '')),reverse=True);unique=[];seen=set()
    for r in rows:
        end=str(r.get('end') or '')
        if end in seen:continue
        seen.add(end);unique.append(r)
    return unique

def value(r):
    try:
        x=float(r.get('val'));return x if math.isfinite(x) else None
    except Exception:return None

def growth(latest,prior):
    a=value(latest) if latest else None;b=value(prior) if prior else None
    if a is None or b in (None,0):return None
    return 100.0*(a/b-1.0)

def latest_form(submissions:dict[str,Any]):
    rec=(submissions.get('filings') or {}).get('recent') or {};forms=rec.get('form') or [];accessions=rec.get('accessionNumber') or [];docs=rec.get('primaryDocument') or [];filed=rec.get('filingDate') or [];periods=rec.get('reportDate') or [];items=rec.get('items') or []
    candidates=[]
    for i,form in enumerate(forms):
        f=str(form or '').upper()
        if f not in PERIODIC_FORMS:continue
        candidates.append({'form':f,'accession':accessions[i] if i<len(accessions) else '', 'primaryDocument':docs[i] if i<len(docs) else '', 'filingDate':filed[i] if i<len(filed) else '', 'reportDate':periods[i] if i<len(periods) else '', 'items':items[i] if i<len(items) else ''})
    annual=[r for r in candidates if r['form'] in ANNUAL_FORMS]
    chosen=(annual or candidates)
    return chosen[0] if chosen else None

def recent_item_402(submissions:dict[str,Any]):
    rec=(submissions.get('filings') or {}).get('recent') or {};forms=rec.get('form') or [];items=rec.get('items') or [];filed=rec.get('filingDate') or []
    for i,form in enumerate(forms[:300]):
        if str(form or '').upper() not in {'8-K','8-K/A'}:continue
        item=str(items[i] if i<len(items) else '')
        date=str(filed[i] if i<len(filed) else '')
        if '4.02' in item and (not date or date>='2024-01-01'):return True
    return False

def clean_text(raw:str):
    text=re.sub(r'(?is)<script.*?</script>|<style.*?</style>',' ',raw);text=re.sub(r'(?s)<[^>]+>',' ',text);text=html.unescape(text);return re.sub(r'\s+',' ',text).lower()

def clearances(text:str,submissions:dict[str,Any],active_listing:bool):
    going=bool(re.search(r'substantial doubt.{0,250}ability to continue as a going concern|substantial doubt.{0,250}continue as a going concern',text))
    bankruptcy=bool(re.search(r'filed.{0,80}(?:for|under).{0,80}chapter 11|chapter 11 bankruptcy|bankruptcy petition',text)) or going
    investigation=bool(re.search(r'accounting investigation|investigation.{0,120}accounting practices|investigation.{0,120}financial reporting',text))
    restatement=recent_item_402(submissions) or bool(re.search(r'non-reliance on previously issued financial statements|restatement of previously issued financial statements',text))
    delisting=bool(re.search(r'delisting notice|notice of delisting|minimum bid price requirement|noncompliance.{0,120}listing requirement|non-compliance.{0,120}listing requirement',text)) or not active_listing
    return {'bankruptcy_risk':bankruptcy,'going_concern_warning':going,'accounting_investigation':investigation,'material_restatement':restatement,'delisting_risk':delisting}

def choose_lane(row:dict[str,Any],fund:dict[str,Any]):
    st=str(row.get('security_type') or '').lower();sector=str(row.get('sector') or '').lower();name=str(row.get('company') or '').lower()
    if st=='etf':return 'etf'
    if st=='adr':return 'adr'
    if any(x in sector for x in ('finance','financial')) and any(x in name for x in ('bank','bancorp','financial')):return 'bank'
    if any(x in sector for x in ('energy','mineral','materials','basic materials')):return 'cyclical'
    rg=fund.get('revenue_growth_pct');runway=fund.get('cash_runway_months')
    if fund.get('net_income_positive') is False and isinstance(rg,(int,float)) and rg>=20 and isinstance(runway,(int,float)) and runway>=24:return 'recognized_growth'
    return 'established_quality'

def binary_event(row:dict[str,Any],fund:dict[str,Any]):
    if str(row.get('security_type') or '').lower()=='etf':return False
    sector=str(row.get('sector') or '').lower();rev=fund.get('_latest_revenue')
    if any(x in sector for x in ('health','biotech','pharma')):
        if isinstance(rev,(int,float)) and rev<250_000_000 and fund.get('net_income_positive') is False:return True
        if isinstance(rev,(int,float)) and rev>=1_000_000_000 and (fund.get('net_income_positive') is True or fund.get('fcf_positive') is True):return False
        return None
    return False

def build_fundamentals(cf:dict[str,Any]):
    rev=annual_rows(cf,['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet','Revenue'],'USD');ni=annual_rows(cf,['NetIncomeLoss','ProfitLoss'],'USD');op=annual_rows(cf,['OperatingIncomeLoss','ProfitLossFromOperatingActivities'],'USD');ocf=annual_rows(cf,['NetCashProvidedByUsedInOperatingActivities','CashFlowsFromUsedInOperatingActivities'],'USD');capex=annual_rows(cf,['PaymentsToAcquirePropertyPlantAndEquipment','PurchaseOfPropertyPlantAndEquipment'],'USD');cash=instant_rows(cf,['CashAndCashEquivalentsAtCarryingValue','CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents','CashAndCashEquivalents'],'USD');debt_total=instant_rows(cf,['LongTermDebtAndFinanceLeaseObligations','LongTermDebt'],'USD');debt_non=instant_rows(cf,['LongTermDebtNoncurrent'],'USD');debt_cur=instant_rows(cf,['LongTermDebtCurrent','LongTermDebtAndFinanceLeaseObligationsCurrent','DebtCurrent'],'USD');short=instant_rows(cf,['ShortTermBorrowings'],'USD');shares=instant_rows(cf,['EntityCommonStockSharesOutstanding','CommonStockSharesOutstanding'],'shares')
    out={};r0=value(rev[0]) if rev else None;r1=value(rev[1]) if len(rev)>1 else None;n0=value(ni[0]) if ni else None;n1=value(ni[1]) if len(ni)>1 else None;o0=value(op[0]) if op else None;oc0=value(ocf[0]) if ocf else None;cx0=value(capex[0]) if capex else None
    if r0 is not None:out['_latest_revenue']=r0
    g=growth(rev[0],rev[1]) if len(rev)>1 else None
    if g is not None:out['revenue_growth_pct']=round(g,4)
    eg=growth(ni[0],ni[1]) if len(ni)>1 and n0 is not None and n1 is not None and n0*n1>0 else None
    if eg is not None:out['earnings_growth_pct']=round(eg,4)
    if r0 not in (None,0) and o0 is not None:out['operating_margin_pct']=round(100.0*o0/r0,4)
    fcf=None
    if oc0 is not None:
        fcf=oc0-(cx0 or 0.0)
        out['fcf_positive']=fcf>0
        if r0 not in (None,0):out['fcf_margin_pct']=round(100.0*fcf/r0,4)
    if n0 is not None:out['net_income_positive']=n0>0
    cash0=value(cash[0]) if cash else None
    if cash0 is not None:out['cash']=cash0
    d0=value(debt_total[0]) if debt_total else None
    if d0 is None:
        parts=[value(x[0]) if x else None for x in (debt_non,debt_cur,short)];parts=[x for x in parts if x is not None];d0=sum(parts) if parts else None
    if d0 is not None:out['debt']=d0
    if fcf is not None and fcf<0 and cash0 is not None and -fcf>0:out['cash_runway_months']=round(12.0*cash0/(-fcf),2)
    if len(shares)>1:
        s0=value(shares[0]);s1=value(shares[1])
        if s0 is not None and s1 not in (None,0):out['dilution_pct_yoy']=round(100.0*(s0/s1-1.0),4)
    return out

def main():
    ap=argparse.ArgumentParser();ap.add_argument('l2_json',type=Path);ap.add_argument('--out-dir',type=Path,required=True);ap.add_argument('--ruleset-version',default='3.1.0');ap.add_argument('--ruleset-hash',required=True);args=ap.parse_args();args.out_dir.mkdir(parents=True,exist_ok=True)
    l2=json.loads(args.l2_json.read_text(encoding='utf-8'));rows=l2.get('finalists') or [];mapping=ticker_map();throttle();retrieved=nowz();payloads=[];collection=[]
    for idx,row in enumerate(rows,1):
        ticker=str(row.get('ticker') or '').upper();cik=mapping.get(ticker);fund={};ev=[];errors=[];filing=None;filing_url=None
        if cik:
            try:subs=get_json(f'https://data.sec.gov/submissions/CIK{cik}.json');throttle()
            except Exception as e:subs={};errors.append('submissions:'+repr(e))
            try:cf=get_json(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json');throttle();fund=build_fundamentals(cf)
            except Exception as e:cf={};errors.append('companyfacts:'+repr(e))
            filing=latest_form(subs) if subs else None
            if filing and filing.get('accession') and filing.get('primaryDocument'):
                cik_int=str(int(cik));acc=str(filing['accession']).replace('-','');filing_url=f'https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{filing["primaryDocument"]}'
                try:
                    raw=get_text(filing_url);throttle();txt=clean_text(raw);fund.update(clearances(txt,subs,str(row.get('instrument_status') or '').lower() in {'eligible','verified','active'}))
                except Exception as e:errors.append('filing:'+repr(e))
        else:errors.append('cik_not_found')
        be=binary_event(row,fund)
        if be is not None:fund['binary_business_event']=be
        lane=choose_lane(row,fund)
        as_of=(filing or {}).get('filingDate') or retrieved[:10]
        clean_fund={k:v for k,v in fund.items() if not k.startswith('_')}
        if clean_fund:
            ev=[{'evidence_id':f'sec_l3_{ticker}_{cik or "none"}','field':'fundamentals','value':clean_fund,'unit':None,'period':(filing or {}).get('reportDate') or None,'as_of':as_of,'retrieved_at':retrieved,'source':{'source_id':f'sec_edgar_{cik or ticker}','source_type':'derived','url':filing_url or f'https://data.sec.gov/submissions/CIK{cik}.json' if cik else None,'document_id':(filing or {}).get('accession')},'quality_status':'usable','missing_reason':None,'confidence':88.0 if not errors else 70.0,'notes':'Derived from SEC EDGAR Company Facts, submissions metadata, and latest periodic filing text; absent source fields remain missing.'}]
        payload={k:row.get(k) for k in ('ticker','contract_id','company','security_type','instrument_status','exchange','sector')};payload.update({'quality_lane':lane,'fundamentals':clean_fund,'evidence':ev,'l2_status':row.get('l2_status'),'research_priority_score':row.get('research_priority_score'),'retrieved_at':retrieved});payloads.append(payload)
        collection.append({'ticker':ticker,'cik':cik,'lane':lane,'fundamental_field_count':len(clean_fund),'filing':filing,'filing_url':filing_url,'errors':errors})
        print(idx,ticker,lane,len(clean_fund),errors[:1],flush=True)
    results=[]
    for payload in payloads:
        result={k:payload.get(k) for k in ('ticker','contract_id','company','security_type','instrument_status','exchange','sector')};result.update(evaluate_l3.evaluate(payload));result['ruleset_version']=args.ruleset_version;result['ruleset_hash']=args.ruleset_hash;results.append(result)
    def key(r):
        status=2 if r.get('l3_status')=='pass' else 1 if r.get('l3_status')=='conditional' else 0;score=r.get('l3_score');cov=r.get('l3_coverage_pct') or 0;t=str(r.get('ticker') or '');c=str(r.get('contract_id') or '');return(status,float(score) if score is not None else -1.0,float(cov),tuple(-ord(x) for x in t),tuple(-ord(x) for x in c))
    ranked=sorted(results,key=key,reverse=True);eligible=[r for r in ranked if r.get('l3_status') in {'pass','conditional'} and r.get('l3_score') is not None];finalists=eligible[:30];final_keys={(str(r.get('ticker') or '').upper(),str(r.get('contract_id') or '')) for r in finalists}
    for r in ranked:r['selected_for_next_stage']=(str(r.get('ticker') or '').upper(),str(r.get('contract_id') or '')) in final_keys
    out={'stage':'L3','processed_count':len(ranked),'eligible_count':len(eligible),'finalist_count':len(finalists),'finalist_ceiling':30,'weak_candidates_fill_quota':False,'global_ranking':True,'status_counts':dict(sorted(Counter(str(r.get('l3_status') or 'unknown') for r in ranked).items())),'all_results':ranked,'finalists':finalists,'ruleset_version':args.ruleset_version,'ruleset_hash':args.ruleset_hash}
    (args.out_dir/'l3-input.json').write_text(json.dumps({'payloads':payloads},indent=2)+'\n',encoding='utf-8');(args.out_dir/'l3-output.json').write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8');(args.out_dir/'l3-collection.json').write_text(json.dumps({'retrieved_at':retrieved,'candidate_count':len(rows),'results':collection},indent=2)+'\n',encoding='utf-8')
    summary={k:v for k,v in out.items() if k not in {'all_results','finalists'}};summary['top30']=[{k:r.get(k) for k in ('ticker','contract_id','quality_lane','l3_status','l3_score','l3_coverage_pct','hard_vetoes','risk_flags')} for r in finalists];summary['collection_error_count']=sum(bool(r['errors']) for r in collection);summary['cik_missing_count']=sum(r['cik'] is None for r in collection);(args.out_dir/'l3-summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8');print(json.dumps(summary,indent=2))
if __name__=='__main__':main()
