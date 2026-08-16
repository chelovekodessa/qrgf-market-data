#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "screening" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import batch_l2
import classify_l2
import bulk_prefilter


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    base = {"ticker": "BOUNDARY", "return_3m_pct": 1.0, "return_6m_pct": 2.0, "return_12m_pct": 3.0}
    s252, _ = classify_l2.derive_history({**base, "trading_history_days": 252})
    s253, _ = classify_l2.derive_history({**base, "trading_history_days": 253})
    check(s252 == "limited_but_usable", f"252 boundary regressed: {s252}")
    check(s253 == "full", f"253 boundary regressed: {s253}")

    mature, missing = classify_l2.derive_history({
        "ticker": "MATURE", "return_3m_pct": 1.0, "return_6m_pct": 2.0, "return_12m_pct": None,
        "listing_date": "2020-01-02", "as_of": "2026-08-12",
    })
    check(mature == "full" and "return_12m_pct" in missing, f"listing fallback failed: {mature}, {missing}")

    base_row = {
        "ticker": "GAP", "sources": ["first"], "source_conflicts": [], "_field_meta": {},
        "trading_history_days": 180, "return_3m": 1.0, "return_6m": 2.0, "return_12m": None,
        "momentum_history_status": "limited_but_usable",
    }
    gap_item = {"ticker": "GAP", "sources": ["gap-source"], "source_conflicts": [], "_field_meta": {}, "momentum_history_status": "source_gap"}
    bulk_prefilter._merge_item(base_row, gap_item)
    check(base_row["momentum_history_status"] == "unknown", f"source_gap lost: {base_row['momentum_history_status']}")
    check(base_row.get("_explicit_history_source_gap") is True, "source_gap sticky marker missing")
    followup = {
        "ticker": "GAP", "sources": ["followup"], "source_conflicts": [], "_field_meta": {},
        "trading_history_days": 180, "return_3m": 1.0, "return_6m": 2.0, "momentum_history_status": "limited_but_usable",
    }
    bulk_prefilter._merge_item(base_row, followup)
    check(base_row["momentum_history_status"] == "unknown", "later update erased source_gap")

    rules = {
        "ruleset_version": "5.0.0",
        "selection_setup": {
            "model_version": "2.2.0",
            "weights": {"prior_growth": 20, "pullback_geometry": 25, "liquidity": 15, "data_completeness": 10},
            "require_all_components": True,
        },
        "quality_rescue": {
            "enabled": True,
            "minimum_quality_score": 70.0,
            "minimum_coverage_pct": 60.0,
            "max_bonus_points": 2.0,
        },
    }
    complete = batch_l2.apply_selection_contract({
        "l2_status": "conditional",
        "research_priority_score": 99.0,
        "research_priority_coverage_pct": 70.0,
        "research_components": {
            "prior_growth": 80.0,
            "pullback_geometry": 70.0,
            "liquidity": 90.0,
            "data_completeness": 88.0,
            "quality_prior": None,
            "room_to_target": None,
        },
    }, rules)
    check(complete["l2_setup_score"] is not None and complete["l2_confidence_pct"] == 100.0, "fixed setup score failed")
    check(complete["l2_quality_prior_score"] is None and complete["l2_room_to_target_score"] is None, "optional unknowns were invented")
    check(complete["l2_quality_rescue_bonus"] == 0.0, "unknown quality changed L2 progression")
    check(complete["research_priority_score"] == complete["l2_setup_score"], "compatibility score does not use fixed setup")

    strong = batch_l2.apply_selection_contract({
        **complete,
        "quality_prior_score": 98.0,
        "quality_prior_coverage_pct": 100.0,
    }, rules)
    check(strong["l2_quality_rescue_bonus"] > 1.5, "strong quality did not receive bounded rescue")
    check(strong["l2_progression_score"] > strong["l2_setup_score"], "quality rescue did not affect progression")
    check(strong["l2_quality_rescue_bonus"] <= 2.0, "quality rescue exceeded configured cap")

    incomplete = batch_l2.apply_selection_contract({
        "l2_status": "conditional",
        "research_priority_score": 99.0,
        "research_priority_coverage_pct": 50.0,
        "research_components": {
            "prior_growth": 80.0,
            "pullback_geometry": 70.0,
            "liquidity": 90.0,
            "data_completeness": None,
            "quality_prior": None,
            "room_to_target": None,
        },
    }, rules)
    check(incomplete["l2_setup_score"] is None, "missing setup component was renormalized away")
    check(incomplete["l2_status"] == "recheck" and "selection_setup_components" in incomplete["checks_missing"], "missing setup did not fail closed")

    print("producer contract regression tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
