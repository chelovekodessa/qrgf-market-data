#!/usr/bin/env python3
"""QRGF 3.5 production wrapper for the L1 snapshot builder.

Primary market history is Alpaca SIP. Reference data first try the public Nasdaq
screener; when that transport is unavailable from GitHub Actions, official SEC
bulk Company Facts provide common-equity shares outstanding so market cap can be
derived as split-adjusted Alpaca price x SEC shares. ADR market cap is never
invented from SEC underlying-share counts because the ADR ratio may differ.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

import build_l1_snapshot as core

_ORIGINAL_ALPACA_HISTORY = core.fetch_alpaca_history
_ORIGINAL_METRICS = core.metrics_for
_TARGET_SYMBOLS: set[str] = set()
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_COMPANYFACTS_ZIP = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SEC_USER_AGENT = "qrgf-market-data-bot/1.0 contact=https://github.com/chelovekodessa/qrgf-market-data"


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
            "reference_source": "nasdaq_screener",
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
        "provider": "nasdaq_screener",
        "http_status": 200,
        "rows": len(rows),
        "unique_symbols": len(result),
        "reported_total": _parse_int(data.get("totalrecords", data.get("totalRecords"))) if isinstance(data, dict) else None,
        "pages": 1,
        "page_size": len(rows),
        "duplicate_conflicts": duplicates,
        "mode": "curl_full_download_http1_1",
    }


def _curl_file(url: str, destination: Path, max_time: int) -> None:
    command = [
        "curl", "--fail", "--location", "--silent", "--show-error", "--compressed",
        "--retry", "3", "--retry-delay", "2", "--connect-timeout", "20", "--max-time", str(max_time),
        "-H", f"User-Agent: {SEC_USER_AGENT}",
        "-H", "Accept-Encoding: gzip, deflate",
        "-o", str(destination), url,
    ]
    proc = subprocess.run(command, text=True, capture_output=True, timeout=max_time + 30)
    if proc.returncode != 0 or not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError(f"SEC bulk download failed rc={proc.returncode}: {proc.stderr[-500:]}")


def _latest_sec_shares(payload: dict[str, Any]) -> tuple[float | None, bool, str | None]:
    facts = payload.get("facts") or {}
    concept = ((facts.get("dei") or {}).get("EntityCommonStockSharesOutstanding") or {}) if isinstance(facts, dict) else {}
    units = concept.get("units") or {} if isinstance(concept, dict) else {}
    rows = units.get("shares") or [] if isinstance(units, dict) else []
    valid: list[tuple[str, str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = core.parse_number(row.get("val"))
        filed = str(row.get("filed") or "")
        end = str(row.get("end") or "")
        if value is None or value <= 0 or not filed or not end:
            continue
        valid.append((filed, end, value))
    if not valid:
        return None, False, None
    latest_filed = max(item[0] for item in valid)
    filed_rows = [item for item in valid if item[0] == latest_filed]
    latest_end = max(item[1] for item in filed_rows)
    values = sorted({round(item[2], 6) for item in filed_rows if item[1] == latest_end})
    if not values:
        return None, False, latest_filed
    if len(values) > 1:
        return None, True, latest_filed
    return float(values[0]), False, latest_filed


def _fetch_sec_reference(prior_errors: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not _TARGET_SYMBOLS:
        raise RuntimeError("SEC fallback has no target L0 symbols")
    with tempfile.TemporaryDirectory(prefix="qrgf-sec-") as td:
        root = Path(td)
        ticker_path = root / "company_tickers_exchange.json"
        facts_path = root / "companyfacts.zip"
        _curl_file(SEC_TICKERS_URL, ticker_path, 90)
        ticker_payload = json.loads(ticker_path.read_text(encoding="utf-8"))
        fields = ticker_payload.get("fields") or []
        data = ticker_payload.get("data") or []
        if not isinstance(fields, list) or not isinstance(data, list):
            raise RuntimeError("SEC ticker mapping has invalid shape")
        index = {str(name): i for i, name in enumerate(fields)}
        required = {"cik", "ticker"}
        if not required.issubset(index):
            raise RuntimeError("SEC ticker mapping is missing cik/ticker fields")
        target_by_cik: dict[int, set[str]] = {}
        mapped_targets: set[str] = set()
        wanted = {symbol.upper(): symbol.upper() for symbol in _TARGET_SYMBOLS}
        wanted.update({symbol.upper().replace(".", "-"): symbol.upper() for symbol in _TARGET_SYMBOLS if "." in symbol})
        for row in data:
            if not isinstance(row, list) or len(row) <= max(index.values()):
                continue
            sec_ticker = str(row[index["ticker"]] or "").strip().upper()
            target = wanted.get(sec_ticker)
            if not target:
                continue
            cik = _parse_int(row[index["cik"]])
            if cik is None:
                continue
            target_by_cik.setdefault(cik, set()).add(target)
            mapped_targets.add(target)

        _curl_file(SEC_COMPANYFACTS_ZIP, facts_path, 420)
        result: dict[str, dict[str, Any]] = {}
        conflicts: list[str] = []
        with_shares = 0
        with zipfile.ZipFile(facts_path) as archive:
            name_by_cik: dict[int, str] = {}
            for name in archive.namelist():
                base = Path(name).name
                if base.startswith("CIK") and base.endswith(".json"):
                    try:
                        cik = int(base[3:-5])
                    except ValueError:
                        continue
                    if cik in target_by_cik:
                        name_by_cik[cik] = name
            for cik, targets in target_by_cik.items():
                name = name_by_cik.get(cik)
                shares = None
                conflict = False
                filed = None
                if name:
                    try:
                        payload = json.loads(archive.read(name).decode("utf-8"))
                        shares, conflict, filed = _latest_sec_shares(payload)
                    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, OSError):
                        shares = None
                if shares is not None:
                    with_shares += len(targets)
                if conflict:
                    conflicts.extend(sorted(targets))
                for ticker in targets:
                    result[ticker] = {
                        "market_cap": None,
                        "sector": None,
                        "industry": None,
                        "ipo_year": None,
                        "shares_outstanding": shares,
                        "shares_outstanding_conflict": conflict,
                        "shares_filed": filed,
                        "reference_source": "sec_companyfacts",
                    }
        return result, {
            "provider": "sec_companyfacts",
            "mode": "official_sec_bulk_fallback",
            "target_symbols": len(_TARGET_SYMBOLS),
            "ticker_mapped": len(mapped_targets),
            "reference_symbols": len(result),
            "shares_available": with_shares,
            "shares_conflicts": sorted(set(conflicts)),
            "companyfacts_zip_bytes": facts_path.stat().st_size,
            "prior_nasdaq_errors": prior_errors,
        }


def fetch_reference() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Use Nasdaq when reachable; fail over only to official SEC bulk data."""
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
                    "provider": "nasdaq_screener",
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
    return _fetch_sec_reference(errors)


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
    ref = reference or {}
    security_type = str(row.get("security_type") or "").strip().lower()
    if security_type == "adr":
        result["market_cap_applicability"] = "optional"
    elif security_type == "etf":
        result["market_cap_applicability"] = "not_applicable"
    else:
        result["market_cap_applicability"] = "required"
        if result.get("market_cap") is None and ref.get("reference_source") == "sec_companyfacts":
            shares = core.parse_number(ref.get("shares_outstanding"))
            price = core.parse_number(result.get("price"))
            if shares is not None and price is not None and not ref.get("shares_outstanding_conflict"):
                result["market_cap"] = shares * price
                result["market_cap_source"] = "sec_companyfacts_shares_x_alpaca_price"
    if result.get("market_cap") is not None and ref.get("reference_source") == "nasdaq_screener":
        result["market_cap_source"] = "nasdaq_screener"

    sessions = int(result.get("trading_history_days") or 0)
    if 0 < sessions < 253:
        ipo_year = _parse_int(ref.get("ipo_year"))
        current_year = dt.datetime.now(dt.timezone.utc).year
        if ipo_year is not None and ipo_year <= current_year - 2:
            result["momentum_history_status"] = "source_gap"
            result["history_data_status"] = "source_gap_mature_listing"
        elif ipo_year is None:
            # The provider was queried five years back, but absent listing-date
            # evidence means we still do not pretend the company is young.
            result["momentum_history_status"] = "unknown"
            result["history_data_status"] = "short_history_without_listing_evidence"
    return result


def _load_targets_from_args() -> None:
    global _TARGET_SYMBOLS
    try:
        index = sys.argv.index("--l0-pages-dir")
        pages_dir = Path(sys.argv[index + 1])
    except (ValueError, IndexError):
        return
    targets: set[str] = set()
    for page in sorted(pages_dir.glob("page-*.csv")):
        with page.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ticker = str(row.get("ticker") or "").strip().upper()
                if ticker:
                    targets.add(ticker)
    _TARGET_SYMBOLS = targets


def _postprocess_manifest() -> None:
    try:
        index = sys.argv.index("--output-dir")
        output_dir = Path(sys.argv[index + 1])
    except (ValueError, IndexError):
        return
    path = output_dir / "manifest.json"
    if not path.exists():
        return
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["source_id"] = "alpaca_sip_daily_plus_free_reference"
    if "nasdaq_reference" in manifest:
        manifest["reference_provider"] = manifest["nasdaq_reference"]
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


core.fetch_nasdaq_reference = fetch_reference
core.fetch_alpaca_history = fetch_alpaca_history
core.metrics_for = metrics_for


if __name__ == "__main__":
    _load_targets_from_args()
    rc = core.main()
    if rc == 0:
        _postprocess_manifest()
    raise SystemExit(rc)
