from __future__ import annotations
import statistics,sys
from pathlib import Path
from typing import Any,Mapping
SCRIPT_DIR=Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:sys.path.insert(0,str(SCRIPT_DIR))
from qrgf_l3_common import clamp,evidence_ids_for_field,normalize_evidence_records,required_evidence_links,tolerant_bool,tolerant_float
LANES={'established_quality','recognized_growth','cyclical','bank','etf','adr'}

def _valid_evidence(payload:Mapping[str,Any],required_fields:set[str]):
    records,errors=normalize_evidence_records(payload.get('evidence'));links,missing_fields=required_evidence_links(records,required_fields);combined=list(errors)+[f'missing evidence for {field}' for field in missing_fields];return bool(records) and not combined,combined,records,links,missing_fields

def _score_range(value:float|None,low:float,good:float,excellent:float,reverse:bool=False):
    if value is None:return None
    if reverse:
        if value<=low:return 95.0
        if value<=good:return 80.0
        if value<=excellent:return 55.0
        return 20.0
    if value>=excellent:return 95.0
    if value>=good:return 80.0
    if value>=low:return 55.0
    return 25.0

def evaluate(payload:dict[str,Any])->dict[str,Any]:
    ticker=str(payload.get('ticker') or '').strip().upper()
    if not ticker:raise ValueError('ticker is required')
    lane=str(payload.get('quality_lane') or 'established_quality').strip().lower()
    if lane not in LANES:raise ValueError(f'unsupported quality_lane {lane}')
    fundamentals=payload.get('fundamentals') or {}
    if not isinstance(fundamentals,Mapping):raise ValueError('fundamentals must be an object')
    present={f'fundamentals.{n}' for n,v in fundamentals.items() if v is not None and str(v).strip()!=''}
    clearance={'fundamentals.bankruptcy_risk','fundamentals.going_concern_warning','fundamentals.accounting_investigation','fundamentals.material_restatement','fundamentals.delisting_risk','fundamentals.binary_business_event'}
    evidence_ok,evidence_errors,evidence_records,evidence_links,missing_evidence_fields=_valid_evidence(payload,present.union(clearance))
    revenue_growth=tolerant_float(fundamentals.get('revenue_growth_pct'));earnings_growth=tolerant_float(fundamentals.get('earnings_growth_pct'));operating_margin=tolerant_float(fundamentals.get('operating_margin_pct'));fcf_margin=tolerant_float(fundamentals.get('fcf_margin_pct'));cash=tolerant_float(fundamentals.get('cash'));debt=tolerant_float(fundamentals.get('debt'));net_debt_ebitda=tolerant_float(fundamentals.get('net_debt_to_ebitda'));dilution=tolerant_float(fundamentals.get('dilution_pct_yoy'));cash_runway=tolerant_float(fundamentals.get('cash_runway_months'));customer_concentration=tolerant_float(fundamentals.get('customer_concentration_pct'));product_concentration=tolerant_float(fundamentals.get('product_concentration_pct'));profitable=tolerant_bool(fundamentals.get('net_income_positive'));fcf_positive=tolerant_bool(fundamentals.get('fcf_positive'));guidance=str(fundamentals.get('guidance_trend') or 'unknown').strip().lower()
    hard=[]
    for field,veto in [('bankruptcy_risk','bankruptcy_risk'),('going_concern_warning','going_concern_warning'),('accounting_investigation','accounting_investigation'),('material_restatement','material_restatement'),('delisting_risk','delisting_risk'),('binary_business_event','binary_business_event')]:
        if tolerant_bool(fundamentals.get(field)) is True:hard.append(veto)
    if dilution is not None and dilution>=25 and fcf_positive is not True:hard.append('chronic_dilution')
    if guidance in {'withdrawn_structural','severe_cut_structural'}:hard.append('structural_guidance_cut_without_stabilization')
    risk=[]
    if dilution is not None and dilution>=8:risk.append('elevated_dilution')
    if customer_concentration is not None and customer_concentration>=35:risk.append('single_customer_concentration')
    if product_concentration is not None and product_concentration>=60:risk.append('single_product_concentration')
    if net_debt_ebitda is not None and net_debt_ebitda>=4:risk.append('debt_maturity_risk')
    if guidance in {'cut','lowered'}:risk.append('guidance_reduction')
    components={'growth_trajectory':None,'profitability_and_margins':None,'cash_generation':None,'balance_sheet':None,'dilution_and_concentration':None,'guidance_durability':None}
    growth_values=[v for v in (revenue_growth,earnings_growth) if v is not None]
    if growth_values:components['growth_trajectory']=clamp(55+statistics.mean(growth_values)*1.2)
    if operating_margin is not None:
        components['profitability_and_margins']=clamp(45+operating_margin*1.8)
        if profitable is True:components['profitability_and_margins']=min(100.0,components['profitability_and_margins']+10)
        elif profitable is False:components['profitability_and_margins']=max(0.0,components['profitability_and_margins']-20)
    if fcf_margin is not None:
        components['cash_generation']=clamp(50+fcf_margin*2.0)
        if fcf_positive is False:components['cash_generation']=max(0.0,components['cash_generation']-30)
    elif fcf_positive is not None:components['cash_generation']=75.0 if fcf_positive else 25.0
    if net_debt_ebitda is not None:components['balance_sheet']=_score_range(net_debt_ebitda,0.5,2.0,4.0,reverse=True)
    elif cash is not None and debt is not None:components['balance_sheet']=90.0 if cash>=debt else 70.0 if cash>=debt*0.5 else 45.0
    elif cash_runway is not None:components['balance_sheet']=clamp(35+cash_runway*2.0)
    penalty=0.0
    if dilution is not None:penalty+=max(0.0,dilution-3)*2
    if customer_concentration is not None:penalty+=max(0.0,customer_concentration-25)*0.7
    if product_concentration is not None:penalty+=max(0.0,product_concentration-50)*0.4
    if any(v is not None for v in (dilution,customer_concentration,product_concentration)):components['dilution_and_concentration']=clamp(95-penalty)
    components['guidance_durability']={'raised':95.0,'improving':90.0,'stable':78.0,'maintained':78.0,'cut':45.0,'lowered':45.0,'withdrawn':25.0,'unknown':None}.get(guidance,40.0)
    if lane=='recognized_growth':
        if profitable is False and (cash_runway or 0)>=24 and (revenue_growth or 0)>=20 and components['profitability_and_margins'] is not None:components['profitability_and_margins']=max(55.0,components['profitability_and_margins'])
        if fcf_positive is False and (cash_runway or 0)<18:risk.append('cash_runway_risk')
    elif lane=='cyclical':
        normalized=tolerant_float(fundamentals.get('normalized_cycle_quality_score'))
        if normalized is not None:components['growth_trajectory']=clamp(normalized)
    elif lane=='bank':
        vals=[v for v in (tolerant_float(fundamentals.get('capital_quality_score')),tolerant_float(fundamentals.get('asset_quality_score')),tolerant_float(fundamentals.get('funding_stability_score'))) if v is not None]
        if vals:components['balance_sheet']=statistics.mean(vals)
    elif lane=='etf':
        sq=tolerant_float(fundamentals.get('sector_fundamental_quality_score'));hq=tolerant_float(fundamentals.get('holdings_quality_score'))
        if sq is not None:components['growth_trajectory']=clamp(sq)
        if hq is not None:components['profitability_and_margins']=clamp(hq)
    known=[v for v in components.values() if v is not None];coverage=round(100.0*len(known)/len(components),2);score=round(statistics.mean(known),2) if known else None;missing=[n for n,v in components.items() if v is None]
    missing_clearance=[n for n in ('bankruptcy_risk','going_concern_warning','accounting_investigation','material_restatement','delisting_risk','binary_business_event') if tolerant_bool(fundamentals.get(n)) is None]
    component_fields={'growth_trajectory':('revenue_growth_pct','earnings_growth_pct','normalized_cycle_quality_score','sector_fundamental_quality_score'),'profitability_and_margins':('operating_margin_pct','net_income_positive','holdings_quality_score'),'cash_generation':('fcf_margin_pct','fcf_positive'),'balance_sheet':('cash','debt','net_debt_to_ebitda','cash_runway_months','capital_quality_score','asset_quality_score','funding_stability_score'),'dilution_and_concentration':('dilution_pct_yoy','customer_concentration_pct','product_concentration_pct'),'guidance_durability':('guidance_trend',)}
    comp_ids={}
    for comp,fields in component_fields.items():
        ids=set()
        for field in fields:ids.update(evidence_ids_for_field(evidence_records,f'fundamentals.{field}'))
        comp_ids[comp]=sorted(ids)
    if hard:status='rejected'
    elif not evidence_ok or missing_clearance or coverage<50:status='recheck'
    elif risk or coverage<80:status='conditional'
    else:status='pass'
    return {'ticker':ticker,'depth':'L3','quality_lane':lane,'l3_status':status,'l3_score':score,'l3_coverage_pct':coverage,'fundamental_components':components,'hard_vetoes':sorted(set(hard)),'risk_flags':sorted(set(risk)),'missing_fundamental_components':missing,'missing_clearance_checks':missing_clearance,'missing_evidence_fields':missing_evidence_fields,'evidence_validated':evidence_ok,'trusted_evidence_ids':[r['evidence_id'] for r in evidence_records] if evidence_ok else [],'evidence':evidence_records,'evidence_links':evidence_links,'component_evidence_ids':{'opportunity':comp_ids},'evidence_errors':evidence_errors,'next_required_check':'run_L4_events_and_drop_cause' if status in {'pass','conditional'} else 'resolve_L3_missing_or_veto'}
