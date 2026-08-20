#!/usr/bin/env python3
"""Structural-quality passports for v3 broad search.

The passport layer deliberately excludes fast market/recovery facts such as
current price, current guidance trend, analyst targets, and account context.
It produces issuer-level structural quality that can be reused across searches
when a fresh delta check authorizes reuse.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

from common import clamp, load_policy, mean, number, parse_time, piecewise, semantic_hash
from evidence import bind
from eligibility import validate as validate_instrument
import selection


def _score(value: Any) -> float | None:
    parsed = number(value)
    return clamp(parsed) if parsed is not None else None


def _normalize_next_review_date(value: Any, *, research_cutoff: dt.datetime) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        review_date = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("next_review_date must use YYYY-MM-DD") from exc
    if review_date < research_cutoff.date():
        raise ValueError("next_review_date cannot precede the research cutoff date")
    return review_date.isoformat()


def _growth(value: Any) -> float | None:
    return piecewise(value, [(-20, 10), (-10, 25), (0, 50), (10, 68), (20, 82), (30, 92), (50, 98)])


def _margin(value: Any) -> float | None:
    return piecewise(value, [(-30, 10), (-10, 25), (0, 50), (5, 62), (10, 75), (20, 90), (35, 98)])


def _growth_margin(value: Any) -> float | None:
    return piecewise(value, [(-60, 10), (-40, 25), (-25, 42), (-15, 55), (0, 72), (10, 85), (20, 95), (35, 99)])


def _margin_change(value: Any) -> float | None:
    return piecewise(value, [(-15, 10), (-8, 25), (-3, 45), (0, 65), (3, 80), (8, 95), (15, 99)])


def _debt(value: Any) -> float | None:
    parsed = number(value)
    if parsed is None:
        return None
    if parsed <= 0:
        return 98.0
    return piecewise(parsed, [(0, 95), (1, 90), (2, 80), (3, 65), (4, 50), (6, 25), (10, 5)])


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
    parsed = number(value)
    if parsed is None:
        return None
    if parsed <= 0:
        return 98.0
    return piecewise(parsed, [(0, 98), (2, 92), (5, 80), (8, 65), (15, 40), (25, 15), (40, 5)])


def _concentration(value: Any) -> float | None:
    return piecewise(value, [(0, 98), (10, 95), (25, 85), (35, 70), (50, 50), (70, 25), (100, 5)])


def _bool_score(value: Any, true_score: float = 88.0, false_score: float = 30.0) -> float | None:
    if value is True:
        return true_score
    if value is False:
        return false_score
    return None


def _component(values: list[tuple[str, float | None]]) -> dict[str, Any]:
    known = [value for _, value in values if value is not None]
    return {
        "score": round(mean(known), 2) if known else None,
        "fact_fields": [f"facts.{name}" for name, value in values if value is not None],
        "expected_fact_count": len(values),
        "known_fact_count": len(known),
        "fact_coverage_pct": round(100 * len(known) / len(values), 2) if values else 0.0,
    }


def resolve_archetype(payload: Mapping[str, Any]) -> dict[str, str]:
    """Resolve a stable economic archetype.

    Quarterly revenue growth alone is intentionally insufficient to flip an
    issuer between established and growth. An explicit previously validated
    archetype wins unless the instrument type/economics make it impossible.
    """
    security_type = str(payload.get("security_type") or "").strip().lower()
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    sector = str(payload.get("sector") or "").strip().lower()
    explicit = str(payload.get("economic_archetype") or "").strip().lower()
    allowed = {"established_quality", "recognized_growth", "cyclical", "bank", "etf"}
    if security_type == "etf":
        archetype, reason = "etf", "instrument_type"
    elif any(facts.get(name) is not None for name in ("cet1_ratio_pct", "capital_quality_score", "funding_stability_score")) or "bank" in sector:
        archetype, reason = "bank", "bank_economics"
    elif facts.get("normalized_cycle_quality_score") is not None or sector in {"energy", "materials", "metals", "mining"}:
        archetype, reason = "cyclical", "cycle_evidence"
    elif explicit in allowed - {"etf", "bank", "cyclical"}:
        archetype, reason = explicit, "explicit_validated_archetype"
    elif facts.get("business_reality_score") is not None or facts.get("sales_efficiency_score") is not None or (number(facts.get("revenue_cagr_3y_pct")) or -999) >= 20:
        archetype, reason = "recognized_growth", "multi_period_growth_economics"
    else:
        archetype, reason = "established_quality", "default_operating_company"
    return {
        "economic_archetype": archetype,
        "listing_overlay": "adr" if security_type == "adr" else "none",
        "archetype_resolution": reason,
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
        # Structural trajectory intentionally excludes latest-quarter/YoY
        # momentum. Fast revenue/earnings/margin changes belong to fresh L4.
        "trajectory_quality": _component([
            ("revenue_cagr_3y_pct", _growth(f.get("revenue_cagr_3y_pct"))),
            ("multi_period_earnings_quality_score", _score(f.get("multi_period_earnings_quality_score"))),
        ]),
        "capital_discipline": _component([
            ("dilution_pct_yoy", _dilution(f.get("dilution_pct_yoy"))),
            ("capital_allocation_score", _score(f.get("capital_allocation_score"))),
            ("management_quality_score", _score(f.get("management_quality_score"))),
        ]),
    }


def _growth_company(f: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
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
        # TTM level economics may be cached until the next material filing;
        # YoY changes are fast momentum and are evaluated fresh later.
        "unit_economics": _component([
            ("operating_margin_pct", _growth_margin(f.get("operating_margin_pct"))),
            ("fcf_margin_pct", _growth_margin(f.get("fcf_margin_pct"))),
        ]),
        "growth_quality": _component([
            ("revenue_cagr_3y_pct", _growth(f.get("revenue_cagr_3y_pct"))),
            ("multi_period_growth_quality_score", _score(f.get("multi_period_growth_quality_score"))),
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
    capital = _score(f.get("capital_quality_score"))
    if capital is None:
        capital = piecewise(f.get("cet1_ratio_pct"), [(5, 10), (7, 35), (9, 60), (11, 78), (13, 90), (16, 98)])
    asset = _score(f.get("asset_quality_score"))
    if asset is None and number(f.get("nonperforming_assets_pct")) is not None:
        asset = piecewise(f.get("nonperforming_assets_pct"), [(0, 98), (0.5, 92), (1, 80), (2, 60), (3, 40), (5, 15), (10, 5)])
    profitability = _score(f.get("bank_profitability_score"))
    if profitability is None:
        profitability = mean([
            piecewise(f.get("roa_pct"), [(-1, 5), (0, 35), (0.7, 65), (1, 80), (1.5, 95), (2, 99)]),
            piecewise(f.get("roe_pct"), [(-10, 5), (0, 35), (7, 65), (10, 80), (15, 95), (22, 99)]),
        ])
    return {
        "franchise_durability": _component([
            ("competitive_position_score", _score(f.get("competitive_position_score"))),
            ("management_quality_score", _score(f.get("management_quality_score"))),
            ("franchise_quality_score", _score(f.get("franchise_quality_score"))),
        ]),
        "capital_strength": _component([("capital_strength", capital)]),
        "asset_quality": _component([("asset_quality", asset)]),
        "profitability_quality": _component([("profitability_quality", profitability)]),
        "funding_stability": _component([("funding_stability_score", _score(f.get("funding_stability_score")))]),
    }


def _etf(f: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    diversification = _score(f.get("breadth_quality_score"))
    if diversification is None:
        diversification = mean([
            piecewise(f.get("largest_holding_weight_pct"), [(0, 99), (5, 95), (10, 80), (15, 65), (25, 40), (40, 15), (100, 5)]),
            piecewise(f.get("top10_weight_pct"), [(0, 99), (25, 95), (40, 85), (55, 70), (70, 50), (85, 25), (100, 5)]),
        ])
    resilience = _score(f.get("fund_structure_quality_score"))
    if resilience is None:
        resilience = piecewise(f.get("fund_aum"), [(50e6, 30), (100e6, 50), (500e6, 70), (2e9, 85), (10e9, 95), (100e9, 99)])
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


SCORERS = {
    "established_quality": _established,
    "recognized_growth": _growth_company,
    "cyclical": _cyclical,
    "bank": _bank,
    "etf": _etf,
}


def _weighted(component_scores: Mapping[str, Any], weights: Mapping[str, Any]) -> tuple[float | None, float]:
    total = sum(float(value) for value in weights.values())
    numerator = 0.0
    known = 0.0
    for name, raw_weight in weights.items():
        value = number(component_scores.get(name))
        if value is None:
            continue
        weight = float(raw_weight)
        known += weight
        numerator += clamp(value) * weight
    return (round(numerator / total, 2) if total and known else None, round(100 * known / total, 2) if total else 0.0)


FAST_MOMENTUM_FACTS = {
    "guidance_trend",
    "revenue_growth_pct",
    "earnings_growth_pct",
    "operating_margin_change_pp_yoy",
    "fcf_margin_change_pp_yoy",
}


def _structural_fact_names(components: Mapping[str, Mapping[str, Any]]) -> set[str]:
    names: set[str] = set()
    for detail in components.values():
        for field in detail.get("fact_fields") or []:
            text = str(field)
            if text.startswith("facts."):
                names.add(text.split(".", 1)[1])
    # cash/debt are inputs to the derived cash_debt helper and do not appear in
    # the helper's synthetic fact name. Preserve them explicitly.
    names.update({"cash", "debt"})
    return names


def _safe_lineage(records: Any, structural_names: set[str]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if not isinstance(records, list):
        return output
    allowed_fields = {f"facts.{name}" for name in structural_names}
    for raw in records:
        if not isinstance(raw, Mapping) or str(raw.get("field") or "") not in allowed_fields:
            continue
        source = raw.get("source") if isinstance(raw.get("source"), Mapping) else {}
        row = {
            "field": raw.get("field"),
            "evidence_id": raw.get("evidence_id"),
            "as_of": raw.get("as_of"),
            "retrieved_at": raw.get("retrieved_at"),
            "period": raw.get("period"),
            "unit": raw.get("unit"),
            "quality_status": raw.get("quality_status"),
            "source": {
                key: source.get(key)
                for key in (
                    "source_type", "source_id", "provider", "transport",
                    "accession_or_edgar_handle", "period", "filing_lineage",
                )
                if source.get(key) not in (None, "")
            },
        }
        output.append(row)
    output.sort(key=lambda row: (str(row.get("field") or ""), str(row.get("evidence_id") or "")))
    return output


_UNCERTAINTY_ORDER = {"low": 0, "medium": 1, "high": 2}


def _assessment_rationale(records: Any, structural_names: set[str]) -> tuple[list[dict[str, Any]], list[str], str]:
    """Extract auditable model-judgment rubrics from normalized evidence.

    A source citation alone cannot prove a subjective moat or management
    score.  V4.2 therefore requires the scoring rubric, narrative rationale,
    contrary evidence considered, and uncertainty to survive in the durable
    Passport.  Missing material is an evidence error, not an invented zero.
    """
    output: list[dict[str, Any]] = []
    errors: list[str] = []
    uncertainties: list[str] = []
    if not isinstance(records, list):
        return output, ["subjective assessment evidence missing"], "high"
    allowed_fields = {f"facts.{name}" for name in structural_names if name.endswith("_score")}
    for raw in records:
        if not isinstance(raw, Mapping) or str(raw.get("field") or "") not in allowed_fields:
            continue
        field = str(raw.get("field"))
        rubric = raw.get("rubric") if isinstance(raw.get("rubric"), Mapping) else {}
        criteria = rubric.get("criteria") if isinstance(rubric.get("criteria"), list) else []
        rationale = str(rubric.get("rationale") or raw.get("rationale") or "").strip()
        contrary = rubric.get("contrary_evidence") if isinstance(rubric.get("contrary_evidence"), list) else None
        uncertainty = str(rubric.get("uncertainty") or "").strip().lower()
        method = str(rubric.get("method_version") or "").strip()
        if not method:
            errors.append(f"{field} subjective score rubric method missing")
        if not criteria:
            errors.append(f"{field} subjective score criteria missing")
        if not rationale:
            errors.append(f"{field} subjective score rationale missing")
        if contrary is None:
            errors.append(f"{field} contrary evidence field missing")
            contrary = []
        if uncertainty not in _UNCERTAINTY_ORDER:
            errors.append(f"{field} assessment uncertainty missing or invalid")
            uncertainty = "high"
        uncertainties.append(uncertainty)
        output.append({
            "field": field,
            "evidence_id": raw.get("evidence_id"),
            "score": raw.get("value"),
            "method_version": method or None,
            "criteria": criteria,
            "rationale": rationale or None,
            "contrary_evidence": contrary,
            "uncertainty": uncertainty,
        })
    expected = allowed_fields
    observed = {str(item["field"]) for item in output}
    for field in sorted(expected - observed):
        errors.append(f"{field} subjective assessment record missing")
    output.sort(key=lambda item: (str(item.get("field") or ""), str(item.get("evidence_id") or "")))
    overall = max(uncertainties, key=lambda item: _UNCERTAINTY_ORDER[item]) if uncertainties else "low"
    return output, errors, overall


def evaluate(payload: Mapping[str, Any]) -> dict[str, Any]:
    ticker = str(payload.get("ticker") or "").strip().upper()
    contract_id = str(payload.get("contract_id") or "").strip()
    if not ticker or not contract_id:
        raise ValueError("quality passport evaluation requires ticker and contract_id")
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    clearances = payload.get("clearances") if isinstance(payload.get("clearances"), Mapping) else {}
    arch = resolve_archetype(payload)
    lane = arch["economic_archetype"]
    overlay = arch["listing_overlay"]
    policy = load_policy()
    l3 = policy["l3"]
    components = SCORERS[lane](facts)
    component_scores = {name: detail["score"] for name, detail in components.items()}
    score, weighted_coverage = _weighted(component_scores, l3["weights"][lane])
    total_expected = sum(int(detail["expected_fact_count"]) for detail in components.values())
    total_known = sum(int(detail["known_fact_count"]) for detail in components.values())
    fact_coverage = round(100 * total_known / total_expected, 2) if total_expected else 0.0
    coverage = min(weighted_coverage, fact_coverage)

    required_clearances = list(l3["required_clearances"][lane])
    if overlay == "adr":
        required_clearances.extend(l3["adr_overlay_clearances"])
    # Bind actual source facts, not names of derived helper metrics such as
    # cash_debt/capital_strength. Fast guidance is deliberately excluded from
    # structural Passport evidence even when the caller included it.
    structural_names = _structural_fact_names(components)
    required_fields = [f"facts.{name}" for name in sorted(structural_names) if facts.get(name) is not None and name not in FAST_MOMENTUM_FACTS]
    required_fields.extend(f"clearances.{name}" for name in required_clearances)
    evidence_result = bind(dict(payload), sorted(set(required_fields)))
    assessment_rationale, assessment_errors, assessment_uncertainty = _assessment_rationale(
        evidence_result.get("records"), structural_names
    )
    if assessment_errors:
        evidence_result["errors"].extend(assessment_errors)
        evidence_result["valid"] = False
    if facts.get("cash") is not None and facts.get("debt") is not None:
        linked = {row["evidence_id"]: row for row in evidence_result["records"]}
        cash_rows = [linked[ident] for ident in evidence_result["links"].get("facts.cash", []) if ident in linked]
        debt_rows = [linked[ident] for ident in evidence_result["links"].get("facts.debt", []) if ident in linked]
        if not cash_rows or not debt_rows or {str(row.get("period") or "") for row in cash_rows} != {str(row.get("period") or "") for row in debt_rows} or {str(row.get("unit") or "") for row in cash_rows} != {str(row.get("unit") or "") for row in debt_rows}:
            evidence_result["errors"].append("structural cash/debt derivation requires the same period and unit")
            evidence_result["valid"] = False

    missing_clearances: list[str] = []
    hard_vetoes = list(validate_instrument(payload)["hard_vetoes"])
    for name in sorted(set(required_clearances)):
        value = str(clearances.get(name) or "unknown").lower()
        if value == "triggered":
            hard_vetoes.append(l3["clearance_veto_map"][name])
        elif value != "clear":
            missing_clearances.append(name)

    issuer_id = selection.derive_issuer_id(payload)
    research_cutoff = parse_time(payload.get("research_cutoff_at")) if payload.get("research_cutoff_at") else dt.datetime.now(dt.timezone.utc)
    next_review_date = _normalize_next_review_date(payload.get("next_review_date"), research_cutoff=research_cutoff)
    collection = str(payload.get("collection_status") or "ready")
    evidence_problem = not evidence_result["valid"] or missing_clearances or score is None or coverage < float(l3["minimum_coverage_pct"])
    if hard_vetoes:
        status, reason = "rejected", "hard_veto"
    elif evidence_problem:
        if collection == "exhausted" and next_review_date:
            status, reason = "insufficient_data", "sources_exhausted"
        elif collection == "exhausted":
            status, reason = "recheck", "next_review_date_missing_for_insufficient_data"
        else:
            status, reason = "recheck", "evidence_or_coverage_incomplete"
    elif score < float(l3["minimum_quality_score"]):
        status, reason = "rejected", "below_minimum_structural_quality"
    elif coverage < float(l3["pass_coverage_pct"]):
        status, reason = "conditional", "structural_quality_passed_with_partial_coverage"
    else:
        status, reason = "pass", "structural_quality_gate_passed"
    eligible = status in {"pass", "conditional"} and score is not None and score >= float(l3["minimum_quality_score"]) and not hard_vetoes

    summary = {
        "ticker": ticker,
        "contract_id": contract_id,
        "issuer_id": issuer_id,
        "security_type": str(payload.get("security_type") or ""),
        **arch,
        "quality_status": status,
        "quality_status_reason": reason,
        "quality_score": score,
        "quality_coverage_pct": coverage,
        "quality_eligible": eligible,
        "quality_components": component_scores,
        "missing_clearance_checks": sorted(set(missing_clearances)),
        "hard_vetoes": sorted(set(hard_vetoes)),
        "evidence_validated": evidence_result["valid"] and not missing_clearances,
        "evidence_structure_validated": evidence_result["valid"] and not missing_clearances,
        "assessment_is_model_judgment": bool(assessment_rationale),
        "assessment_rubric_version": sorted({str(item.get("method_version")) for item in assessment_rationale if item.get("method_version")}),
        "assessment_uncertainty": assessment_uncertainty,
        "auditability_class": "v42_rich" if not assessment_errors else "v42_incomplete",
        "quality_policy_version": str(policy.get("quality_registry", {}).get("quality_policy_version") or "3.0.0"),
        "as_of": research_cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "reuse_class": "structural_quality",
        "next_review_date": next_review_date,
    }
    summary["quality_result_sha256"] = semantic_hash(summary)
    structural_facts = {name: facts.get(name) for name in sorted(structural_names) if facts.get(name) is not None and name not in FAST_MOMENTUM_FACTS}
    return {
        **summary,
        "structural_facts": structural_facts,
        "component_details": components,
        "assessment_rationale": assessment_rationale,
        "evidence_lineage": _safe_lineage(evidence_result["records"], structural_names),
        "evidence": evidence_result["records"],
        "evidence_links": evidence_result["links"],
        "evidence_errors": evidence_result["errors"],
    }


def durable_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "ticker", "contract_id", "issuer_id", "security_type", "economic_archetype", "listing_overlay",
        "archetype_resolution", "quality_status", "quality_status_reason", "quality_score",
        "quality_coverage_pct", "quality_eligible", "quality_components", "missing_clearance_checks",
        "hard_vetoes", "evidence_validated", "evidence_structure_validated", "assessment_is_model_judgment",
        "assessment_rubric_version", "assessment_uncertainty", "auditability_class",
        "quality_policy_version", "as_of", "reuse_class", "next_review_date",
        "quality_result_sha256",
    )
    value = {field: result.get(field) for field in fields if field in result}
    if value.get("quality_result_sha256") is None:
        body = dict(value)
        body.pop("quality_result_sha256", None)
        value["quality_result_sha256"] = semantic_hash(body)
    return value


def proposal(result: Mapping[str, Any], *, event_scan_through: str | None = None) -> dict[str, Any]:
    """Build a public-safe Registry proposal from a structural research result.

    Raw connector payloads, quotes, account data and licensed analyst payloads
    are never serialized. The durable Passport keeps normalized structural
    facts, component/rubric outputs and source lineage only.
    """
    summary = durable_summary(result)
    passport_payload = {
        "schema_version": "1.0.0",
        "kind": "qrgf_quality_passport",
        "issuer_id": summary["issuer_id"],
        "quality_policy_version": summary.get("quality_policy_version"),
        "economic_archetype": summary.get("economic_archetype"),
        "listing_overlay": summary.get("listing_overlay"),
        "as_of": summary.get("as_of"),
        "summary": summary,
        "structural_facts": dict(result.get("structural_facts") or {}),
        "component_details": dict(result.get("component_details") or {}),
        "assessment_rationale": list(result.get("assessment_rationale") or []),
        "evidence_lineage": list(result.get("evidence_lineage") or []),
    }
    passport_sha = semantic_hash(passport_payload)
    body = {
        "schema_version": "1.0.0",
        "kind": "qrgf_passport_update_proposal",
        "issuer_id": summary["issuer_id"],
        "quality_policy_version": summary.get("quality_policy_version"),
        "event_scan_through": event_scan_through,
        "summary": summary,
        "passport_sha256": passport_sha,
        "passport_payload": passport_payload,
    }
    return {**body, "proposal_sha256": semantic_hash(body)}
