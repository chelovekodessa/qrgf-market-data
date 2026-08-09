#!/usr/bin/env python3
"""Build a compact, deterministic L1 market-data snapshot for QRGF.

Inputs are the checked-in L0 pages/manifest. Alpaca SIP daily bars are used for
price/volume history. Nasdaq Screener is used only for reference market cap,
sector and industry. The script makes no trading requests and never prints
credentials. It may publish an incomplete manifest for diagnostics; consumers
must require ``complete=true``.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
USER_AGENT = "Mozilla/5.0 (compatible; QRGFMarketDataBridge/1.0; +https://github.com/)"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text or text.lower() in {"n/a", "na", "none", "null", "--", "nan"}:
        return None
    multiplier = 1.0
    if text[-1:].upper() in {"K", "M", "B", "T"}:
        suffix = text[-1:].upper()
        text = text[:-1]
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[suffix]
    try:
        number = float(text) * multiplier
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def http_json(url: str, headers: dict[str, str], *, attempts: int = 5, timeout: int = 90) -> tuple[int, dict[str, Any], dict[str, str]]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON response root is not an object")
                return response.status, payload, {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"message": body[:1000]}
            if exc.code == 429 and attempt + 1 < attempts:
                retry = 65
                raw_retry = exc.headers.get("Retry-After") if exc.headers else None
                if raw_retry and str(raw_retry).isdigit():
                    retry = max(retry, int(raw_retry))
                time.sleep(retry)
                continue
            if 500 <= exc.code < 600 and attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            return exc.code, payload if isinstance(payload, dict) else {"message": str(payload)}, {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            break
    raise RuntimeError(f"HTTP JSON request failed: {last_error}")


def validate_and_load_l0(manifest_path: Path, pages_dir: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    manifest = load_json(manifest_path)
    if manifest.get("complete") is not True:
        raise ValueError("L0 manifest is not complete")
    declared = manifest.get("pages") or []
    if not isinstance(declared, list) or not declared:
        raise ValueError("L0 manifest has no pages")
    rows: list[dict[str, str]] = []
    for expected_index, item in enumerate(declared, start=1):
        if not isinstance(item, dict):
            raise ValueError("invalid L0 page declaration")
        name = str(item.get("name") or "")
        expected_name = f"page-{expected_index:04d}.csv"
        if name != expected_name:
            raise ValueError(f"unexpected L0 page order/name: {name}")
        path = pages_dir / name
        if not path.exists():
            raise ValueError(f"missing L0 page {name}")
        if sha256_file(path) != str(item.get("sha256") or ""):
            raise ValueError(f"L0 page checksum mismatch: {name}")
        page_rows = read_csv(path)
        if len(page_rows) != int(item.get("rows") or -1):
            raise ValueError(f"L0 page row-count mismatch: {name}")
        rows.extend(page_rows)
    if len(rows) != int(manifest.get("accepted_unique") or -1):
        raise ValueError("L0 total accepted count mismatch")
    seen: set[tuple[str, str]] = set()
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        contract_id = str(row.get("contract_id") or "").strip()
        if not ticker or not contract_id:
            raise ValueError("L0 row is missing ticker/contract_id")
        key = (ticker, contract_id)
        if key in seen:
            raise ValueError(f"duplicate L0 identity: {ticker}/{contract_id}")
        seen.add(key)
    return manifest, rows


def fetch_nasdaq_reference() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    params = urllib.parse.urlencode({"download": "true", "limit": "10000", "offset": "0", "tableonly": "true"})
    status, payload, headers = http_json(
        f"{NASDAQ_SCREENER_URL}?{params}",
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
            "Origin": "https://www.nasdaq.com",
        },
        attempts=4,
    )
    data = payload.get("data") or {}
    rows = data.get("rows") or [] if isinstance(data, dict) else []
    if status != 200 or not isinstance(rows, list):
        raise RuntimeError(f"Nasdaq screener failed with HTTP {status}")
    result: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        item = {
            "market_cap": parse_number(row.get("marketCap")),
            "sector": str(row.get("sector") or "").strip() or None,
            "industry": str(row.get("industry") or "").strip() or None,
            "nasdaq_last_sale": parse_number(row.get("lastsale")),
            "nasdaq_volume": parse_number(row.get("volume")),
        }
        if ticker in result and result[ticker] != item:
            duplicates.append(ticker)
            continue
        result[ticker] = item
    return result, {
        "http_status": status,
        "rows": len(rows),
        "unique_symbols": len(result),
        "duplicate_conflicts": sorted(set(duplicates)),
        "request_id": headers.get("x-request-id") or headers.get("x-amzn-requestid"),
    }


def iter_batches(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def fetch_alpaca_history(symbols: list[str], start: str, end: str, batch_size: int) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    key_id = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret_key = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    if not key_id or not secret_key:
        raise RuntimeError("APCA_API_KEY_ID/APCA_API_SECRET_KEY are missing")

    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    request_count = 0
    pages = 0
    rate_limit = None
    remaining = None
    provider_errors: list[dict[str, Any]] = []
    for batch_index, batch in enumerate(iter_batches(symbols, batch_size), start=1):
        token: str | None = None
        for page_index in range(1, 30):
            params = {
                "symbols": ",".join(batch),
                "timeframe": "1Day",
                "start": start,
                "end": end,
                "limit": "10000",
                "adjustment": "split",
                "feed": "sip",
                "sort": "asc",
            }
            if token:
                params["page_token"] = token
            status, payload, headers = http_json(
                f"{ALPACA_BARS_URL}?{urllib.parse.urlencode(params)}",
                {
                    "APCA-API-KEY-ID": key_id,
                    "APCA-API-SECRET-KEY": secret_key,
                    "Accept": "application/json",
                    "User-Agent": "qrgf-l1-bridge/1.0",
                },
                attempts=5,
            )
            request_count += 1
            pages += 1
            rate_limit = headers.get("x-ratelimit-limit", rate_limit)
            remaining = headers.get("x-ratelimit-remaining", remaining)
            if status != 200:
                provider_errors.append({
                    "batch": batch_index,
                    "page": page_index,
                    "http_status": status,
                    "message": str(payload.get("message") or payload.get("code") or "provider_error")[:500],
                })
                break
            bars = payload.get("bars") or {}
            if not isinstance(bars, dict):
                provider_errors.append({"batch": batch_index, "page": page_index, "http_status": status, "message": "unexpected_bars_shape"})
                break
            for symbol, rows in bars.items():
                if isinstance(rows, list):
                    history[str(symbol).upper()].extend(dict(row) for row in rows if isinstance(row, dict))
            token = payload.get("next_page_token")
            if not token:
                break
        else:
            provider_errors.append({"batch": batch_index, "message": "pagination_exceeded_29_pages"})
    return dict(history), {
        "requests": request_count,
        "pages": pages,
        "batch_size": batch_size,
        "feed": "sip",
        "adjustment": "split",
        "timeframe": "1Day",
        "rate_limit": rate_limit,
        "rate_limit_remaining": remaining,
        "errors": provider_errors,
    }


def normalize_bars(rows: list[dict[str, Any]]) -> tuple[list[tuple[str, float, float]], str | None]:
    by_date: dict[str, tuple[str, float, float]] = {}
    conflict = False
    for row in rows:
        timestamp = str(row.get("t") or "")
        day = timestamp[:10]
        close = parse_number(row.get("c"))
        volume = parse_number(row.get("v"))
        if len(day) != 10 or close is None or close <= 0 or volume is None or volume < 0:
            continue
        item = (day, close, volume)
        if day in by_date and by_date[day] != item:
            conflict = True
        else:
            by_date[day] = item
    if conflict:
        return [], "duplicate_bar_conflict"
    ordered = [by_date[day] for day in sorted(by_date)]
    return ordered, None


def ret(closes: list[float], sessions: int) -> float | None:
    if len(closes) <= sessions or closes[-1 - sessions] <= 0:
        return None
    return 100.0 * (closes[-1] / closes[-1 - sessions] - 1.0)


def volatility(closes: list[float], sessions: int = 30) -> float | None:
    if len(closes) < sessions + 1:
        return None
    values = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - sessions, len(closes))]
    return statistics.stdev(values) * math.sqrt(252) * 100.0 if len(values) >= 2 else None


def metrics_for(row: dict[str, str], bars: list[dict[str, Any]], reference: dict[str, Any] | None) -> dict[str, Any]:
    normalized, history_error = normalize_bars(bars)
    closes = [item[1] for item in normalized]
    ref = reference or {}
    security_type = str(row.get("security_type") or "").strip().lower()
    market_cap = ref.get("market_cap") if security_type != "etf" else None
    if normalized:
        dollars = [close * volume for _, close, volume in normalized[-63:]]
        drawdown = 100.0 * (1.0 - closes[-1] / max(closes[-252:])) if closes else None
    else:
        dollars = []
        drawdown = None
    history_sessions = len(closes)
    if history_error:
        status = "source_gap"
    elif history_sessions >= 253:
        status = "full"
    elif history_sessions >= 127:
        status = "limited_but_usable"
    else:
        status = "insufficient"
    return {
        "ticker": str(row.get("ticker") or "").strip().upper(),
        "company": row.get("company_name"),
        "contract_id": row.get("contract_id"),
        "security_type": security_type,
        "instrument_status": row.get("instrument_status"),
        "exchange": row.get("listing_exchange"),
        "sector": ref.get("sector"),
        "industry": ref.get("industry"),
        "price": closes[-1] if closes else None,
        "market_cap": market_cap,
        "avg_dollar_volume": (sum(dollars) / len(dollars)) if dollars else None,
        "return_1m": ret(closes, 21),
        "return_3m": ret(closes, 63),
        "return_6m": ret(closes, 126),
        "return_12m": ret(closes, 252),
        "drawdown_52w": drawdown,
        "historical_volatility": volatility(closes, 30),
        "trading_history_days": history_sessions,
        "momentum_history_status": status,
        "history_data_status": history_error or ("usable" if normalized else "missing"),
        "as_of": normalized[-1][0] if normalized else None,
        "percent_unit": "percent",
        "market_cap_applicability": "not_applicable" if security_type == "etf" else "required",
        "market_cap_source": None if security_type == "etf" else ("nasdaq_screener" if market_cap is not None else None),
    }


def write_csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    fields = [
        "ticker", "company", "contract_id", "security_type", "instrument_status", "exchange", "sector", "industry",
        "price", "market_cap", "avg_dollar_volume", "return_1m", "return_3m", "return_6m", "return_12m",
        "drawdown_52w", "historical_volatility", "trading_history_days", "momentum_history_status", "history_data_status",
        "as_of", "percent_unit", "market_cap_applicability", "market_cap_source",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fields})
    return stream.getvalue().encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--l0-manifest", type=Path, required=True)
    parser.add_argument("--l0-pages-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--history-calendar-days", type=int, default=400)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--producer-file", type=Path, action="append", default=[])
    args = parser.parse_args()

    l0_manifest, universe = validate_and_load_l0(args.l0_manifest, args.l0_pages_dir)
    symbols = [str(row["ticker"]).strip().upper() for row in universe]
    if len(set(symbols)) != len(symbols):
        raise ValueError("L0 ticker duplication is unsupported in L1 provider mapping")

    now = dt.datetime.now(dt.timezone.utc)
    end = now.date() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=args.history_calendar_days)

    reference, nasdaq_meta = fetch_nasdaq_reference()
    history, alpaca_meta = fetch_alpaca_history(symbols, str(start), str(end), args.batch_size)

    rows = [metrics_for(row, history.get(str(row["ticker"]).strip().upper(), []), reference.get(str(row["ticker"]).strip().upper())) for row in universe]
    rows.sort(key=lambda item: (str(item.get("ticker") or ""), str(item.get("contract_id") or "")))

    non_etf = [row for row in rows if row.get("security_type") != "etf"]
    no_history = [row for row in rows if int(row.get("trading_history_days") or 0) == 0]
    mature_like_missing_core = [
        row for row in non_etf
        if int(row.get("trading_history_days") or 0) >= 253 and (
            row.get("price") is None or row.get("market_cap") is None or row.get("avg_dollar_volume") is None
            or row.get("return_3m") is None or row.get("return_6m") is None or row.get("return_12m") is None
            or row.get("drawdown_52w") is None or row.get("historical_volatility") is None
        )
    ]
    market_cap_missing = [row for row in non_etf if row.get("market_cap") is None]
    provider_errors = list(alpaca_meta.get("errors") or [])

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    csv_bytes = write_csv_bytes(rows)
    gzip_bytes = gzip.compress(csv_bytes, compresslevel=9, mtime=0)
    bundle_bytes = base64.encodebytes(gzip_bytes)
    bundle_path = output / "l1-snapshot.csv.gz.b64"
    bundle_path.write_bytes(bundle_bytes)

    page_size = 250
    pages_dir = output / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    for old in pages_dir.glob("page-*.csv"):
        old.unlink()
    fields = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))).fieldnames or [])
    pages: list[dict[str, Any]] = []
    for start_index in range(0, len(rows), page_size):
        chunk = rows[start_index:start_index + page_size]
        page_stream = io.StringIO(newline="")
        writer = csv.DictWriter(page_stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in chunk:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fields})
        data = page_stream.getvalue().encode("utf-8")
        name = f"page-{start_index // page_size + 1:04d}.csv"
        path = pages_dir / name
        path.write_bytes(data)
        pages.append({"name": name, "rows": len(chunk), "sha256": sha256_bytes(data)})

    producer_hashes = {path.name: sha256_file(path) for path in args.producer_file}
    # Transport completeness is distinct from per-symbol data sufficiency.
    # A full snapshot may contain young/illiquid/source-gap rows; the skill
    # coverage gate decides whether those rows are terminal or unresolved.
    complete = (
        not provider_errors
        and len(rows) == int(l0_manifest.get("accepted_unique") or -1)
    )
    manifest = {
        "schema_version": "1.0.0",
        "complete": complete,
        "source_id": "alpaca_sip_daily_plus_nasdaq_reference",
        "retrieved_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "history_start": str(start),
        "history_end": str(end),
        "history_feed": "sip",
        "history_adjustment": "split",
        "history_timeframe": "1Day",
        "l0_lineage": {
            "manifest_sha256": sha256_file(args.l0_manifest),
            "accepted_unique": int(l0_manifest.get("accepted_unique") or 0),
            "bundle_csv_sha256": (l0_manifest.get("bundle") or {}).get("csv_sha256"),
            "source_raw_sha256": l0_manifest.get("raw_sha256"),
            "retrieved_at": l0_manifest.get("retrieved_at"),
        },
        "rows": len(rows),
        "page_size": page_size,
        "page_count": len(pages),
        "pages": pages,
        "bundle": {
            "name": bundle_path.name,
            "encoding": "base64+gzip",
            "rows": len(rows),
            "sha256": sha256_bytes(bundle_bytes),
            "gzip_sha256": sha256_bytes(gzip_bytes),
            "csv_sha256": sha256_bytes(csv_bytes),
            "bytes": len(bundle_bytes),
            "line_count": bundle_bytes.count(b"\n"),
        },
        "producer_hashes": producer_hashes,
        "alpaca": alpaca_meta,
        "nasdaq_reference": nasdaq_meta,
        "coverage": {
            "total": len(rows),
            "with_history": len(rows) - len(no_history),
            "missing_history": len(no_history),
            "non_etf": len(non_etf),
            "market_cap_present_non_etf": len(non_etf) - len(market_cap_missing),
            "market_cap_missing_non_etf": len(market_cap_missing),
            "mature_missing_core": len(mature_like_missing_core),
        },
        "diagnostics": {
            "missing_history_symbols": [row["ticker"] for row in no_history],
            "missing_market_cap_symbols": [row["ticker"] for row in market_cap_missing],
            "mature_missing_core_symbols": [row["ticker"] for row in mature_like_missing_core],
            "alpaca_provider_errors": provider_errors,
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "complete": complete,
        "rows": len(rows),
        "with_history": len(rows) - len(no_history),
        "missing_history": len(no_history),
        "market_cap_missing_non_etf": len(market_cap_missing),
        "mature_missing_core": len(mature_like_missing_core),
        "alpaca_requests": alpaca_meta.get("requests"),
        "alpaca_errors": len(provider_errors),
        "nasdaq_reference_rows": nasdaq_meta.get("rows"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
