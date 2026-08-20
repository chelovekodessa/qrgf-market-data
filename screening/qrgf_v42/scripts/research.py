#!/usr/bin/env python3
"""Deterministic L3-L5 evaluators over evidence-bound canonical payloads."""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Callable, Mapping

from common import clamp, load_connectors, load_policy, mean, number, parse_time, piecewise, truth, weighted_score
from contracts import validate as validate_contract
from evidence import bind
from eligibility import validate as validate_instrument
from scoring import score_dimensions


def classify_lane(payload: Mapping[str, Any]) -> dict[str, str]:
    security_type = str(payload.get("security_type") or "").lower()
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    sector = str(payload.get("sector") or "").lower()
    if security_type == "etf":
        lane, reason = "etf", "instrument_type"
    elif any(facts.get(name) is not None for name in ("cet1_ratio_pct", "nonperforming_assets_pct", "funding_stability_score")) or "bank" in sector:
        lane, reason = "bank", "bank_economics"
    elif facts.get("normalized_cycle_quality_score") is not None or sector in {"energy", "materials", "metals", "mining"}:
        lane, reason = "cyclical", "cycle_evidence"
    elif (number(facts.get("revenue_growth_pct")) or -999) >= 20 or facts.get("business_reality_score") is not None or facts.get("sales_efficiency_score") is not None:
        lane, reason = "recognized_growth", "growth_economics"
    else:
        lane, reason = "established_quality", "default_operating_company"
    return {
        "quality_lane": lane,
        "listing_overlay": "adr" if security_type == "adr" else "none",
        "lane_resolution": reason,
    }


def _score(value: Any) -> float | None:
    numeric = number(value)
    return clamp(numeric) if numeric is not None else None


def _growth(value: Any) -> float | None:
    return piecewise(value, [(-20, 10), (-10, 25), (0, 50), (10, 68), (20, 82), (30, 92), (50, 98)])


def _margin(value: Any) -> float | None:
    return piecewise(value, [(-30, 10), (-10, 25), (0, 50), (5, 62), (10, 75), (20, 90), (35, 98)])


def _growth_margin(value: Any) -> float | None:
    return piecewise(value, [(-60, 10), (-40, 25), (-25, 42), (-15, 55), (0, 72), (10, 85), (20, 95), (35, 99)])


def _margin_change(value: Any) -> float | None:
    return piecewise(value, [(-15, 10), (-8, 25), (-3, 45), (0, 65), (3, 80), (8, 95), (15, 99)])


def _debt(value: Any) -> float | None:
    numeric = number(value)
    if numeric is None:
        return None
    if numeric <= 0:
        return 98.0
    return piecewise(numeric, [(0, 95), (1, 90), (2, 80), (3, 65), (4, 50), (6, 25), (10, 5)])


def _cash_debt(cash: Any, debt: Any) -> float | None:
    cash_value, debt_value = number(cash), number(debt)
    if cash_value is None or debt_value is None:
        return None
    if debt_value <= 0:
        return 98.0
    return piecewise(cash_value / debt_value, [(0, 20), (0.25, 40), (0.5, 60), (1, 82), (2, 95), (5, 99)])


def _runway(value: Any) -> float | None:
    return piecewise(value, [(0, 5), (6, 25), (12, 50), (18, 70), (24, 85), (36, 95), (60, 99)])


def _dilution(value: Any) -> float | None:
    numeric = number(value)
    if numeric is None:
        return None
    if numeric <= 0:
        return 98.0
    return piecewise(numeric, [(0, 98), (2, 92), (5, 80), (8, 65), (15, 40), (25, 15), (40, 5)])


def _concentration(value: Any) -> float | None:
    return piecewise(value, [(0, 98), (10, 95), (25, 85), (35, 70), (50, 50), (70, 25), (100, 5)])


def _guidance(value: Any) -> float | None:
    return {
        "raised": 95.0, "improving": 90.0, "stable": 80.0, "maintained": 80.0,
        "cut": 52.0, "lowered": 52.0, "withdrawn": 35.0, "unknown": None, "": None,
    }.get(str(value or "").strip().lower(), 55.0)


def _bool_score(value: Any, true_score: float = 88.0, false_score: float = 30.0) -> float | None:
    parsed = truth(value)
    return true_score if parsed is True else false_score if parsed is False else None


def _component(values: list[tuple[str, float | None]]) -> dict[str, Any]:
    known = [value for _, value in values if value is not None]
    return {
        "score": round(mean(known), 2) if known else None,
        "fact_fields": [f"facts.{name}" for name, value in values if value is not None],
        "expected_fact_count": len(values),
        "known_fact_count": len(known),
        "fact_coverage_pct": round(100 * len(known) / len(values), 2) if values else 0.0,
    }


def _established(f: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "business_durability": _component([
            ("competitive_position_score", _score(f.get("competitive_position_score"))),
            ("moat_quality_score", _score(f.get("moat_quality_score"))),
            ("management_quality_score", _score(f.get("management_quality_score"))),
        ]),
        "financial_resilience": _component([
            ("net_debt_to_ebitda", _debt(f.get("net_debt_to_ebitda"))),
            ("cash_debt", _cash_debt(f.get("cash"), f.get("debt"))),
        ]),
        "earnings_cash_quality": _component([
            ("operating_margin_pct", _margin(f.get("operating_margin_pct"))),
            ("net_income_positive", _bool_score(f.get("net_income_positive"))),
            ("fcf_margin_pct", _margin(f.get("fcf_margin_pct"))),
            ("fcf_positive", _bool_score(f.get("fcf_positive"))),
        ]),
        "trajectory_quality": _component([
            ("revenue_growth_pct", _growth(f.get("revenue_growth_pct"))),
            ("revenue_cagr_3y_pct", _growth(f.get("revenue_cagr_3y_pct"))),
            ("earnings_growth_pct", _growth(f.get("earnings_growth_pct"))),
            ("operating_margin_change_pp_yoy", _margin_change(f.get("operating_margin_change_pp_yoy"))),
            ("guidance_trend", _guidance(f.get("guidance_trend"))),
        ]),
        "capital_discipline": _component([
            ("dilution_pct_yoy", _dilution(f.get("dilution_pct_yoy"))),
            ("capital_allocation_score", _score(f.get("capital_allocation_score"))),
            ("management_quality_score", _score(f.get("management_quality_score"))),
        ]),
    }


def _recognized_growth(f: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "business_durability": _component([
            ("competitive_position_score", _score(f.get("competitive_position_score"))),
            ("moat_quality_score", _score(f.get("moat_quality_score"))),
            ("management_quality_score", _score(f.get("management_quality_score"))),
            ("business_reality_score", _score(f.get("business_reality_score"))),
        ]),
        "financial_resilience": _component([
            ("cash_runway_months", _runway(f.get("cash_runway_months"))),
            ("cash_debt", _cash_debt(f.get("cash"), f.get("debt"))),
            ("net_debt_to_ebitda", _debt(f.get("net_debt_to_ebitda"))),
        ]),
        "unit_economics": _component([
            ("operating_margin_pct", _growth_margin(f.get("operating_margin_pct"))),
            ("operating_margin_change_pp_yoy", _margin_change(f.get("operating_margin_change_pp_yoy"))),
            ("fcf_margin_pct", _growth_margin(f.get("fcf_margin_pct"))),
            ("fcf_margin_change_pp_yoy", _margin_change(f.get("fcf_margin_change_pp_yoy"))),
        ]),
        "growth_quality": _component([
            ("revenue_growth_pct", _growth(f.get("revenue_growth_pct"))),
            ("revenue_cagr_3y_pct", _growth(f.get("revenue_cagr_3y_pct"))),
            ("guidance_trend", _guidance(f.get("guidance_trend"))),
        ]),
        "capital_discipline": _component([
            ("dilution_pct_yoy", _dilution(f.get("dilution_pct_yoy"))),
            ("sales_efficiency_score", _score(f.get("sales_efficiency_score"))),
            ("management_quality_score", _score(f.get("management_quality_score"))),
        ]),
    }


def _cyclical(f: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "business_durability": _component([
            ("competitive_position_score", _score(f.get("competitive_position_score"))),
            ("moat_quality_score", _score(f.get("moat_quality_score"))),
            ("management_quality_score", _score(f.get("management_quality_score"))),
        ]),
        "financial_resilience": _component([
            ("net_debt_to_ebitda", _debt(f.get("net_debt_to_ebitda"))),
            ("cash_debt", _cash_debt(f.get("cash"), f.get("debt"))),
        ]),
        "through_cycle_quality": _component([("normalized_cycle_quality_score", _score(f.get("normalized_cycle_quality_score")))]),
        "cash_generation": _component([
            ("normalized_fcf_quality_score", _score(f.get("normalized_fcf_quality_score"))),
            ("fcf_margin_pct", _margin(f.get("fcf_margin_pct"))),
        ]),
        "capital_discipline": _component([
            ("dilution_pct_yoy", _dilution(f.get("dilution_pct_yoy"))),
            ("capital_allocation_score", _score(f.get("capital_allocation_score"))),
            ("management_quality_score", _score(f.get("management_quality_score"))),
        ]),
    }


def _bank(f: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    cet1 = _score(f.get("capital_quality_score")) or piecewise(f.get("cet1_ratio_pct"), [(5, 10), (7, 35), (9, 60), (11, 78), (13, 90), (16, 98)])
    npa = _score(f.get("asset_quality_score"))
    if npa is None and number(f.get("nonperforming_assets_pct")) is not None:
        npa = piecewise(f.get("nonperforming_assets_pct"), [(0, 98), (0.5, 92), (1, 80), (2, 60), (3, 40), (5, 15), (10, 5)])
    profitability = _score(f.get("bank_profitability_score")) or mean([
        piecewise(f.get("roa_pct"), [(-1, 5), (0, 35), (0.7, 65), (1, 80), (1.5, 95), (2, 99)]),
        piecewise(f.get("roe_pct"), [(-10, 5), (0, 35), (7, 65), (10, 80), (15, 95), (22, 99)]),
    ])
    return {
        "franchise_durability": _component([
            ("competitive_position_score", _score(f.get("competitive_position_score"))),
            ("management_quality_score", _score(f.get("management_quality_score"))),
            ("franchise_quality_score", _score(f.get("franchise_quality_score"))),
        ]),
        "capital_strength": _component([("capital_strength", cet1)]),
        "asset_quality": _component([("asset_quality", npa)]),
        "profitability_quality": _component([("profitability_quality", profitability)]),
        "funding_stability": _component([("funding_stability_score", _score(f.get("funding_stability_score")))]),
    }


def _etf(f: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    diversification = _score(f.get("breadth_quality_score")) or mean([
        piecewise(f.get("largest_holding_weight_pct"), [(0, 99), (5, 95), (10, 80), (15, 65), (25, 40), (40, 15), (100, 5)]),
        piecewise(f.get("top10_weight_pct"), [(0, 99), (25, 95), (40, 85), (55, 70), (70, 50), (85, 25), (100, 5)]),
    ])
    resilience = _score(f.get("fund_structure_quality_score")) or piecewise(f.get("fund_aum"), [(50e6, 30), (100e6, 50), (500e6, 70), (2e9, 85), (10e9, 95), (100e9, 99)])
    return {
        "holdings_quality": _component([("holdings_quality_score", _score(f.get("holdings_quality_score")))]),
        "sector_fundamental_quality": _component([("sector_fundamental_quality_score", _score(f.get("sector_fundamental_quality_score")))]),
        "diversification": _component([("diversification", diversification)]),
        "fund_resilience": _component([("fund_resilience", resilience)]),
        "concentration_control": _component([
            ("largest_holding_weight_pct", _concentration(f.get("largest_holding_weight_pct"))),
            ("top10_weight_pct", _concentration(f.get("top10_weight_pct"))),
        ]),
    }


LANE_SCORERS: dict[str, Callable[[Mapping[str, Any]], dict[str, dict[str, Any]]]] = {
    "established_quality": _established,
    "recognized_growth": _recognized_growth,
    "cyclical": _cyclical,
    "bank": _bank,
    "etf": _etf,
}


def evaluate_l3(payload: Mapping[str, Any]) -> dict[str, Any]:
    validate_contract("l3-input", payload)
    ticker = str(payload.get("ticker") or "").upper()
    contract_id = str(payload.get("contract_id") or "")
    if not ticker or not contract_id:
        raise ValueError("L3 requires ticker and contract_id")
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    lane_info = classify_lane(payload)
    lane = lane_info["quality_lane"]
    overlay = lane_info["listing_overlay"]
    policy = load_policy()
    l3_policy = policy["l3"]
    components = LANE_SCORERS[lane](facts)
    component_scores = {name: value["score"] for name, value in components.items()}
    score, weighted_component_coverage = weighted_score(component_scores, l3_policy["weights"][lane])
    total_expected = sum(int(value["expected_fact_count"]) for value in components.values())
    total_known = sum(int(value["known_fact_count"]) for value in components.values())
    fact_coverage = round(100 * total_known / total_expected, 2) if total_expected else 0.0
    coverage = min(weighted_component_coverage, fact_coverage)

    clearances = payload.get("clearances") if isinstance(payload.get("clearances"), Mapping) else {}
    required_clearances = list(l3_policy["required_clearances"][lane])
    if overlay == "adr":
        required_clearances.extend(l3_policy["adr_overlay_clearances"])
    evidence_fields = [f"facts.{name}" for name, value in facts.items() if value is not None]
    evidence_fields.extend(f"clearances.{name}" for name in required_clearances)
    evidence_payload = {**dict(payload), "facts": dict(facts), "clearances": dict(clearances)}
    evidence_result = bind(evidence_payload, evidence_fields)
    if facts.get("cash") is not None and facts.get("debt") is not None:
        linked = {row["evidence_id"]: row for row in evidence_result["records"]}
        cash_rows = [linked[ident] for ident in evidence_result["links"].get("facts.cash", []) if ident in linked]
        debt_rows = [linked[ident] for ident in evidence_result["links"].get("facts.debt", []) if ident in linked]
        cash_periods = {str(row.get("period") or "") for row in cash_rows}
        debt_periods = {str(row.get("period") or "") for row in debt_rows}
        cash_units = {str(row.get("unit") or "") for row in cash_rows}
        debt_units = {str(row.get("unit") or "") for row in debt_rows}
        if not cash_rows or not debt_rows or cash_periods != debt_periods or cash_units != debt_units:
            evidence_result["errors"].append("cash/debt derivation requires the same period and unit")
            evidence_result["valid"] = False

    missing_clearances: list[str] = []
    instrument = validate_instrument(payload)
    hard_vetoes: list[str] = list(instrument["hard_vetoes"])
    for name in sorted(set(required_clearances)):
        value = str(clearances.get(name) or "unknown").lower()
        if value == "triggered":
            hard_vetoes.append(l3_policy["clearance_veto_map"][name])
        elif value != "clear":
            missing_clearances.append(name)
    risk_flags: list[str] = []
    dilution_value = number(facts.get("dilution_pct_yoy"))
    if dilution_value is not None and dilution_value >= 8:
        risk_flags.append("elevated_dilution")
    leverage_value = number(facts.get("net_debt_to_ebitda"))
    if leverage_value is not None and leverage_value >= 4:
        risk_flags.append("high_leverage")
    if str(facts.get("guidance_trend") or "").lower() in {"cut", "lowered", "withdrawn"}:
        risk_flags.append("guidance_reduction_requires_L4_context")
    if lane == "recognized_growth" and number(facts.get("cash_runway_months")) is not None and float(facts["cash_runway_months"]) < 18:
        risk_flags.append("cash_runway_risk")

    collection = str(payload.get("collection_status") or "ready")
    evidence_problem = not evidence_result["valid"] or missing_clearances or coverage < l3_policy["minimum_coverage_pct"] or score is None
    if hard_vetoes:
        status, reason = "rejected", "hard_veto"
    elif collection == "global_blocked":
        status, reason = "recheck", "global_transport_blocked"
    elif evidence_problem:
        status, reason = ("insufficient_data", "sources_exhausted") if collection == "exhausted" else ("recheck", "evidence_or_coverage_incomplete")
    elif score < l3_policy["minimum_quality_score"]:
        status, reason = "rejected", "below_minimum_fundamental_quality"
    elif risk_flags or coverage < l3_policy["pass_coverage_pct"]:
        status, reason = "conditional", "quality_passed_with_non_veto_risk_or_partial_coverage"
    else:
        status, reason = "pass", "fundamental_quality_gate_passed"
    eligible = status in {"pass", "conditional"} and score is not None and score >= l3_policy["minimum_quality_score"] and not hard_vetoes
    financial_name = "financial_resilience" if "financial_resilience" in component_scores else "capital_strength" if lane == "bank" else "fund_resilience"
    return {
        "ticker": ticker,
        "contract_id": contract_id,
        "security_type": str(payload.get("security_type") or ""),
        "depth": "L3",
        **lane_info,
        "l3_status": status,
        "l3_status_reason": reason,
        "l3_score": score,
        "l3_coverage_pct": coverage,
        "component_coverage_pct": weighted_component_coverage,
        "fact_coverage_pct": fact_coverage,
        "fundamental_eligible": eligible,
        "fundamental_components": component_scores,
        "fundamental_component_details": components,
        "financial_durability_score": component_scores.get(financial_name),
        "clearance_results": {name: str(clearances.get(name) or "unknown").lower() for name in sorted(set(required_clearances))},
        "missing_clearance_checks": missing_clearances,
        "hard_vetoes": sorted(set(hard_vetoes)),
        "instrument_validation": instrument,
        "risk_flags": sorted(set(risk_flags)),
        "evidence_validated": evidence_result["valid"] and not missing_clearances,
        "evidence": evidence_result["records"],
        "evidence_links": evidence_result["links"],
        "evidence_errors": evidence_result["errors"],
        "collection_status": collection,
        **({"identity_resolution_status": str(payload.get("identity_resolution_status"))} if payload.get("identity_resolution_status") is not None else {}),
        **({"resolved_contract_id": str(payload.get("resolved_contract_id"))} if payload.get("resolved_contract_id") is not None else {}),
    }


def _analyst_actions(actions: Any, analysis_time: dt.datetime, last_earnings: dt.datetime | None) -> list[dict[str, Any]]:
    if not isinstance(actions, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for raw in actions:
        if not isinstance(raw, Mapping):
            continue
        firm = str(raw.get("bank") or raw.get("firm") or "").strip()
        analyst = str(raw.get("analyst") or "").strip()
        rating = str(raw.get("rating") or raw.get("action") or "").strip()
        target = number(raw.get("new_target", raw.get("target")))
        date = parse_time(raw.get("date") or raw.get("action_date"))
        source = str(raw.get("source") or raw.get("source_id") or "").strip()
        if not firm or not analyst or not source or date is None or date > analysis_time + dt.timedelta(days=1):
            continue
        key = (firm.lower(), analyst.lower(), date.date().isoformat(), rating.lower(), str(target), source)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "firm": firm, "analyst": analyst, "rating": rating or None, "new_target": target,
            "previous_target": number(raw.get("previous_target", raw.get("old_target"))),
            "currency": str(raw.get("currency") or "USD").upper(), "date": date.date().isoformat(),
            "source": source, "post_earnings": bool(last_earnings and date >= last_earnings),
            "age_days": (analysis_time.date() - date.date()).days,
        })
    return sorted(result, key=lambda row: (row["date"], row["firm"], row["analyst"]), reverse=True)


def evaluate_l4(payload: Mapping[str, Any]) -> dict[str, Any]:
    validate_contract("l4-input", payload)
    ticker, contract_id = str(payload.get("ticker") or "").upper(), str(payload.get("contract_id") or "")
    if not ticker or not contract_id:
        raise ValueError("L4 requires ticker and contract_id")
    events = payload.get("events") if isinstance(payload.get("events"), Mapping) else {}
    analysis_time = parse_time(payload.get("analysis_time")) or dt.datetime.now(dt.timezone.utc)
    is_etf = str(payload.get("security_type") or "").lower() == "etf" or str((payload.get("l3") or {}).get("quality_lane") or "") == "etf"
    policy = load_policy()["l4"]
    required = ["events.cause_classification", "events.next_review_date"]
    if is_etf:
        required += ["events.holdings_change_status", "events.fund_structure_status", "events.sector_catalyst_status"]
    else:
        required += ["events.last_earnings_date", "events.guidance_status", "events.next_earnings_date"]
    evidence_fields = sorted(set(required + [f"events.{name}" for name, value in events.items() if value is not None]))
    if not is_etf and policy.get("fresh_analyst_actions_required_for_operating_company") is True:
        evidence_fields.append("analyst_actions")
    elif isinstance(payload.get("analyst_actions"), list) and payload.get("analyst_actions"):
        evidence_fields.append("analyst_actions")
    evidence_result = bind({**dict(payload), "events": dict(events)}, evidence_fields)
    if "analyst_actions" in evidence_fields:
        action_records = [row for row in evidence_result["records"] if row.get("field") == "analyst_actions"]
        freshness_hours = float(policy["analyst_action_freshness_hours"])
        if action_records and any(parse_time(row.get("retrieved_at")) is None or (analysis_time - parse_time(row.get("retrieved_at"))).total_seconds() > freshness_hours * 3600 for row in action_records):
            evidence_result["errors"].append("analyst_actions evidence is stale")
            evidence_result["valid"] = False

    cause = str(events.get("cause_classification") or "unknown").lower()
    allowed_causes = {"temporary_supported", "mixed", "structural_damage", "market_or_sector_driven", "valuation_reset", "event_uncertainty", "unknown"}
    if cause not in allowed_causes:
        cause = "unknown"
    hard_vetoes: list[str] = []
    if cause == "structural_damage" or any(truth(events.get(name)) is True for name in ("bankruptcy_risk", "going_concern_warning", "accounting_investigation", "material_restatement", "delisting_risk")):
        hard_vetoes.append("structural_thesis_damage")
    components: dict[str, float | None]
    if is_etf:
        components = {
            "fund_structure": _score(events.get("fund_structure_score")),
            "holdings_quality_change": _score(events.get("holdings_change_score")),
            "sector_catalyst": _score(events.get("sector_catalyst_score")),
            "temporary_drop_support": {"temporary_supported": 90.0, "market_or_sector_driven": 82.0, "valuation_reset": 75.0, "event_uncertainty": 50.0, "mixed": 40.0, "structural_damage": 5.0, "unknown": None}[cause],
        }
    else:
        surprise = mean([number(events.get("revenue_surprise_pct")), number(events.get("eps_surprise_pct"))])
        earnings = clamp(60 + surprise * 3) if surprise is not None else None
        if earnings is not None and number(events.get("margin_change_bps")) is not None:
            earnings = clamp(earnings + float(events["margin_change_bps"]) / 50)
        revisions = mean([number(events.get("guidance_midpoint_change_pct")), number(events.get("estimate_revision_90d_pct"))])
        last_earnings = parse_time(events.get("last_earnings_date"))
        actions = _analyst_actions(payload.get("analyst_actions"), analysis_time, last_earnings)
        fresh_actions = [row for row in actions if row["age_days"] <= int(policy["analyst_event_window_days"]) and (row["post_earnings"] or last_earnings is None)]
        targets = [float(row["new_target"]) for row in fresh_actions if row.get("new_target") is not None and row.get("currency") == str(policy.get("target_currency") or "USD").upper()]
        analyst_status = "missing"
        if len(fresh_actions) >= int(policy.get("minimum_fresh_analyst_actions_for_complete") or 5):
            analyst_status = "fresh_complete"
        elif fresh_actions:
            analyst_status = "fresh_partial"
        elif actions:
            analyst_status = "stale"
        components = {
            "earnings_quality": earnings,
            "guidance_and_revisions": clamp(55 + revisions * 2.5) if revisions is not None else None,
            "temporary_drop_support": {"temporary_supported": 90.0, "market_or_sector_driven": 82.0, "valuation_reset": 75.0, "event_uncertainty": 50.0, "mixed": 40.0, "structural_damage": 5.0, "unknown": None}[cause],
            "catalyst_quality": 85.0 if truth(events.get("catalyst_confirmed")) is True else 35.0 if truth(events.get("catalyst_confirmed")) is False else None,
            "analyst_confirmation": min(90.0, 50 + 6 * len(fresh_actions)) if fresh_actions else None,
        }
    known = [value for value in components.values() if value is not None]
    score = round(mean(known), 2) if known else None
    coverage = round(100 * len(known) / len(components), 2)
    missing = [field for field in required if field in evidence_result["missing_fields"]]
    if cause == "unknown":
        missing.append("events.cause_classification")
    if is_etf:
        analyst_status = "not_applicable"
        actions = []
        fresh_actions = []
        targets = []
    elif analyst_status in {"missing", "stale"}:
        missing.append("fresh_analyst_actions")
    if hard_vetoes:
        status = "rejected"
    elif not evidence_result["valid"] or missing or coverage < policy["minimum_coverage_pct"] or score is None:
        status = "recheck"
    elif score < policy["minimum_score"]:
        status = "rejected"
    elif cause == "mixed" or analyst_status == "fresh_partial" or coverage < policy["pass_coverage_pct"]:
        status = "conditional"
    else:
        status = "pass"
    target_median = statistics.median(targets) if targets else None
    target_average = statistics.mean(targets) if targets else None
    target_min = min(targets) if targets else None
    target_max = max(targets) if targets else None
    target_range = (target_max - target_min) if target_min is not None and target_max is not None else None
    target_range_pct = (100.0 * target_range / target_median) if target_range is not None and target_median not in (None, 0) else None
    target_dispersion_pct = (100.0 * statistics.pstdev(targets) / target_median) if len(targets) >= 2 and target_median not in (None, 0) else None
    return {
        "ticker": ticker, "contract_id": contract_id, "security_type": str(payload.get("security_type") or ""), "depth": "L4", "l4_status": status,
        "l4_score": score, "l4_coverage_pct": coverage, "cause_of_drop": cause,
        "event_components": components, "hard_vetoes": sorted(set(hard_vetoes)),
        "missing_checks": sorted(set(missing)), "evidence_validated": evidence_result["valid"],
        "analyst_data_status": analyst_status, "analyst_actions": actions,
        "fresh_post_earnings_analyst_actions": fresh_actions,
        "fresh_target_count": len(targets),
        "fresh_target_median": round(float(target_median), 4) if target_median is not None else None,
        "fresh_target_average": round(float(target_average), 4) if target_average is not None else None,
        "fresh_target_min": round(float(target_min), 4) if target_min is not None else None,
        "fresh_target_max": round(float(target_max), 4) if target_max is not None else None,
        "fresh_target_range": round(float(target_range), 4) if target_range is not None else None,
        "fresh_target_range_pct": round(float(target_range_pct), 4) if target_range_pct is not None else None,
        "fresh_target_dispersion_pct": round(float(target_dispersion_pct), 4) if target_dispersion_pct is not None else None,
        "evidence": evidence_result["records"], "evidence_links": evidence_result["links"],
        "evidence_errors": evidence_result["errors"], "instrument_path": "etf" if is_etf else "operating_company",
    }


def evaluate_l5(payload: Mapping[str, Any]) -> dict[str, Any]:
    validate_contract("l5-input", payload)
    ticker, contract_id = str(payload.get("ticker") or "").upper(), str(payload.get("contract_id") or "")
    if not ticker or not contract_id:
        raise ValueError("L5 requires ticker and contract_id")
    quote = payload.get("quote") if isinstance(payload.get("quote"), Mapping) else {}
    technicals = payload.get("technicals") if isinstance(payload.get("technicals"), Mapping) else {}
    recovery = payload.get("historical_recovery") if isinstance(payload.get("historical_recovery"), Mapping) else {}
    market = payload.get("market_context") if isinstance(payload.get("market_context"), Mapping) else {}
    l3 = payload.get("l3") if isinstance(payload.get("l3"), Mapping) else {}
    l4 = payload.get("l4") if isinstance(payload.get("l4"), Mapping) else {}
    now = parse_time(payload.get("analysis_time")) or dt.datetime.now(dt.timezone.utc)
    security_type = str(payload.get("security_type") or "").lower()
    market_policy = load_policy().get("l5_market_context") or {}
    required = [
        "quote.last", "quote.bid", "quote.ask", "quote.as_of", "quote.bid_ask_as_of", "quote.avg_dollar_volume",
        "technicals.historical_volatility_30d_pct", "technicals.atr_14_pct", "technicals.drawdown_52w_pct",
        "technicals.data_sufficiency", "technicals.stabilization", "technicals.resistance_evaluation_status", "technicals.gap_statistics",
        "historical_recovery", "market_context.market_status", "market_context.sector_status", "market_context.opportunity_score",
        "market_context.benchmarks", "l3", "l4",
    ]
    if security_type != "etf":
        if market_policy.get("require_sector_etf") is True:
            required.append("market_context.sector_etf")
        if market_policy.get("require_competitors_for_operating_company") is True:
            required.append("market_context.competitors")
    for field in ("quote.market_session", "technicals.distance_to_nearest_resistance_pct", "valuation_score", "event_risk_score", "cyclicality_risk_score"):
        root, *rest = field.split(".")
        source = payload.get(root) if rest else payload
        value = source.get(rest[0]) if rest and isinstance(source, Mapping) else payload.get(field) if not rest else None
        if value is not None:
            required.append(field)
    evidence_result = bind({**dict(payload), "quote": dict(quote), "technicals": dict(technicals), "historical_recovery": dict(recovery), "market_context": dict(market), "l3": dict(l3), "l4": dict(l4)}, required)
    market_missing: list[str] = []
    benchmarks = market.get("benchmarks") if isinstance(market.get("benchmarks"), Mapping) else {}
    for benchmark in market_policy.get("required_benchmarks") or []:
        value = benchmarks.get(benchmark)
        if not isinstance(value, Mapping) or not value:
            market_missing.append(f"market_context.benchmarks.{benchmark}")
    if security_type != "etf" and market_policy.get("require_sector_etf") is True:
        sector_etf = market.get("sector_etf") if isinstance(market.get("sector_etf"), Mapping) else {}
        if not str(sector_etf.get("ticker") or "").strip():
            market_missing.append("market_context.sector_etf")
    if security_type != "etf" and market_policy.get("require_competitors_for_operating_company") is True:
        competitors = market.get("competitors") if isinstance(market.get("competitors"), list) else []
        valid_competitors = [row for row in competitors if isinstance(row, Mapping) and str(row.get("ticker") or "").strip()]
        unique_competitors = {str(row.get("ticker") or "").strip().upper() for row in valid_competitors}
        if len(unique_competitors) < int(market_policy.get("minimum_competitors") or 2):
            market_missing.append("market_context.competitors")
    last, bid, ask = number(quote.get("last")), number(quote.get("bid")), number(quote.get("ask"))
    adv = number(quote.get("avg_dollar_volume"))
    quote_time = parse_time(quote.get("as_of")) if quote.get("as_of") else None
    bid_ask_time = parse_time(quote.get("bid_ask_as_of")) if quote.get("bid_ask_as_of") else None
    quote_age = (now - quote_time).total_seconds() if quote_time else None
    bid_ask_age = (now - bid_ask_time).total_seconds() if bid_ask_time else None
    spread = 100 * (ask - bid) / ((ask + bid) / 2) if bid is not None and ask is not None and bid > 0 and ask >= bid else None
    execution_policy = load_connectors()["execution"]["quote"]
    quote_fresh = quote_age is not None and bid_ask_age is not None and -300 <= quote_age <= execution_policy["max_open_now_quote_age_seconds"] and -300 <= bid_ask_age <= execution_policy["max_open_now_quote_age_seconds"]
    spread_ok = spread is not None and spread <= execution_policy["max_open_now_spread_pct"]
    liquidity_ok = adv is not None and adv >= execution_policy["minimum_average_dollar_volume_usd"]

    stabilization_block = technicals.get("stabilization") if isinstance(technicals.get("stabilization"), Mapping) else {}
    stabilization = str(stabilization_block.get("stabilization_status") or "not_evaluated")
    resistance_status = str(technicals.get("resistance_evaluation_status") or "not_evaluated")
    distance = number(technicals.get("distance_to_nearest_resistance_pct"))
    room5 = distance is not None and distance >= 5 if resistance_status == "confirmed_level" else True if resistance_status == "no_resistance_found_in_126d" else None
    room7 = distance is not None and distance >= 7 if resistance_status == "confirmed_level" else True if resistance_status == "no_resistance_found_in_126d" else None

    recovery_status = str(recovery.get("sample_status") or recovery.get("sufficiency") or "unknown")
    recovery_episodes = int(recovery.get("eligible_episode_count") or recovery.get("episode_count") or 0)
    recovery_score = number(recovery.get("hit_7pct_rate_pct")) if recovery_status == "adequate" and recovery_episodes >= 5 else None
    stabilization_score = {"confirmed": 90.0, "emerging": 65.0, "absent": 35.0, "not_evaluated": None}.get(stabilization)
    technical_path = mean([stabilization_score, recovery_score])
    hv = number(technicals.get("historical_volatility_30d_pct"))
    atr = number(technicals.get("atr_14_pct"))
    drawdown = number(technicals.get("drawdown_52w_pct"))
    gap = technicals.get("gap_statistics") if isinstance(technicals.get("gap_statistics"), Mapping) else {}
    max_down_gap = number(gap.get("max_down_gap_pct"))
    history = technicals.get("data_sufficiency") if isinstance(technicals.get("data_sufficiency"), Mapping) else {}
    daily_status = str(history.get("daily_history_status") or "insufficient")

    opportunity_components = {
        "business_quality": number(l3.get("l3_score")),
        "financial_durability": number(l3.get("financial_durability_score")),
        "temporary_pullback": number((l4.get("event_components") or {}).get("temporary_drop_support")),
        "technical_recovery_path": technical_path,
        "earnings_estimates_catalyst": number(l4.get("l4_score")),
        "valuation": number(payload.get("valuation_score")),
        "market_sector": number(market.get("opportunity_score")),
        "liquidity": 95.0 if adv is not None and adv >= 100e6 else 80.0 if adv is not None and adv >= 25e6 else 60.0 if adv is not None and adv >= 5e6 else 35.0 if adv is not None else None,
    }
    atr_gap = mean([clamp(atr * 12) if atr is not None else None, clamp(abs(max_down_gap) * 8) if max_down_gap is not None else None])
    risk_components = {
        "historical_volatility": clamp(hv * 0.8) if hv is not None else None,
        "atr_and_gap_risk": atr_gap,
        "drawdown_and_structure": clamp(abs(drawdown) * 1.6) if drawdown is not None else None,
        "event_risk": number(payload.get("event_risk_score")),
        "cyclicality_and_estimate_dispersion": number(payload.get("cyclicality_risk_score")),
        "history_uncertainty": {"full": 15.0, "limited_but_usable": 65.0, "insufficient": 95.0}.get(daily_status),
    }
    scores = score_dimensions(opportunity_components, risk_components)
    inherited_vetoes = sorted(set(str(value) for value in list(l3.get("hard_vetoes") or []) + list(l4.get("hard_vetoes") or []) if value))
    missing = list(evidence_result["missing_fields"]) + market_missing
    if l3.get("fundamental_eligible") is not True or l3.get("evidence_validated") is not True:
        missing.append("l3.validated_result")
    if l4.get("l4_status") not in {"pass", "conditional"} or l4.get("evidence_validated") is not True:
        missing.append("l4.validated_result")
    critical_complete = not missing and evidence_result["valid"] and not inherited_vetoes and quote_fresh and spread_ok and liquidity_ok and scores["opportunity_class"] != "E" and scores["intrinsic_risk_coverage_pct"] >= 50
    session = str(quote.get("market_session") or "unknown").lower()
    if inherited_vetoes:
        readiness = "do_not_enter"
    elif not critical_complete:
        readiness = "wait"
    elif stabilization != "confirmed" or room7 is not True:
        readiness = "wait"
    elif session != "regular":
        readiness = "prepare_limit_order"
    else:
        readiness = "open_now_candidate"
    return {
        "ticker": ticker, "contract_id": contract_id, "security_type": str(payload.get("security_type") or ""), "depth": "L5", "entry_readiness": readiness,
        "critical_complete": critical_complete, "missing_checks": sorted(set(missing)),
        "hard_vetoes": inherited_vetoes, "quote": {**dict(quote), "age_seconds": quote_age, "bid_ask_age_seconds": bid_ask_age, "spread_pct": spread, "fresh": quote_fresh},
        "room_to_5pct": room5, "room_to_7pct": room7, "technical_stabilization": stabilization,
        "resistance_evaluation_status": resistance_status, "historical_recovery_sample_status": recovery_status,
        "historical_recovery_episode_count": recovery_episodes, **scores,
        "l3": dict(l3), "l4": dict(l4),
        "market_context_validation": {"complete": not market_missing, "missing_checks": sorted(set(market_missing))},
        "evidence_validated": evidence_result["valid"], "evidence": evidence_result["records"],
        "evidence_links": evidence_result["links"], "evidence_errors": evidence_result["errors"],
    }
