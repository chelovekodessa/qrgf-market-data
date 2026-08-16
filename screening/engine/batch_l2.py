#!/usr/bin/env python3
"""Run one ruleset over all L1 candidates and perform one global L2 ranking."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from classify_l2 import classify  # noqa: E402
from qrgf_common import atomic_write_json, parse_canonical_csv_row  # noqa: E402


def load_candidates(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            payload = payload.get("candidates", payload.get("rows", payload.get("data", payload)))
        if not isinstance(payload, list):
            raise ValueError("candidate JSON must contain a list")
        return [dict(row) for row in payload if isinstance(row, dict)]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [parse_canonical_csv_row(row) for row in csv.DictReader(handle)]


def adapt_l1_candidate(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    aliases = {
        "current_price": ("current_price", "price"),
        "return_3m_pct": ("return_3m_pct", "return_3m"),
        "return_6m_pct": ("return_6m_pct", "return_6m"),
        "return_12m_pct": ("return_12m_pct", "return_12m"),
        "drawdown_pct": ("drawdown_pct", "drawdown_52w"),
        "historical_volatility_pct": ("historical_volatility_pct", "historical_volatility"),
        "momentum_history_status": ("momentum_history_status", "history_sufficiency"),
    }
    for target, sources in aliases.items():
        if result.get(target) not in (None, ""):
            continue
        for source in sources:
            if row.get(source) not in (None, ""):
                result[target] = row.get(source)
                break
    return result


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def apply_selection_contract(row: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """Materialize fixed-denominator setup; optional evidence never shrinks it."""
    result = dict(row)
    legacy_score = _number(result.get("research_priority_score"))
    legacy_coverage = _number(result.get("research_priority_coverage_pct"))
    components = result.get("research_components") if isinstance(result.get("research_components"), dict) else {}
    setup = rules.get("selection_setup") or {}
    weights = setup.get("weights") or {}
    if not weights:
        raise ValueError("selection_setup.weights are required")
    full_weight = sum(float(weight) for weight in weights.values())
    known_weight = 0.0
    numerator = 0.0
    missing: list[str] = []
    for name, raw_weight in weights.items():
        weight = float(raw_weight)
        value = _number(components.get(name))
        if value is None:
            missing.append(name)
            continue
        known_weight += weight
        numerator += max(0.0, min(100.0, value)) * weight
    coverage = round(100.0 * known_weight / full_weight, 2) if full_weight else 0.0
    require_all = bool(setup.get("require_all_components", True))
    score = None if (require_all and missing) or not full_weight else round(numerator / full_weight, 2)

    result["legacy_research_priority_score"] = legacy_score
    result["legacy_research_priority_coverage_pct"] = legacy_coverage
    result["l2_setup_score"] = score
    result["l2_confidence_pct"] = coverage
    result["l2_setup_missing"] = missing
    result["l2_quality_prior_score"] = _number(components.get("quality_prior"))
    result["l2_room_to_target_score"] = _number(components.get("room_to_target"))
    result["l2_selection_model_version"] = str(setup.get("model_version") or rules.get("ruleset_version") or "")
    result["research_priority_score"] = score
    result["research_priority_coverage_pct"] = coverage
    result["l2_opportunity_score"] = score
    if score is None and result.get("l2_status") in {"pass", "conditional"}:
        result["l2_status"] = "recheck"
        missing_checks = list(result.get("checks_missing") or [])
        if "selection_setup_components" not in missing_checks:
            missing_checks.append("selection_setup_components")
        result["checks_missing"] = missing_checks
        result["next_required_check"] = "enrich_missing_l2_data"
    return result


def research_rescue(row: dict[str, Any], rules: dict[str, Any]) -> tuple[float, str]:
    """Small research-priority rescue for established market evidence only.

    This is deliberately not a fundamental-quality score and never changes L2
    Opportunity or Risk. It only protects a near-cutoff, liquid, full-history,
    non-chaotic security from being discarded before authoritative L3 research.
    """
    cfg = rules.get("near_cutoff_research_rescue") or {}
    if not cfg.get("enabled", False):
        return 0.0, "disabled"
    history = str(row.get("momentum_history_status") or "").strip().lower()
    sessions = _number(row.get("trading_history_days"))
    if history != "full" and (sessions is None or sessions < 253):
        return 0.0, "history_not_full"
    adv = _number(row.get("avg_dollar_volume"))
    hv = _number(row.get("historical_volatility_pct"))
    r12 = _number(row.get("return_12m_pct"))
    dd = _number(row.get("drawdown_pct"))
    if None in {adv, hv, r12, dd}:
        return 0.0, "evidence_missing"
    min_dd = float(cfg.get("minimum_drawdown_pct", 8.0))
    max_dd = float(cfg.get("maximum_drawdown_pct", 50.0))
    if dd < min_dd or dd > max_dd:
        return 0.0, "drawdown_outside_recovery_band"

    strong = cfg.get("strong") or {}
    if (
        adv >= float(strong.get("minimum_avg_dollar_volume", 500_000_000))
        and hv <= float(strong.get("maximum_historical_volatility_pct", 60.0))
        and r12 >= float(strong.get("minimum_return_12m_pct", 0.0))
    ):
        return float(strong.get("bonus_points", cfg.get("maximum_bonus_points", 1.5))), "strong_established_prior"

    standard = cfg.get("standard") or {}
    if (
        adv >= float(standard.get("minimum_avg_dollar_volume", 100_000_000))
        and hv <= float(standard.get("maximum_historical_volatility_pct", 70.0))
        and r12 >= float(standard.get("minimum_return_12m_pct", -5.0))
    ):
        return float(standard.get("bonus_points", 0.75)), "standard_established_prior"
    return 0.0, "criteria_not_met"


def setup_rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    status_rank = {"pass": 2, "conditional": 2, "recheck": 1, "rejected": 0}.get(str(row.get("l2_status")), -1)
    setup = _number(row.get("l2_setup_score"))
    coverage = _number(row.get("l2_confidence_pct")) or 0.0
    ticker = str(row.get("ticker") or "")
    return (status_rank, setup if setup is not None else -1.0, coverage, tuple(-ord(char) for char in ticker))


def progression_rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    status_rank = {"pass": 2, "conditional": 2, "recheck": 1, "rejected": 0}.get(str(row.get("l2_status")), -1)
    progression = _number(row.get("l2_progression_score"))
    setup = _number(row.get("l2_setup_score"))
    coverage = _number(row.get("l2_confidence_pct")) or 0.0
    ticker = str(row.get("ticker") or "")
    return (status_rank, progression if progression is not None else -1.0, setup if setup is not None else -1.0, coverage, tuple(-ord(char) for char in ticker))


def run(candidates: list[dict[str, Any]], rules: dict[str, Any], rules_hash: str, keep: int = 120, *, ruleset_version: str | None = None, ruleset_hash: str | None = None) -> dict[str, Any]:
    effective_version = str(ruleset_version or rules.get("ruleset_version") or "")
    effective_hash = str(ruleset_hash or rules_hash)
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        adapted = adapt_l1_candidate(candidate)
        result = apply_selection_contract(classify(adapted, rules, effective_hash), rules)
        result["ruleset_version"] = effective_version
        result["ruleset_hash"] = effective_hash
        result["l2_rules_hash"] = rules_hash
        results.append({**adapted, **result})

    base_ranked = sorted(results, key=setup_rank_key, reverse=True)
    base_candidates = [row for row in base_ranked if row["l2_status"] in {"pass", "conditional", "recheck"} and row.get("l2_setup_score") is not None]
    if len(base_candidates) < keep:
        raise ValueError(f"only {len(base_candidates)} rankable L2 candidates for keep={keep}")
    base_finalists = base_candidates[:keep]
    base_cutoff = float(base_finalists[-1]["l2_setup_score"])
    max_bonus = float((rules.get("near_cutoff_research_rescue") or {}).get("maximum_bonus_points", 0.0))
    rescue_floor = base_cutoff - max_bonus

    for row in results:
        setup = _number(row.get("l2_setup_score"))
        bonus = 0.0
        tier = "outside_rescue_band"
        if setup is not None and setup >= rescue_floor and str(row.get("l2_status")) in {"pass", "conditional", "recheck"}:
            bonus, tier = research_rescue(row, rules)
            bonus = max(0.0, min(max_bonus, bonus))
        row["l2_research_rescue_bonus"] = round(bonus, 4)
        row["l2_research_rescue_tier"] = tier
        row["l2_progression_score"] = round(setup + bonus, 4) if setup is not None else None

    ranked = sorted(results, key=progression_rank_key, reverse=True)
    finalists = [row for row in ranked if row["l2_status"] in {"pass", "conditional", "recheck"} and row.get("l2_progression_score") is not None][:keep]
    finalist_keys = {(str(row.get("ticker") or "").upper(), str(row.get("contract_id") or "")) for row in finalists}
    base_keys = {(str(row.get("ticker") or "").upper(), str(row.get("contract_id") or "")) for row in base_finalists}
    for row in ranked:
        row["selected_for_next_stage"] = (str(row.get("ticker") or "").upper(), str(row.get("contract_id") or "")) in finalist_keys
    rescued = sorted(f"{t}:{c}" for t, c in finalist_keys - base_keys)
    displaced = sorted(f"{t}:{c}" for t, c in base_keys - finalist_keys)
    final_cutoff = _number(finalists[-1].get("l2_progression_score")) if finalists else None
    return {
        "ruleset_version": effective_version,
        "ruleset_hash": effective_hash,
        "l2_rules_hash": rules_hash,
        "selection_model_version": str((rules.get("selection_setup") or {}).get("model_version") or ""),
        "input_count": len(candidates),
        "processed_count": len(results),
        "status_counts": {status: sum(row["l2_status"] == status for row in results) for status in ("pass", "conditional", "recheck", "rejected")},
        "global_ranking": True,
        "finalist_ceiling": keep,
        "base_setup_cutoff_score": round(base_cutoff, 4),
        "rescue_floor_setup_score": round(rescue_floor, 4),
        "final_progression_cutoff_score": final_cutoff,
        "research_rescued_count": len(rescued),
        "research_rescued_identities": rescued,
        "research_displaced_identities": displaced,
        "finalists": finalists,
        "all_results": ranked,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--rules", type=Path, default=Path(__file__).resolve().parent.parent / "assets" / "l2-rules.json")
    parser.add_argument("--keep", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ruleset-version")
    parser.add_argument("--ruleset-hash")
    args = parser.parse_args()
    if not 1 <= args.keep <= 120:
        parser.error("keep must be between 1 and 120")
    rules_bytes = args.rules.read_bytes()
    result = run(load_candidates(args.input), json.loads(rules_bytes), hashlib.sha256(rules_bytes).hexdigest(), args.keep, ruleset_version=args.ruleset_version, ruleset_hash=args.ruleset_hash)
    atomic_write_json(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
