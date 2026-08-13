#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "screening" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

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
    print("producer contract regression tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
