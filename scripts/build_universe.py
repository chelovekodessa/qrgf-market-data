#!/usr/bin/env python3
"""Build a verified L0 universe from the Nasdaq Trader symbol directory.

The classifier admits only verified ordinary common equity, eligible American
depositary receipts/shares, and explicitly approved ordinary ETFs. Ambiguous
instrument types are quarantined and never become rankable L1 records.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from qrgf_common import atomic_write_json, normalize_ticker, strict_bool  # noqa: E402

NORMAL_FINANCIAL_STATUS = {"", "N"}

ADR_PATTERN = re.compile(
    r"(?:american\s+deposit(?:a|o)ry\s+(?:shares?|receipts?)|"
    r"american\s+depository\s+(?:shares?|receipts?)|"
    r"\bADS\b|\bADR\b|depositary\s+(?:shares?|receipts?))",
    re.I,
)

COMMON_EQUITY_PATTERN = re.compile(
    r"(?:common\s+stock|common\s+shares?|ordinary\s+shares?|"
    r"class\s+[a-z0-9-]+\s+(?:common\s+)?(?:stock|shares?)|"
    r"capital\s+stock|shares?\s+of\s+beneficial\s+interest)",
    re.I,
)

PREFERRED_PATTERN = re.compile(r"\bpreferred\b|\bpreference\s+shares?\b", re.I)
WARRANT_PATTERN = re.compile(r"\bwarrants?\b", re.I)
RIGHT_SECURITY_PATTERN = re.compile(
    r"(?:^|\s[-–—]\s)(?:subscription\s+)?rights?\b|\brights?\s*$",
    re.I,
)
UNIT_SECURITY_PATTERN = re.compile(r"(?:^|\s[-–—]\s)units?\b|\bunits?\s*$", re.I)
ADR_REPRESENTING_UNIT_PATTERN = re.compile(
    r"\b(?:each\s+)?(?:representing|represents?|repstg)\s+"
    r"(?:the\s+right\s+to\s+receive\s+)?"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+(?:\.\d+)?)\s+units?\b",
    re.I,
)
DEBT_PATTERN = re.compile(
    r"\b(?:exchange[- ]traded\s+notes?|ETNs?|senior\s+notes?|subordinated\s+notes?|"
    r"convertible\s+notes?|debentures?|bonds?)\b",
    re.I,
)
UNSUITABLE_FUND_PATTERN = re.compile(
    r"\b(?:closed[- ]end\s+fund|term\s+fund|income\s+fund|interval\s+fund|"
    r"management\s+investment\s+company|CEF)\b",
    re.I,
)
LIMITED_PARTNERSHIP_PATTERN = re.compile(r"\blimited\s+partnership\b|\bmaster\s+limited\s+partnership\b", re.I)
SPAC_PATTERN = re.compile(
    r"\b(?:blank\s+check|special\s+purpose\s+acquisition|acquisition)\b.*"
    r"\b(?:corp(?:oration)?|company|co\.?|holdings?|partners?|limited|ltd\.?|plc)\b",
    re.I,
)
SYMBOL_EXCLUDE_PATTERN = re.compile(r"(?:\$|\^|=|/WS$|/W$|\.W$|\.U$|\.R$)", re.I)

ETF_PROHIBITED_NAME_PATTERN = re.compile(
    r"(?:\b(?:leveraged|inverse|ultra(?:pro)?|bear)\b|"
    r"\b(?:-?1(?:\.\d+)?x|-?2x|-?3x)\b|"
    r"\bdaily\b.{0,40}\b(?:reset|long|short|bull|bear|1(?:\.\d+)?x|2x|3x)\b|"
    r"\bshort\b(?![- ]term)|\bETN\b|exchange[- ]traded\s+notes?)",
    re.I,
)


def normalize_symbol(value: Any) -> str:
    return normalize_ticker(value)


def _boolish(value: Any, default: bool = False) -> bool:
    try:
        parsed = strict_bool(value)
    except ValueError:
        return default
    return default if parsed is None else parsed


def load_approved_etfs(path: Path) -> dict[str, dict[str, Any]]:
    """Load one-row-per-ETF approval metadata.

    Backward-compatible sector maps with primary_etf/secondary_etf are accepted,
    but production approval files should include leverage_multiple, inverse,
    daily_reset, product_type, status and contract_id.
    """
    approved: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"Approved ETF CSV {path} has no header")
        normalized = {name.strip().lower(): name for name in reader.fieldnames}
        ticker_fields = [
            normalized[key]
            for key in ("ticker", "symbol", "primary_etf", "secondary_etf")
            if key in normalized
        ]
        if not ticker_fields:
            raise ValueError(f"Approved ETF CSV {path} needs ticker/symbol or primary_etf/secondary_etf")
        for row in reader:
            for ticker_field in ticker_fields:
                ticker = normalize_symbol(row.get(ticker_field))
                if not ticker:
                    continue
                metadata = {
                    "ticker": ticker,
                    "contract_id": str(row.get(normalized.get("contract_id", "")) or "").strip() or None,
                    "product_type": str(row.get(normalized.get("product_type", "")) or "ordinary_etf").strip().lower(),
                    "leverage_multiple": float(str(row.get(normalized.get("leverage_multiple", "")) or "1").strip() or 1),
                    "inverse": _boolish(row.get(normalized.get("inverse", "")), False),
                    "daily_reset": _boolish(row.get(normalized.get("daily_reset", "")), False),
                    "status": str(row.get(normalized.get("status", "")) or "approved").strip().lower(),
                    "full_name": str(row.get(normalized.get("full_name", "")) or "").strip() or None,
                    "sector": str(row.get(normalized.get("sector", "")) or "").strip() or None,
                    "last_verified_at": str(row.get(normalized.get("last_verified_at", "")) or "").strip() or None,
                }
                approved[ticker] = metadata
    if not approved:
        raise ValueError(f"Approved ETF CSV {path} produced an empty allowlist")
    return approved


def load_seed_symbols(paths: Iterable[Path]) -> dict[str, list[str]]:
    membership: dict[str, list[str]] = {}
    for path in paths:
        label = path.stem
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            rows = payload.get("data", payload) if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise ValueError(f"Seed JSON {path} must contain a list")
            symbols = [
                row.get("Symbol", row.get("symbol", row.get("ticker"))) if isinstance(row, dict) else row
                for row in rows
            ]
        else:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    raise ValueError(f"Seed CSV {path} has no header")
                field = next((name for name in reader.fieldnames if name.lower() in {"symbol", "ticker"}), None)
                if field is None:
                    raise ValueError(f"Seed CSV {path} needs Symbol or ticker column")
                symbols = [row.get(field) for row in reader]
        for raw in symbols:
            symbol = normalize_symbol(raw)
            if symbol:
                membership.setdefault(symbol, []).append(label)
    return membership


def _approved_etf_meta(approved_etfs: set[str] | Mapping[str, Mapping[str, Any]] | None, symbol: str) -> Mapping[str, Any] | None:
    if approved_etfs is None:
        return None
    if isinstance(approved_etfs, set):
        # Bare ticker sets do not carry contract identity, leverage metadata or
        # verification date and are therefore not trusted in production.
        return None
    return approved_etfs.get(symbol)


def _etf_metadata_is_safe(meta: Mapping[str, Any]) -> bool:
    status = str(meta.get("status") or "approved").strip().lower()
    product_type = str(meta.get("product_type") or "ordinary_etf").strip().lower()
    contract_id = str(meta.get("contract_id") or "").strip()
    verified_raw = str(meta.get("last_verified_at") or "").strip()
    try:
        leverage = float(meta.get("leverage_multiple", 1))
        verified = dt.date.fromisoformat(verified_raw)
    except (TypeError, ValueError):
        return False
    inverse = _boolish(meta.get("inverse"), False)
    daily_reset = _boolish(meta.get("daily_reset"), False)
    verification_fresh = 0 <= (dt.date.today() - verified).days <= 366
    return (
        status in {"approved", "active"}
        and product_type in {"ordinary_etf", "broad_etf", "sector_etf"}
        and bool(contract_id)
        and verification_fresh
        and leverage == 1
        and not inverse
        and not daily_reset
    )


def classify_row(
    row: dict[str, str],
    include_etfs: bool,
    approved_etfs: set[str] | Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[bool, str, str, bool]:
    """Classify one symbol-directory row.

    Return ``(eligible, reason, security_type, adr_flag)``. Ambiguous records are
    returned as ineligible with ``instrument_resolution_required`` so they are
    quarantined outside the structural universe.
    """
    symbol = normalize_symbol(row.get("Symbol"))
    name = str(row.get("Security Name") or "").strip()
    traded = str(row.get("Nasdaq Traded") or "").strip().upper()
    test_issue = str(row.get("Test Issue") or "").strip().upper()
    financial_status = str(row.get("Financial Status") or "").strip().upper()
    is_etf = str(row.get("ETF") or "").strip().upper() == "Y"

    if not symbol or symbol.lower().startswith("file creation time"):
        return False, "metadata_or_blank", "unknown", False
    if traded != "Y":
        return False, "not_nasdaq_traded", "unknown", False
    if test_issue != "N":
        return False, "test_issue", "unknown", False
    if financial_status not in NORMAL_FINANCIAL_STATUS:
        return False, f"abnormal_financial_status:{financial_status}", "unknown", False
    if SYMBOL_EXCLUDE_PATTERN.search(symbol):
        return False, "non_common_symbol_pattern", "non_common_instrument", False

    if is_etf:
        if not include_etfs:
            return False, "etf_excluded_from_stock_universe", "etf", False
        meta = _approved_etf_meta(approved_etfs, symbol)
        if meta is None:
            return False, "etf_not_in_approved_allowlist", "etf", False
        if not _etf_metadata_is_safe(meta) or ETF_PROHIBITED_NAME_PATTERN.search(name):
            return False, "leveraged_or_inverse_or_daily_reset_etf", "etf", False
        if DEBT_PATTERN.search(name):
            return False, "exchange_traded_note_not_etf", "etn", False
        return True, "", "etf", False

    # ADR/ADS descriptions often contain words such as "rights" or "units" in
    # the ratio explanation. Detect the depositary instrument before applying
    # security-level right/unit exclusions.
    is_adr = bool(ADR_PATTERN.search(name))
    if is_adr:
        if PREFERRED_PATTERN.search(name):
            return False, "preferred_depositary_security", "preferred", True
        explicit_unit_security = bool(UNIT_SECURITY_PATTERN.search(name)) and not bool(
            ADR_REPRESENTING_UNIT_PATTERN.search(name)
        )
        if WARRANT_PATTERN.search(name) or RIGHT_SECURITY_PATTERN.search(name) or explicit_unit_security:
            return False, "adr_warrant_right_or_unit", "non_common_instrument", True
        if DEBT_PATTERN.search(name):
            return False, "depositary_debt_security", "debt", True
        return True, "", "adr", True

    if SPAC_PATTERN.search(name):
        return False, "acquisition_vehicle", "spac", False
    if LIMITED_PARTNERSHIP_PATTERN.search(name):
        return False, "limited_partnership_not_common_equity", "limited_partnership", False
    if UNSUITABLE_FUND_PATTERN.search(name):
        return False, "unsuitable_non_etf_fund", "fund", False
    if PREFERRED_PATTERN.search(name):
        return False, "preferred", "preferred", False
    if WARRANT_PATTERN.search(name):
        return False, "warrant", "warrant", False
    if RIGHT_SECURITY_PATTERN.search(name):
        return False, "right", "right", False
    if UNIT_SECURITY_PATTERN.search(name):
        return False, "unit", "unit", False
    if DEBT_PATTERN.search(name):
        return False, "debt_note_or_bond", "debt", False
    if COMMON_EQUITY_PATTERN.search(name):
        return True, "", "common_equity", False

    return False, "instrument_resolution_required", "ambiguous", False


def build_universe(
    listing_path: Path,
    seed_membership: dict[str, list[str]],
    include_etfs: bool,
    approved_etfs: set[str] | Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    raw_rows = 0
    with listing_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="|")
        if not reader.fieldnames or "Symbol" not in reader.fieldnames:
            raise ValueError("Listing file does not look like nasdaqtraded.txt")
        for row in reader:
            raw_rows += 1
            symbol = normalize_symbol(row.get("Symbol"))
            keep, reason, security_type, is_adr = classify_row(row, include_etfs, approved_etfs)
            exchange = str(row.get("Listing Exchange") or "").strip()
            etf_meta = _approved_etf_meta(approved_etfs, symbol) if security_type == "etf" else None
            normalized = {
                "ticker": symbol,
                "company_name": str(row.get("Security Name") or "").strip(),
                "listing_exchange": exchange,
                "market_category": str(row.get("Market Category") or "").strip(),
                "financial_status": str(row.get("Financial Status") or "").strip(),
                "security_type": security_type,
                "instrument_status": "eligible" if keep else ("resolution_required" if reason == "instrument_resolution_required" else "ineligible"),
                "rankable": bool(keep),
                "adr_flag": "Y" if is_adr else "N",
                "etf_flag": str(row.get("ETF") or "").strip().upper(),
                "quality_seed": " | ".join(sorted(set(seed_membership.get(symbol, [])))),
                "contract_id": (str((etf_meta or {}).get("contract_id") or "").strip() if security_type == "etf" else f"nasdaqtrader:{exchange or 'US'}:{symbol}") if symbol else None,
                "contract_id_status": "approved_allowlist_identity" if security_type == "etf" else "provisional_symbol_directory_id",
                "approved_etf_product_type": (etf_meta or {}).get("product_type") if security_type == "etf" else None,
                "approved_etf_sector": (etf_meta or {}).get("sector") if security_type == "etf" else None,
                "approved_etf_last_verified_at": (etf_meta or {}).get("last_verified_at") if security_type == "etf" else None,
                "source_id": "nasdaq_trader_all_us_listed",
            }
            if keep:
                accepted.append(normalized)
            else:
                rejected.append({**normalized, "rejection_reason": reason})

    # Any duplicate symbol is a contract-resolution problem unless the records
    # are byte-equivalent after normalization. Never silently keep the first row.
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for record in accepted:
        by_symbol.setdefault(record["ticker"], []).append(record)
    unique: list[dict[str, Any]] = []
    duplicate_rejections = 0
    for symbol, records in by_symbol.items():
        signatures = {(r["company_name"], r["listing_exchange"], r["security_type"], r["contract_id"]) for r in records}
        if len(signatures) > 1:
            duplicate_rejections += len(records)
            for record in records:
                rejected.append({**record, "instrument_status": "resolution_required", "rankable": False, "rejection_reason": "duplicate_symbol_contract_resolution_required"})
            continue
        unique.append(records[0])

    accepted = sorted(unique, key=lambda row: (row["security_type"], row["ticker"]))
    quarantined = [row for row in rejected if row.get("instrument_status") == "resolution_required"]
    summary = {
        "raw_rows": raw_rows,
        "accepted_unique": len(accepted),
        "structurally_eligible": len(accepted),
        "rankable_l0": len(accepted),
        "rejected_rows": len(rejected) - len(quarantined),
        "quarantined_rows": len(quarantined),
        "ambiguous_review_count": 0,
        "duplicate_contract_rows": duplicate_rejections,
        "quality_seed_count": sum(bool(row["quality_seed"]) for row in accepted),
        "adr_count": sum(row["adr_flag"] == "Y" for row in accepted),
        "common_equity_count": sum(row["security_type"] == "common_equity" for row in accepted),
        "approved_etfs_accepted": sum(row["security_type"] == "etf" for row in accepted),
        "etfs_included": include_etfs,
        "approved_etf_allowlist_size": len(approved_etfs or {}),
        "complete": True,
    }
    return accepted, rejected, summary


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("ticker\n", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def download_listing(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 QRGF/3.0"})
    with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - explicit trusted URL supplied by operator
        data = response.read()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("listing", type=Path, nargs="?", help="Downloaded nasdaqtraded.txt")
    parser.add_argument("--download-url", help="Optional official symbol-directory URL")
    parser.add_argument("--download-to", type=Path, help="Destination when --download-url is used")
    parser.add_argument("--seed", type=Path, action="append", default=[], help="Optional JSON or CSV quality seed; repeatable")
    parser.add_argument("--include-etfs", action="store_true")
    parser.add_argument(
        "--approved-etfs",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "config" / "approved-etfs.csv",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rejected", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    listing = args.listing
    if args.download_url:
        listing = args.download_to or Path("nasdaqtraded.txt")
        download_listing(args.download_url, listing)
    if listing is None:
        parser.error("listing or --download-url is required")

    approved = load_approved_etfs(args.approved_etfs) if args.include_etfs else None
    accepted, rejected, summary = build_universe(
        listing,
        load_seed_symbols(args.seed),
        args.include_etfs,
        approved,
    )
    write_csv(args.output, accepted)
    if args.rejected:
        write_csv(args.rejected, rejected)
    if args.summary:
        atomic_write_json(args.summary, summary)
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
