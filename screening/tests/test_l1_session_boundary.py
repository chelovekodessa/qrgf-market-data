#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_l1_snapshot_free as l1_free


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.timezone.utc)


def main() -> int:
    # Summer EDT cutoff. 20:59 UTC is 16:59 ET; 21:00 UTC is 17:00 ET.
    check(l1_free.latest_completed_session(utc(2026, 8, 13, 20, 59)) == dt.date(2026, 8, 12), "pre-cutoff session boundary regressed")
    check(l1_free.latest_completed_session(utc(2026, 8, 13, 21, 0)) == dt.date(2026, 8, 13), "post-cutoff session boundary regressed")
    check(l1_free.latest_completed_session(utc(2026, 8, 13, 21, 38, 11)) == dt.date(2026, 8, 13), "reported production boundary regressed")

    # Weekend and U.S. market holiday handling.
    check(l1_free.latest_completed_session(utc(2026, 8, 15, 18, 0)) == dt.date(2026, 8, 14), "weekend fallback regressed")
    check(l1_free.latest_completed_session(utc(2026, 9, 8, 20, 0)) == dt.date(2026, 9, 4), "Labor Day fallback regressed")

    # Winter EST cutoff verifies DST-sensitive America/New_York conversion.
    check(l1_free.latest_completed_session(utc(2026, 12, 15, 21, 59)) == dt.date(2026, 12, 14), "winter pre-cutoff boundary regressed")
    check(l1_free.latest_completed_session(utc(2026, 12, 15, 22, 0)) == dt.date(2026, 12, 15), "winter post-cutoff boundary regressed")

    start, end = l1_free.effective_history_window("2025-07-08", "2026-08-12", utc(2026, 8, 13, 21, 38, 11))
    check((start, end) == ("2025-07-09", "2026-08-13"), f"effective history window regressed: {start}, {end}")

    print("L1 session-boundary regression tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
