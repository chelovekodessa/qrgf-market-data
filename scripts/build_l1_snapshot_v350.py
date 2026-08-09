#!/usr/bin/env python3
"""QRGF 3.5 production wrapper for the L1 snapshot builder.

Hardens the generic builder without duplicating its calculation logic: Nasdaq
reference data are attempted through bounded requests and a curl/browser-style
fallback, Alpaca short histories are extended up to five years, and short
history is treated as young only when listing evidence supports that conclusion.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import urllib.parse
from typing import Any

import build_l1_snapshot as core

_ORIGINAL_ALPACA_HISTORY = core.fetch_alpaca_history
_ORIGINAL_METRICS = core.metrics_for


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"n/a", "na", "none", "null", "--"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _reference_from_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    result: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for row in rows:
        ticker = str(row.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        item = {
            "market_cap": core.parse_number(row.get("marketCap")),
            "sector": str(row.get("sector") or "").strip() or None,
            "industry": str(row.get("industry") or "").strip() or None,
            "nasdaq_last_sale": core.parse_number(row.get("lastsale")),
            "nasdaq_volume": core.parse_number(row.get("volume")),
            "ipo_year": _parse_int(row.get("ipoyear", row.get("ipoYear"))),
        }
        if ticker in result and result[ticker] != item:
            duplicates.append(ticker)
            continue
        result[ticker] = item
    return result, sorted(set(duplicates))


def _nasdaq_page(limit: int, offset: int) -> tuple[list[dict[str, Any]], int | None, dict[str, str]]:
    params = urllib.parse.urlencode({"limit": str(limit), "offset": str(offset), "tableonly": "true"})
    status, payload, headers = core.http_json(
        f"{core.NASDAQ_SCREENER_URL}?{params}",
        {
            "User-Agent": core.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
            "Origin": "https://www.nasdaq.com",
        },
        attempts=2,
        timeout=30,
    )
    if status != 200:
        raise RuntimeError(f"Nasdaq screener page failed with HTTP {status}")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("Nasdaq screener returned invalid data root")
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        raise RuntimeError("Nasdaq screener rows are not a list")
    total = _parse_int(data.get("totalrecords", data.get("totalRecords")))
    return [dict(row) for row in rows if isinstance(row, dict)], total, headers


def _fetch_nasdaq_curl() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    url = f"{core.NASDAQ_SCREENER_URL}?download=true&limit=10000&offset=0&tableonly=true"
    command = [
        "curl", "--fail", "--silent", "--show-error", "--compressed", "--http1.1",
        "--connect-timeout", "20", "--max-time", "120",
        "-H", f"User-Agent: {core.USER_AGENT}",
        "-H", "Accept: application/json, text/plain, */*",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", "Referer: https://www.nasdaq.com/market-activity/stocks/screener",
        "-H", "Origin: https://www.nasdaq.com",
        url,
    ]
    proc = subprocess.run(command, text=True, capture_output=True, timeout=135)
    if proc.returncode != 0:
        raise RuntimeError(f"curl Nasdaq request failed rc={proc.returncode}: {proc.stderr[-500:]}")
    payload = json.loads(proc.stdout)
    data = payload.get("data") or {}
    rows = data.get("rows") or [] if isinstance(data, dict) else []
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("curl Nasdaq response has no rows")
    result, duplicates = _reference_from_rows([dict(row) for row in rows if isinstance(row, dict)])
    if not result:
        raise RuntimeError("curl Nasdaq response produced zero symbols")
    return result, {
        "http_status": 200,
        "rows": len(rows),
        "unique_symbols": len(result),
        "reported_total": _parse_int(data.get("totalrecords", data.get("totalRecords"))) if isinstance(data, dict) else None,
        "pages": 1,
        "page_size": len(rows),
        "duplicate_conflicts": duplicates,
        "request_ids": [],
        "mode": "curl_full_download_http1_1",
    }


def fetch_nasdaq_reference() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Fetch public Nasdaq reference data without turning a transport failure into guessed values."""
    errors: list[str] = []
    for page_size in (500, 250):
        try:
            result: dict[str, dict[str, Any]] = {}
            duplicates: list[str] = []
            request_ids: list[str] = []
            offset = 0
            raw_rows = 0
            total: int | None = None
            pages = 0
            while pages < 80:
                rows, reported_total, headers = _nasdaq_page(page_size, offset)
                pages += 1
                raw_rows += len(rows)
                if reported_total is not None:
                    total = reported_total
                request_id = headers.get("x-request-id") or headers.get("x-amzn-requestid")
                if request_id:
                    request_ids.append(request_id)
                parsed, page_duplicates = _reference_from_rows(rows)
                duplicates.extend(page_duplicates)
                for ticker, item in parsed.items():
                    if ticker in result and result[ticker] != item:
                        duplicates.append(ticker)
                        continue
                    result[ticker] = item
                if not rows:
                    break
                offset += len(rows)
                if total is not None and offset >= total:
                    break
                if total is None and len(rows) < page_size:
                    break
            if result:
                return result, {
                    "http_status": 200,
                    "rows": raw_rows,
                    "unique_symbols": len(result),
                    "reported_total": total,
                    "pages": pages,
                    "page_size": page_size,
                    "duplicate_conflicts": sorted(set(duplicates)),
                    "request_ids": request_ids,
                    "mode": "bounded_pagination_without_download_flag",
                }
            raise RuntimeError("Nasdaq pagination returned zero symbols")
        except Exception as exc:
            errors.append(f"page_size={page_size}:{type(exc).__name__}:{exc}")
    try:
        result, meta = _fetch_nasdaq_curl()
        meta["prior_attempt_errors"] = errors
        return result, meta
    except Exception as exc:
        errors.append(f"curl:{type(exc).__name__}:{exc}")
    raise RuntimeError("Nasdaq reference failed: " + " | ".join(errors)[-2000:])


def fetch_alpaca_history(symbols: list[str], start: str, end: str, batch_size: int):
    """Extend only short-but-present histories to distinguish source truncation."""
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
    sessions = int(result.get("trading_history_days") or 0)
    if 0 < sessions < 253:
        ref = reference or {}
        ipo_year = _parse_int(ref.get("ipo_year"))
        current_year = dt.datetime.now(dt.timezone.utc).year
        if ipo_year is not None and ipo_year <= current_year - 2:
            result["momentum_history_status"] = "source_gap"
            result["history_data_status"] = "source_gap_mature_listing"
        elif ipo_year is None:
            result["momentum_history_status"] = "unknown"
            result["history_data_status"] = "short_history_without_listing_evidence"
    return result


core.fetch_nasdaq_reference = fetch_nasdaq_reference
core.fetch_alpaca_history = fetch_alpaca_history
core.metrics_for = metrics_for


if __name__ == "__main__":
    raise SystemExit(core.main())
