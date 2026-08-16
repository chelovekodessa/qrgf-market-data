#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "screening" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import sec_quality_prior as quality


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def annual(val, start, end, filed, form="20-F"):
    return {"val": val, "start": start, "end": end, "filed": filed, "form": form}


def instant(val, end, filed, form="6-K"):
    return {"val": val, "end": end, "filed": filed, "form": form}


def main() -> int:
    payload = {
        "facts": {
            "ifrs-full": {
                "Revenue": {"units": {"TWD": [
                    annual(100, "2024-01-01", "2024-12-31", "2025-03-01"),
                    annual(120, "2025-01-01", "2025-12-31", "2026-03-01"),
                    annual(999, "2026-01-01", "2026-12-31", "2027-03-01"),
                ]}},
                "ProfitLoss": {"units": {"TWD": [annual(20, "2025-01-01", "2025-12-31", "2026-03-01")]}},
                "CashFlowsFromUsedInOperatingActivities": {"units": {"TWD": [annual(30, "2025-01-01", "2025-12-31", "2026-03-01")]}},
                "Equity": {"units": {"TWD": [instant(50, "2026-06-30", "2026-07-20")]}},
            }
        }
    }
    prior = quality.derive_quality_prior(payload, dt.date(2026, 8, 14))
    check(prior["quality_prior_status"] == "usable", f"IFRS quality prior unusable: {prior}")
    check(prior["quality_prior_coverage_pct"] == 100.0, f"coverage wrong: {prior}")
    check(float(prior["quality_prior_score"]) > 90.0, f"strong quality scored too low: {prior}")
    check(prior["quality_prior_filing_date"] == "2026-07-20", "future filing leaked into point-in-time prior")

    config = {"minimum_coverage_pct": 60, "minimum_quality_score": 70, "max_bonus_points": 2}
    bonus = quality.quality_rescue_bonus(89.29, prior["quality_prior_score"], prior["quality_prior_coverage_pct"], config)
    check(1.0 < bonus <= 2.0, f"bounded quality rescue failed: {bonus}")
    check(quality.quality_rescue_bonus(89.29, None, None, config) == 0.0, "unknown quality was invented")
    check(quality.quality_rescue_bonus(89.29, 60.0, 100.0, config) == 0.0, "weak quality received a bonus")

    base = {
        "all_results": [
            {"ticker": f"T{i}", "contract_id": str(i), "l2_status": "conditional", "l2_setup_score": 100 - i * 0.1}
            for i in range(200)
        ]
    }
    cohort, cutoff = quality.candidate_cohort(base, 120, 2.0)
    check(len(cohort) == 140, f"cutoff cohort is not mathematically bounded: {len(cohort)}")
    check(abs(cutoff - 88.1) < 1e-9, f"unexpected base cutoff: {cutoff}")

    print("SEC quality prior regression tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
