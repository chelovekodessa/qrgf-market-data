#!/usr/bin/env python3
"""Safe read-only preflight for Alpaca historical US-equity market data.

Uses only https://data.alpaca.markets. It never calls trading/order endpoints and
never prints API credentials. The JSON result is safe to commit for inspection.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://data.alpaca.markets/v2/stocks/bars"
SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "V", "MA",
    "XOM", "CVX", "UNH", "JNJ", "PFE", "ABBV", "LLY", "COST", "WMT", "HD",
    "LOW", "PG", "KO", "PEP", "MCD", "NKE", "DIS", "NFLX", "CRM", "ORCL",
    "IBM", "INTC", "AMD", "QCOM", "AVGO", "MU", "TXN", "AMAT", "LRCX", "KLAC",
    "ADI", "NOW", "INTU", "ADBE", "SPY", "QQQ", "IWM", "DIA", "TSM", "ASML",
]


def request_json(params: dict[str, str], key_id: str, secret_key: str) -> tuple[int, dict, dict[str, str]]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{BASE_URL}?{query}",
        headers={
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
            "User-Agent": "qrgf-alpaca-preflight/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
            headers = {
                "x-request-id": response.headers.get("X-Request-ID", ""),
                "x-ratelimit-limit": response.headers.get("X-RateLimit-Limit", ""),
                "x-ratelimit-remaining": response.headers.get("X-RateLimit-Remaining", ""),
            }
            return response.status, payload, headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"message": body[:1000]}
        return exc.code, payload, {
            "x-request-id": exc.headers.get("X-Request-ID", "") if exc.headers else "",
            "x-ratelimit-limit": exc.headers.get("X-RateLimit-Limit", "") if exc.headers else "",
            "x-ratelimit-remaining": exc.headers.get("X-RateLimit-Remaining", "") if exc.headers else "",
        }


def main() -> int:
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "data/preflight/alpaca.json")
    output.parent.mkdir(parents=True, exist_ok=True)

    key_id = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret_key = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    now = datetime.now(timezone.utc)
    end_date = (now - timedelta(days=1)).date()
    start_date = end_date - timedelta(days=400)

    result: dict = {
        "schema_version": "1.0.0",
        "test": "alpaca_free_historical_sip_preflight",
        "tested_at": now.isoformat(),
        "endpoint": BASE_URL,
        "start": str(start_date),
        "end": str(end_date),
        "requested_feed": "sip",
        "timeframe": "1Day",
        "adjustment": "all",
        "requested_symbols": SYMBOLS,
        "credentials_present": bool(key_id and secret_key),
        "credentials_exposed": False,
        "pages": 0,
        "total_bars": 0,
        "bars_by_symbol": {},
        "symbols_with_data": 0,
        "missing_symbols": [],
        "pagination_exercised": False,
        "sip_http_status": None,
        "sip_request_ids": [],
        "rate_limit": {},
        "aapl_sip_vs_iex": {},
        "checks": {},
        "passed": False,
        "error": None,
    }

    if not key_id or not secret_key:
        result["error"] = "missing_GitHub_Actions_secrets"
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 2

    counts: Counter[str] = Counter()
    latest_aapl_bar: dict | None = None
    token: str | None = None

    try:
        for _ in range(20):
            params = {
                "symbols": ",".join(SYMBOLS),
                "timeframe": "1Day",
                "start": str(start_date),
                "end": str(end_date),
                "limit": "10000",
                "adjustment": "all",
                "feed": "sip",
                "sort": "asc",
            }
            if token:
                params["page_token"] = token
            status, payload, headers = request_json(params, key_id, secret_key)
            result["sip_http_status"] = status
            if headers.get("x-request-id"):
                result["sip_request_ids"].append(headers["x-request-id"])
            if headers.get("x-ratelimit-limit") or headers.get("x-ratelimit-remaining"):
                result["rate_limit"] = {
                    "limit": headers.get("x-ratelimit-limit"),
                    "remaining_after_last_page": headers.get("x-ratelimit-remaining"),
                }
            if status != 200:
                result["error"] = {
                    "stage": "sip_historical_request",
                    "http_status": status,
                    "provider_response": payload,
                }
                break

            result["pages"] += 1
            bars = payload.get("bars") or {}
            if not isinstance(bars, dict):
                raise ValueError("unexpected bars payload shape")
            for symbol, rows in bars.items():
                if not isinstance(rows, list):
                    continue
                counts[symbol] += len(rows)
                result["total_bars"] += len(rows)
                if symbol == "AAPL" and rows:
                    latest_aapl_bar = rows[-1]

            token = payload.get("next_page_token")
            if not token:
                break
        else:
            result["error"] = "pagination_exceeded_20_pages"

        result["bars_by_symbol"] = dict(sorted(counts.items()))
        result["symbols_with_data"] = sum(1 for symbol in SYMBOLS if counts.get(symbol, 0) > 0)
        result["missing_symbols"] = [symbol for symbol in SYMBOLS if counts.get(symbol, 0) == 0]
        result["pagination_exercised"] = result["pages"] >= 2

        if result["error"] is None and latest_aapl_bar:
            bar_day = str(latest_aapl_bar.get("t", ""))[:10]
            status, payload, headers = request_json(
                {
                    "symbols": "AAPL",
                    "timeframe": "1Day",
                    "start": bar_day,
                    "end": bar_day,
                    "limit": "10",
                    "adjustment": "all",
                    "feed": "iex",
                    "sort": "asc",
                },
                key_id,
                secret_key,
            )
            iex_rows = (payload.get("bars") or {}).get("AAPL", []) if status == 200 else []
            sip_volume = latest_aapl_bar.get("v")
            iex_volume = iex_rows[-1].get("v") if iex_rows else None
            ratio = None
            if isinstance(sip_volume, (int, float)) and isinstance(iex_volume, (int, float)) and iex_volume > 0:
                ratio = sip_volume / iex_volume
            result["aapl_sip_vs_iex"] = {
                "date": bar_day,
                "iex_http_status": status,
                "sip_volume": sip_volume,
                "iex_volume": iex_volume,
                "sip_to_iex_volume_ratio": ratio,
                "different_volumes": bool(ratio is not None and abs(ratio - 1.0) > 0.05),
                "iex_request_id": headers.get("x-request-id", ""),
            }

        result["checks"] = {
            "historical_sip_authorized": result["sip_http_status"] == 200,
            "substantial_history_returned": result["total_bars"] >= 10000,
            "pagination_works": result["pagination_exercised"],
            "aapl_has_252_sessions": counts.get("AAPL", 0) >= 252,
            "broad_symbol_sample_coverage": result["symbols_with_data"] >= int(len(SYMBOLS) * 0.90),
            "sip_differs_from_iex": bool(result.get("aapl_sip_vs_iex", {}).get("different_volumes")),
        }
        result["passed"] = result["error"] is None and all(result["checks"].values())
    except Exception as exc:
        result["error"] = {"stage": "client_validation", "type": type(exc).__name__, "message": str(exc)[:1000]}
        result["passed"] = False

    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "passed": result["passed"],
        "sip_http_status": result["sip_http_status"],
        "pages": result["pages"],
        "total_bars": result["total_bars"],
        "symbols_with_data": result["symbols_with_data"],
        "missing_symbols": result["missing_symbols"],
        "checks": result["checks"],
        "error": result["error"],
    }, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
