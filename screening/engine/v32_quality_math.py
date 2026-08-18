#!/usr/bin/env python3
"""Pure scoring math for the QRGF v3.2 shadow Structural Quality upper bound."""
from __future__ import annotations
from typing import Any, Mapping


def num(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def mean(values: list[float | None]) -> float | None:
    known = [float(v) for v in values if v is not None]
    return sum(known) / len(known) if known else None


def piecewise(value: Any, points: list[tuple[float, float]]) -> float | None:
    x = num(value)
    if x is None:
        return None
    pts = sorted((float(a), float(b)) for a, b in points)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (x - x0) * (y1 - y0) / (x1 - x0)
    return None


def _score(value: Any) -> float | None:
    x = num(value)
    return clamp(x) if x is not None else None


def _growth(value: Any) -> float | None:
    return piecewise(value, [(-20,10),(-10,25),(0,50),(10,68),(20,82),(30,92),(50,98)])


def _margin(value: Any) -> float | None:
    return piecewise(value, [(-30,10),(-10,25),(0,50),(5,62),(10,75),(20,90),(35,98)])


def _growth_margin(value: Any) -> float | None:
    return piecewise(value, [(-60,10),(-40,25),(-25,42),(-15,55),(0,72),(10,85),(20,95),(35,99)])


def _margin_change(value: Any) -> float | None:
    return piecewise(value, [(-15,10),(-8,25),(-3,45),(0,65),(3,80),(8,95),(15,99)])


def _debt(value: Any) -> float | None:
    x = num(value)
    if x is None:
        return None
    if x <= 0:
        return 98.0
    return piecewise(x, [(0,95),(1,90),(2,80),(3,65),(4,50),(6,25),(10,5)])


def _cash_debt(cash: Any, debt: Any) -> float | None:
    c, d = num(cash), num(debt)
    if c is None or d is None:
        return None
    if d <= 0:
        return 98.0
    return piecewise(c / d, [(0,20),(0.25,40),(0.5,60),(1,82),(2,95),(5,99)])


def _runway(value: Any) -> float | None:
    return piecewise(value, [(0,5),(6,25),(12,50),(18,70),(24,85),(36,95),(60,99)])


def _dilution(value: Any) -> float | None:
    x = num(value)
    if x is None:
        return None
    if x <= 0:
        return 98.0
    return piecewise(x, [(0,98),(2,92),(5,80),(8,65),(15,40),(25,15),(40,5)])


def _concentration(value: Any) -> float | None:
    return piecewise(value, [(0,98),(10,95),(25,85),(35,70),(50,50),(70,25),(100,5)])


def _guidance(value: Any) -> float | None:
    return {"raised":95.0,"improving":90.0,"stable":80.0,"maintained":80.0,"cut":52.0,"lowered":52.0,"withdrawn":35.0,"unknown":None,"":None}.get(str(value or "").strip().lower(),55.0)


def _bool(value: Any, true_score: float = 88.0, false_score: float = 30.0) -> float | None:
    if value is True:
        return true_score
    if value is False:
        return false_score
    return None


def _component_upper(values: list[float | None], unknown: float) -> float:
    if not values:
        return unknown
    return sum(float(v) if v is not None else unknown for v in values) / len(values)


def lane_components(f: Mapping[str, Any], lane: str, unknown: float = 100.0) -> dict[str, float]:
    if lane == "established_quality":
        return {
            "business_durability": _component_upper([_score(f.get("competitive_position_score")),_score(f.get("moat_quality_score")),_score(f.get("management_quality_score"))],unknown),
            "financial_resilience": _component_upper([_debt(f.get("net_debt_to_ebitda")),_cash_debt(f.get("cash"),f.get("debt"))],unknown),
            "earnings_cash_quality": _component_upper([_margin(f.get("operating_margin_pct")),_bool(f.get("net_income_positive")),_margin(f.get("fcf_margin_pct")),_bool(f.get("fcf_positive"))],unknown),
            "trajectory_quality": _component_upper([_growth(f.get("revenue_growth_pct")),_growth(f.get("revenue_cagr_3y_pct")),_growth(f.get("earnings_growth_pct")),_margin_change(f.get("operating_margin_change_pp_yoy")),_guidance(f.get("guidance_trend"))],unknown),
            "capital_discipline": _component_upper([_dilution(f.get("dilution_pct_yoy")),_score(f.get("capital_allocation_score")),_score(f.get("management_quality_score"))],unknown),
        }
    if lane == "recognized_growth":
        return {
            "business_durability": _component_upper([_score(f.get("competitive_position_score")),_score(f.get("moat_quality_score")),_score(f.get("management_quality_score")),_score(f.get("business_reality_score"))],unknown),
            "financial_resilience": _component_upper([_runway(f.get("cash_runway_months")),_cash_debt(f.get("cash"),f.get("debt")),_debt(f.get("net_debt_to_ebitda"))],unknown),
            "unit_economics": _component_upper([_growth_margin(f.get("operating_margin_pct")),_margin_change(f.get("operating_margin_change_pp_yoy")),_growth_margin(f.get("fcf_margin_pct")),_margin_change(f.get("fcf_margin_change_pp_yoy"))],unknown),
            "growth_quality": _component_upper([_growth(f.get("revenue_growth_pct")),_growth(f.get("revenue_cagr_3y_pct")),_guidance(f.get("guidance_trend"))],unknown),
            "capital_discipline": _component_upper([_dilution(f.get("dilution_pct_yoy")),_score(f.get("sales_efficiency_score")),_score(f.get("management_quality_score"))],unknown),
        }
    if lane == "cyclical":
        return {
            "business_durability": _component_upper([_score(f.get("competitive_position_score")),_score(f.get("moat_quality_score")),_score(f.get("management_quality_score"))],unknown),
            "financial_resilience": _component_upper([_debt(f.get("net_debt_to_ebitda")),_cash_debt(f.get("cash"),f.get("debt"))],unknown),
            "through_cycle_quality": _component_upper([_score(f.get("normalized_cycle_quality_score"))],unknown),
            "cash_generation": _component_upper([_score(f.get("normalized_fcf_quality_score")),_margin(f.get("fcf_margin_pct"))],unknown),
            "capital_discipline": _component_upper([_dilution(f.get("dilution_pct_yoy")),_score(f.get("capital_allocation_score")),_score(f.get("management_quality_score"))],unknown),
        }
    if lane == "bank":
        cet1 = _score(f.get("capital_quality_score"))
        if cet1 is None:
            cet1 = piecewise(f.get("cet1_ratio_pct"),[(5,10),(7,35),(9,60),(11,78),(13,90),(16,98)])
        npa = _score(f.get("asset_quality_score"))
        if npa is None and num(f.get("nonperforming_assets_pct")) is not None:
            npa = piecewise(f.get("nonperforming_assets_pct"),[(0,98),(0.5,92),(1,80),(2,60),(3,40),(5,15),(10,5)])
        profitability = _score(f.get("bank_profitability_score"))
        if profitability is None:
            profitability = mean([piecewise(f.get("roa_pct"),[(-1,5),(0,35),(0.7,65),(1,80),(1.5,95),(2,99)]),piecewise(f.get("roe_pct"),[(-10,5),(0,35),(7,65),(10,80),(15,95),(22,99)])])
        return {
            "franchise_durability": _component_upper([_score(f.get("competitive_position_score")),_score(f.get("management_quality_score")),_score(f.get("franchise_quality_score"))],unknown),
            "capital_strength": _component_upper([cet1],unknown),
            "asset_quality": _component_upper([npa],unknown),
            "profitability_quality": _component_upper([profitability],unknown),
            "funding_stability": _component_upper([_score(f.get("funding_stability_score"))],unknown),
        }
    if lane == "etf":
        diversification = _score(f.get("breadth_quality_score"))
        if diversification is None:
            diversification = mean([piecewise(f.get("largest_holding_weight_pct"),[(0,99),(5,95),(10,80),(15,65),(25,40),(40,15),(100,5)]),piecewise(f.get("top10_weight_pct"),[(0,99),(25,95),(40,85),(55,70),(70,50),(85,25),(100,5)])])
        resilience = _score(f.get("fund_structure_quality_score"))
        if resilience is None:
            resilience = piecewise(f.get("fund_aum"),[(50e6,30),(100e6,50),(500e6,70),(2e9,85),(10e9,95),(100e9,99)])
        return {
            "holdings_quality": _component_upper([_score(f.get("holdings_quality_score"))],unknown),
            "sector_fundamental_quality": _component_upper([_score(f.get("sector_fundamental_quality_score"))],unknown),
            "diversification": _component_upper([diversification],unknown),
            "fund_resilience": _component_upper([resilience],unknown),
            "concentration_control": _component_upper([_concentration(f.get("largest_holding_weight_pct")),_concentration(f.get("top10_weight_pct"))],unknown),
        }
    raise ValueError(f"unknown lane: {lane}")


def lane_upper_bound(facts: Mapping[str, Any], lane: str, model: Mapping[str, Any]) -> float:
    unknown = float(model.get("unknown_fact_upper_bound",100.0))
    components = lane_components(facts,lane,unknown)
    weights = model["lane_weights"][lane]
    total = sum(float(weights[k]) for k in weights)
    return round(sum(float(components[k])*float(weights[k]) for k in weights)/total,int(model.get("score_round_decimals",4)))


def possible_lanes(*, security_type: str, source_sector: str = "", sic: int | None = None, cik_resolved: bool = False, model: Mapping[str, Any]) -> list[str]:
    if str(security_type).lower()=="etf":
        return ["etf"]
    source_sector=str(source_sector or "").lower()
    bank = "bank" in source_sector
    cyclical = source_sector in {"energy","materials","metals","mining"}
    if sic is not None:
        hints=model.get("sic_lane_hints") or {}
        bank = bank or sic in {int(v) for v in hints.get("bank_exact",[])} or any(int(a)<=sic<=int(b) for a,b in hints.get("bank_ranges",[]))
        cyclical = cyclical or any(int(a)<=sic<=int(b) for a,b in hints.get("cyclical_ranges",[]))
    lanes=["established_quality","recognized_growth"]
    if cyclical:
        lanes.append("cyclical")
    if bank:
        lanes.append("bank")
    if not cik_resolved:
        lanes.extend(["cyclical","bank"])
    return sorted(set(lanes))


def quality_upper_bound(facts: Mapping[str, Any], lanes: list[str], model: Mapping[str, Any]) -> tuple[float,str,dict[str,float]]:
    bounds={lane:lane_upper_bound(facts,lane,model) for lane in lanes}
    lane=max(bounds,key=lambda x:(bounds[x],x))
    return float(bounds[lane]),lane,bounds


def progression_upper_bound(*, quality_upper: float, setup: Any, confidence: Any, model: Mapping[str, Any]) -> float:
    w=model["selection_weights"]
    setup_value=100.0 if num(setup) is None else clamp(float(setup))
    conf_value=100.0 if num(confidence) is None else clamp(float(confidence))
    score=(float(quality_upper)*float(w["structural_quality"])+setup_value*float(w["recovery_setup"])+conf_value*float(w["evidence_confidence"]))/100.0
    return round(score,int(model.get("score_round_decimals",4)))
