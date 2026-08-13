#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

import build_l1_snapshot as core
from l1_free_support import postprocess_manifest
from l1_market_session import effective_window, regression_check

_ORIGINAL_HISTORY = core.fetch_alpaca_history
_ORIGINAL_METRICS = core.metrics_for


def no_bulk_reference() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    return {}, {"provider": "deferred", "mode": "free_bulk_reference_not_required_for_l1", "market_cap_status": "deferred_to_narrowed_research", "sector_industry_status": "deferred_to_narrowed_research"}


def fetch_alpaca_history(symbols: list[str], start: str, end: str, batch_size: int):
    start, end = effective_window(start, end)
    history, meta = _ORIGINAL_HISTORY(symbols, start, end, batch_size)
    short = [symbol for symbol in symbols if 0 < len(history.get(symbol, [])) < 253]
    extension: dict[str, Any] = {"requested_symbols": len(short), "upgraded_to_full": 0, "still_short": 0, "requests": 0, "errors": []}
    if short:
        extended_start = str(dt.date.fromisoformat(end) - dt.timedelta(days=5 * 366))
        extended, extra = _ORIGINAL_HISTORY(short, extended_start, end, min(batch_size, 100))
        extension["requests"] = int(extra.get("requests") or 0)
        extension["errors"] = list(extra.get("errors") or [])
        for symbol in short:
            candidate = extended.get(symbol) or []
            if len(candidate) > len(history.get(symbol, [])):
                history[symbol] = candidate
            if len(history.get(symbol, [])) >= 253:
                extension["upgraded_to_full"] += 1
            else:
                extension["still_short"] += 1
        meta["requests"] = int(meta.get("requests") or 0) + int(extra.get("requests") or 0)
        meta["pages"] = int(meta.get("pages") or 0) + int(extra.get("pages") or 0)
        meta.setdefault("errors", []).extend(extra.get("errors") or [])
    meta["short_history_extension"] = extension
    meta["effective_history_start"] = start
    meta["effective_history_end"] = end
    return history, meta


def metrics_for(row: dict[str, str], bars: list[dict[str, Any]], reference: dict[str, Any] | None) -> dict[str, Any]:
    result = _ORIGINAL_METRICS(row, bars, reference)
    kind = str(row.get("security_type") or "").strip().lower()
    result["market_cap_applicability"] = "not_applicable" if kind == "etf" else ("optional" if kind == "adr" else "deferred")
    result["market_cap_source"] = None
    return result


core.fetch_nasdaq_reference = no_bulk_reference
core.fetch_alpaca_history = fetch_alpaca_history
core.metrics_for = metrics_for


if __name__ == "__main__":
    regression_check()
    rc = core.main()
    if rc == 0 and "--output-dir" in sys.argv:
        postprocess_manifest(Path(sys.argv[sys.argv.index("--output-dir") + 1]))
    raise SystemExit(rc)
