#!/usr/bin/env python3
"""High-recall L2 research triage for quality-pullback-recovery candidates.

L2 ranks the value of further research. It does not make a final trading-entry
decision. Volatility, deep drawdown, weak short-term trend, and nearby
resistance remain risk/timing signals rather than automatic research vetoes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from qrgf_common import clamp, is_missing, strict_bool, strict_float, tolerant_bool, tolerant_float  # noqa: E402

TRUSTED_EXTERNAL_HARD_VETOES = {
    "insufficient_liquidity",
    "price_execution_unavailable",
    "prohibited_instrument",
    "data_integrity_failure",
    "inactive_or_halted_contract",
}


def finite(value: Any, field: str) -> float:
    parsed = strict_float(value, field=field, allow_none=False)
    assert parsed is not None
    return parsed


def positive(value: Any, field: str) -> float:
    parsed = finite(value, field)
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def rule_ids(value: Any, allowed: set[str], field: str) -> list[str]:
    if is_missing(value):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result: list[str] = []
    for item in value:
        ident = str(item).strip()
        if not ident:
            continue
        if ident not in allowed:
            raise ValueError(f"{field}: untrusted rule id {ident!r}")
        if ident not in result:
            result.append(ident)
    return result


def growth_score(r3: float, r6: float, r12: float | None, history: str) -> float:
    """Estimate research relevance without requiring all current windows positive."""
    values = [r3, r6] + ([] if r12 is None else [r12])
    score = 45.0
    # Positive longer-term performance is useful prior-growth evidence.
    if r12 is not None:
        if r12 >= 50:
            score += 28
        elif r12 >= 15:
            score += 20
        elif r12 >= 0:
            score += 12
        elif r12 >= -25:
            score += 6
        elif r12 <= -70:
            score -= 20
    # A recent decline is compatible with the strategy; collapse beyond 70%
    # is a speculation/structure warning but not proof of a broken business.
    if -45 <= r3 <= 5:
        score += 12
    elif r3 < -70:
        score -= 18
    if -50 <= r6 <= 15:
        score += 10
    elif r6 < -75:
        score -= 15
    if history == "limited_but_usable":
        score -= 8
    return clamp(score)


def pullback_score(dd: float) -> float:
    dd = abs(dd)
    if dd < 5:
        return 10.0
    if dd < 8:
        return 28.0
    if dd <= 20:
        return 82.0
    if dd <= 35:
        return 92.0
    if dd <= 50:
        return 95.0
    if dd <= 65:
        return 70.0
    return 30.0


def liquidity_score(adv: float) -> float:
    if adv >= 500_000_000:
        return 100.0
    if adv >= 100_000_000:
        return 92.0
    if adv >= 25_000_000:
        return 80.0
    if adv >= 5_000_000:
        return 62.0
    if adv >= 2_000_000:
        return 45.0
    return 0.0


def volatility_risk(vol: float) -> float:
    if vol <= 25:
        return 20.0
    if vol <= 45:
        return 38.0
    if vol <= 70:
        return 58.0
    if vol <= 100:
        return 75.0
    if vol <= 150:
        return 88.0
    return 96.0


def derive_history(candidate: dict[str, Any]) -> tuple[str, list[str]]:
    sessions = tolerant_float(candidate.get("trading_history_days"))
    r3 = tolerant_float(candidate.get("return_3m_pct"))
    r6 = tolerant_float(candidate.get("return_6m_pct"))
    r12 = tolerant_float(candidate.get("return_12m_pct"))
    missing: list[str] = []
    if sessions is not None:
        if sessions >= 252:
            status = "full"
            if r12 is None:
                missing.append("return_12m_pct")
        elif sessions >= 126:
            status = "limited_but_usable"
            if r3 is None:
                missing.append("return_3m_pct")
            if r6 is None:
                missing.append("return_6m_pct")
        else:
            status = "insufficient"
        return status, missing
    if r12 is not None:
        return "full", missing
    try:
        listed = parse_datetime(candidate.get("listing_date"))
        observed = parse_datetime(candidate.get("as_of") or candidate.get("data_as_of"))
    except ValueError:
        listed = observed = None
    if listed is not None and observed is not None and observed >= listed:
        age_days = (observed - listed).days
        if age_days >= 365:
            status = "full"
            missing.append("return_12m_pct")
        elif age_days >= 180:
            status = "limited_but_usable"
            if r3 is None:
                missing.append("return_3m_pct")
            if r6 is None:
                missing.append("return_6m_pct")
        else:
            status = "insufficient"
        return status, missing
    missing.append("objective_history_evidence")
    return "unknown", missing


def _strict_candidate_bool(candidate: dict[str, Any], field: str) -> bool:
    value = candidate.get(field)
    if is_missing(value):
        return False
    try:
        parsed = strict_bool(value, field=field)
    except ValueError:
        return False
    return parsed is True


def classify(candidate: dict[str, Any], rules: dict[str, Any], rules_hash: str) -> dict[str, Any]:
    ticker = str(candidate.get("ticker") or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required")

    required = list(rules.get("required_numeric_fields") or [])
    checks_missing: list[str] = []
    parsed: dict[str, float | None] = {}
    for field in required:
        value = tolerant_float(candidate.get(field))
        parsed[field] = value
        if value is None:
            checks_missing.append(field)

    history, history_missing = derive_history(candidate)
    checks_missing.extend(history_missing)
    r12 = tolerant_float(candidate.get("return_12m_pct"))
    if history == "full" and r12 is None and "return_12m_pct" not in checks_missing:
        checks_missing.append("return_12m_pct")

    current_price = parsed.get("current_price")
    adv = parsed.get("avg_dollar_volume")
    r3 = parsed.get("return_3m_pct")
    r6 = parsed.get("return_6m_pct")
    dd = parsed.get("drawdown_pct")
    hv = parsed.get("historical_volatility_pct")

    allowed_external = rules.get("advisory_external_rule_ids") or {}
    # Hard vetoes are derived below from normalized facts. An incoming list is
    # never trusted because it could either hide a real veto or fabricate a
    # false rejection. External flags remain advisory and allowlisted.
    allowed_risk = set(allowed_external.get("risk_flag") or [])
    external_risk = rule_ids(candidate.get("risk_flags"), allowed_risk, "risk_flags") if candidate.get("risk_flags") else []
    allowed_opp = set(allowed_external.get("opportunity_flag") or [])
    external_opp = rule_ids(candidate.get("opportunity_flags"), allowed_opp, "opportunity_flags") if candidate.get("opportunity_flags") else []

    hard_vetoes: list[str] = []
    risk_flags = list(external_risk)
    opportunity_flags = list(external_opp)
    decision_rule_ids: list[str] = []

    security_type = str(candidate.get("security_type") or "").strip().lower()
    instrument_status = str(candidate.get("instrument_status") or "resolution_required").strip().lower()
    if instrument_status in {"halted", "inactive", "delisted", "suspended"}:
        hard_vetoes.append("inactive_or_halted_contract")
    elif security_type not in {"common_equity", "adr", "etf"} or instrument_status not in {"eligible", "verified", "active"}:
        hard_vetoes.append("prohibited_instrument")
    if str(candidate.get("data_integrity_status") or "").strip().lower() in {"failed", "invalid", "corrupt"}:
        hard_vetoes.append("data_integrity_failure")
    if current_price is not None and current_price <= 0:
        hard_vetoes.append("price_execution_unavailable")
    min_liquidity = float((rules.get("hard_veto_thresholds") or {}).get("minimum_avg_dollar_volume", 2_000_000))
    if adv is not None and adv < min_liquidity:
        hard_vetoes.append("insufficient_liquidity")
    hard_vetoes = list(dict.fromkeys(hard_vetoes))

    if hv is not None:
        if hv >= 100:
            risk_flags.append("excessive_historical_volatility")
        elif hv >= 65:
            risk_flags.append("elevated_historical_volatility")
    if dd is not None:
        if dd >= 40:
            risk_flags.append("deep_price_break")
        if dd < 8:
            risk_flags.append("pullback_too_shallow_for_current_setup")
    if history == "limited_but_usable":
        risk_flags.append("incomplete_trading_history")
    elif history in {"insufficient", "unknown"}:
        risk_flags.append("history_not_comparable")

    # Compute research score whenever the mass research inputs exist. Resistance
    # evaluation is intentionally not required for research priority.
    research_components: dict[str, float | None] = {
        "prior_growth": None,
        "pullback_geometry": None,
        "liquidity": None,
        "room_to_target": None,
        "quality_prior": None,
        "data_completeness": None,
    }
    if r3 is not None and r6 is not None:
        research_components["prior_growth"] = growth_score(r3, r6, r12, history)
    if dd is not None:
        research_components["pullback_geometry"] = pullback_score(dd)
    if adv is not None:
        research_components["liquidity"] = liquidity_score(adv)
    quality_prior = tolerant_float(candidate.get("quality_prior_score"))
    if quality_prior is not None:
        research_components["quality_prior"] = clamp(quality_prior)
        if quality_prior >= 80:
            opportunity_flags.append("strong_quality_prior")
    # Do not invent a neutral 50 when quality data are absent.
    research_components["data_completeness"] = clamp(100 - 12 * len(set(checks_missing)))

    resistance_status = str(candidate.get("resistance_status") or "not_evaluated").strip().lower()
    touches_raw = candidate.get("resistance_touch_prices") or []
    touch_prices = [tolerant_float(v) for v in touches_raw] if isinstance(touches_raw, list) else []
    touch_prices = [v for v in touch_prices if v is not None and v > 0]
    resistance_price = min(touch_prices) if touch_prices else tolerant_float(candidate.get("major_resistance_price"))
    distance_to_resistance_pct: float | None = None
    preliminary_timing_status = "unknown"
    entry_readiness = "not_assessed"

    if current_price is not None and current_price > 0 and resistance_price is not None:
        distance_to_resistance_pct = 100.0 * (resistance_price / current_price - 1.0)
        research_components["room_to_target"] = clamp(distance_to_resistance_pct / 7.0 * 100.0)
    elif resistance_status == "not_found":
        research_components["room_to_target"] = 80.0
    else:
        if "resistance_evaluation" not in checks_missing:
            checks_missing.append("resistance_evaluation")

    resistance_rules = rules.get("resistance") or {}
    entry_block = float(resistance_rules.get("preliminary_wait_if_distance_pct_below", resistance_rules.get("entry_not_ready_if_distance_pct_below", 5.0)))
    conditional_below = float(resistance_rules.get("conditional_if_distance_pct_below", 7.0))
    catalyst_confirmed = _strict_candidate_bool(candidate, "catalyst_confirmed")

    if resistance_status == "confirmed" and distance_to_resistance_pct is not None:
        if distance_to_resistance_pct < entry_block:
            risk_flags.append("resistance_before_5pct_target")
            decision_rule_ids.append("resistance_before_5pct_target")
            preliminary_timing_status = "wait"
            if catalyst_confirmed:
                decision_rule_ids.append("resistance_before_5pct_with_confirmed_catalyst")
        elif distance_to_resistance_pct < conditional_below:
            preliminary_timing_status = "conditional"
        else:
            preliminary_timing_status = "room_available"
    elif resistance_status == "not_found":
        preliminary_timing_status = "room_unconfirmed_but_no_resistance_found"
    else:
        preliminary_timing_status = "unknown"

    weights = (rules.get("research_priority") or {}).get("weights") or {}
    numerator = 0.0
    denominator = 0.0
    full_weight = 0.0
    for name, weight_raw in weights.items():
        weight = float(weight_raw)
        full_weight += weight
        value = research_components.get(name)
        if value is None:
            continue
        numerator += value * weight
        denominator += weight
    research_priority_score = round(numerator / denominator, 2) if denominator else None
    research_coverage_pct = round(100.0 * denominator / full_weight, 2) if full_weight else 0.0

    risk_components: dict[str, float | None] = {
        "historical_volatility": volatility_risk(hv) if hv is not None else None,
        "drawdown_and_structure": clamp((dd or 0) * 1.6) if dd is not None else None,
        "history_uncertainty": 10.0 if history == "full" else 65.0 if history == "limited_but_usable" else 95.0,
        "nearby_resistance": clamp(100 - (distance_to_resistance_pct or 0) * 12) if distance_to_resistance_pct is not None else None,
    }
    known_risk = [value for value in risk_components.values() if value is not None]
    l2_risk_score = round(sum(known_risk) / len(known_risk), 2) if known_risk else None
    risk_coverage_pct = round(100.0 * len(known_risk) / len(risk_components), 2)

    l2_opportunity_score = research_priority_score
    if hard_vetoes:
        l2_status = "rejected"
    elif history == "insufficient" or any(field in checks_missing for field in required) or (history == "full" and "return_12m_pct" in checks_missing):
        l2_status = "recheck"
    elif risk_flags or preliminary_timing_status in {"wait", "conditional", "unknown"}:
        l2_status = "conditional"
    else:
        l2_status = "pass"

    return {
        "ticker": ticker,
        "l2_status": l2_status,
        "entry_readiness": entry_readiness,
        "preliminary_timing_status": preliminary_timing_status,
        "research_priority_score": research_priority_score,
        "research_priority_coverage_pct": research_coverage_pct,
        "l2_opportunity_score": l2_opportunity_score,
        "l2_risk_score": l2_risk_score,
        "risk_coverage_pct": risk_coverage_pct,
        "research_components": research_components,
        "risk_components": risk_components,
        "risk_flags": sorted(set(risk_flags)),
        "opportunity_flags": sorted(set(opportunity_flags)),
        "hard_vetoes": hard_vetoes,
        "checks_missing": list(dict.fromkeys(checks_missing)),
        "decision_rule_ids": list(dict.fromkeys(decision_rule_ids)),
        "momentum_history_status": history,
        "trading_history_days": tolerant_float(candidate.get("trading_history_days")),
        "resistance_status": resistance_status,
        "resistance_confirmed": resistance_status == "confirmed",
        "resistance_touch_count": len(touch_prices),
        "resistance_touch_prices": touch_prices,
        "major_resistance_price": resistance_price,
        "distance_to_resistance_pct": round(distance_to_resistance_pct, 4) if distance_to_resistance_pct is not None else None,
        "ruleset_version": str(rules.get("ruleset_version") or ""),
        "ruleset_hash": rules_hash,
        "decision_eligible": False,
        "next_required_check": (
            "resolve_hard_veto" if hard_vetoes else
            "enrich_missing_l2_data" if l2_status == "recheck" else
            "run_L3_fundamental_quality" if l2_status in {"pass", "conditional"} else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--rules",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "assets" / "l2-rules.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rules_bytes = args.rules.read_bytes()
    rules = json.loads(rules_bytes)
    candidate = load_json(args.input)
    result = classify(candidate, rules, hashlib.sha256(rules_bytes).hexdigest())
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
