#!/usr/bin/env python3
"""Final free production L1 builder for QRGF 3.5.

Uses the verified free Alpaca Basic historical SIP feed for split-adjusted daily
price/volume history across the full L0 universe. Market cap is deliberately not
a transport-critical L1 dependency: hosted GitHub runners can be blocked by
otherwise-free reference endpoints. Unknown market cap stays missing/deferred
rather than becoming zero or blocking the full-market recovery screen.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import build_l1_snapshot as core

_ORIGINAL_ALPACA_HISTORY = core.fetch_alpaca_history
_ORIGINAL_METRICS = core.metrics_for


def no_bulk_reference() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    return {}, {
        "provider": "deferred",
        "mode": "free_bulk_reference_not_required_for_l1",
        "market_cap_status": "deferred_to_narrowed_research",
        "sector_industry_status": "deferred_to_narrowed_research",
    }


def fetch_alpaca_history(symbols: list[str], start: str, end: str, batch_size: int):
    """Run the full-universe pass, then extend short histories up to five years."""
    history, meta = _ORIGINAL_ALPACA_HISTORY(symbols, start, end, batch_size)
    short = [symbol for symbol in symbols if 0 < len(history.get(symbol, [])) < 253]
    extension_meta: dict[str, Any] = {
        "requested_symbols": len(short),
        "upgraded_to_full": 0,
        "still_short": 0,
        "requests": 0,
        "errors": [],
    }
    if short:
        end_date = dt.date.fromisoformat(end)
        extended_start = str(end_date - dt.timedelta(days=5 * 366))
        extended, extra = _ORIGINAL_ALPACA_HISTORY(short, extended_start, end, min(batch_size, 100))
        extension_meta["requests"] = int(extra.get("requests") or 0)
        extension_meta["errors"] = list(extra.get("errors") or [])
        for symbol in short:
            candidate = extended.get(symbol) or []
            if len(candidate) > len(history.get(symbol, [])):
                history[symbol] = candidate
            if len(history.get(symbol, [])) >= 253:
                extension_meta["upgraded_to_full"] += 1
            else:
                extension_meta["still_short"] += 1
        meta["requests"] = int(meta.get("requests") or 0) + int(extra.get("requests") or 0)
        meta["pages"] = int(meta.get("pages") or 0) + int(extra.get("pages") or 0)
        meta.setdefault("errors", []).extend(extra.get("errors") or [])
    meta["short_history_extension"] = extension_meta
    return history, meta


def metrics_for(row: dict[str, str], bars: list[dict[str, Any]], reference: dict[str, Any] | None) -> dict[str, Any]:
    result = _ORIGINAL_METRICS(row, bars, reference)
    security_type = str(row.get("security_type") or "").strip().lower()
    if security_type == "etf":
        result["market_cap_applicability"] = "not_applicable"
    elif security_type == "adr":
        result["market_cap_applicability"] = "optional"
    else:
        result["market_cap_applicability"] = "deferred"
    result["market_cap_source"] = None
    return result


def postprocess_manifest() -> None:
    try:
        index = sys.argv.index("--output-dir")
        output_dir = Path(sys.argv[index + 1])
    except (ValueError, IndexError):
        return
    path = output_dir / "manifest.json"
    if not path.exists():
        return
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["source_id"] = "alpaca_sip_daily_free_l1"
    manifest["reference_provider"] = {
        "provider": "deferred",
        "mode": "free_bulk_reference_not_required_for_l1",
        "market_cap_status": "deferred_to_narrowed_research",
    }
    manifest["market_cap_policy"] = {
        "l1_required": False,
        "known_direct_value_may_reject_below_usd": 250000000,
        "missing_value": "deferred_not_zero",
    }
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


core.fetch_nasdaq_reference = no_bulk_reference
core.fetch_alpaca_history = fetch_alpaca_history
core.metrics_for = metrics_for


if __name__ == "__main__":
    rc = core.main()
    if rc == 0:
        postprocess_manifest()
    raise SystemExit(rc)
