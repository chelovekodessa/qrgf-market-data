#!/usr/bin/env python3
"""Normalize, enrich, coverage-check and globally rank the full L1 universe.

The script never performs top-N cuts inside source batches. It first merges all
sources deterministically, resolves source conflicts, classifies objective
history sufficiency, applies the Data Coverage Gate, and only then performs one
global ranking. Missing data remain missing; they are never converted to zero.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from qrgf_common import (
    atomic_write_json,
    clamp,
    is_missing,
    normalize_ticker,
    parse_datetime,
    parse_percent,
    source_rank,
    strict_bool,
    strict_float,
    tolerant_bool,
    tolerant_float,
)

ALIASES: dict[str, tuple[str, ...]] = {
    "ticker": ("ticker", "symbol", "code"),
    "company": ("company", "company_name", "name", "security_name"),
    "contract_id": ("contract_id", "conid", "instrument_id"),
    "security_type": ("security_type", "instrument_type", "asset_type"),
    "instrument_status": ("instrument_status", "eligibility_status"),
    "exchange": ("exchange", "listing_exchange", "market"),
    "sector": ("sector", "gics_sector"),
    "price": ("price", "current_price", "last", "last_price", "close"),
    "market_cap": ("market_cap", "marketcap", "market_capitalization"),
    "avg_dollar_volume": ("avg_dollar_volume", "average_dollar_volume", "avg_90d_usd_volume", "average_volume_usd", "dollar_volume"),
    "return_1m": ("return_1m", "perf_1m", "performance_1m", "change_1m", "return_1m_pct"),
    "return_3m": ("return_3m", "perf_3m", "performance_3m", "change_3m", "return_3m_pct"),
    "return_6m": ("return_6m", "perf_6m", "performance_6m", "change_6m", "return_6m_pct"),
    "return_12m": ("return_12m", "perf_1y", "performance_1y", "change_1y", "return_1y", "return_12m_pct"),
    "drawdown_52w": ("drawdown_52w", "drawdown_from_52w_high", "below_52w_high", "from_52w_high", "drawdown_pct"),
    "historical_volatility": ("historical_volatility", "historical_vol", "volatility", "volatility_30d", "historical_volatility_pct"),
    "profitable": ("profitable", "net_income_positive", "profit_positive"),
    "fcf_positive": ("fcf_positive", "free_cash_flow_positive", "positive_fcf"),
    "quality_seed": ("quality_seed", "index_seed", "index_membership"),
    "quality_prior_score": ("quality_prior_score", "quality_prior", "preliminary_quality_score"),
    "momentum_history_status": ("momentum_history_status", "l1_history_status", "history_sufficiency", "history_status", "trading_history_status"),
    "trading_history_days": ("trading_history_days", "trading_history_sessions", "valid_trading_sessions", "history_sessions", "history_days"),
    "listing_date": ("listing_date", "ipo_date", "first_trade_date"),
    "as_of": ("as_of", "data_as_of", "price_as_of"),
    "retrieved_at": ("retrieved_at", "fetched_at"),
    "percent_unit": ("percent_unit", "return_unit", "performance_unit"),
    "source_priority": ("source_priority", "priority"),
}

NON_HISTORY_CORE_FIELDS = ("price", "market_cap", "avg_dollar_volume", "drawdown_52w", "historical_volatility")
COVERAGE_FIELDS = NON_HISTORY_CORE_FIELDS + ("return_3m", "return_6m", "return_12m")
HISTORY_VALUES = {"full", "limited_but_usable", "insufficient", "unknown"}
ELIGIBLE_SECURITY_TYPES = {"common_equity", "adr", "etf", ""}
CRITICAL_NUMERIC_TOLERANCE_PCT = 0.5
NUMERIC_CONFLICT_FIELDS = set(COVERAGE_FIELDS) | {"price", "market_cap", "avg_dollar_volume", "quality_prior_score", "trading_history_days"}
STRUCTURAL_CONFLICT_FIELDS = {"contract_id", "security_type", "instrument_status", "exchange"}
PROHIBITED_SECURITY_TYPES = {"ambiguous", "fund", "etn", "debt", "spac", "warrant", "right", "unit", "preferred", "limited_partnership"}
HARD_NAME_PATTERNS = re.compile(r"\b(?:warrants?|subscription rights?|preferred shares?|exchange[- ]traded notes?|closed[- ]end fund|special purpose acquisition|blank check)\b", re.I)


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def parse_number(value: Any) -> float | None:
    return tolerant_float(value)


def parse_bool(value: Any) -> bool | None:
    return tolerant_bool(value)


def parse_quality_seed(value: Any) -> bool:
    if is_missing(value):
        return False
    parsed = tolerant_bool(value)
    if parsed is not None:
        return parsed
    text = str(value).strip().lower()
    if text in {"none", "null", "nan", "n/a", "na", "false", "0"}:
        return False
    return bool(text)


def normalize_momentum_history_status(raw: Any, history_days: float | None, r3: float | None, r6: float | None, r12: float | None, listing_date: Any = None, as_of: Any = None) -> str:
    _ = raw
    if history_days is not None:
        sessions = int(history_days)
        if sessions >= 252:
            return "full"
        if sessions >= 126:
            return "limited_but_usable" if r3 is not None and r6 is not None else "insufficient"
        return "insufficient"
    if r12 is not None:
        return "full"
    try:
        listed = parse_datetime(listing_date)
        observed = parse_datetime(as_of)
    except ValueError:
        return "unknown"
    if listed is not None and observed is not None and observed >= listed:
        calendar_age_days = (observed - listed).days
        if calendar_age_days >= 365:
            return "full"
        if calendar_age_days >= 180:
            return "limited_but_usable" if r3 is not None and r6 is not None else "insufficient"
        return "insufficient"
    return "unknown"


def history_evidence(history_days: float | None, listing_date: Any, as_of: Any, status: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {"valid_trading_sessions": int(history_days) if history_days is not None else None, "listing_date": None if is_missing(listing_date) else str(listing_date), "as_of": None if is_missing(as_of) else str(as_of), "status": status}
    if history_days is not None:
        evidence["reason_code"] = "valid_trading_sessions"
    else:
        try:
            listed = parse_datetime(listing_date)
            observed = parse_datetime(as_of)
        except ValueError:
            listed = observed = None
        if listed is not None and observed is not None and observed >= listed:
            evidence["calendar_age_days"] = (observed - listed).days
            evidence["reason_code"] = "listing_age_evidence"
        elif status == "full":
            evidence["reason_code"] = "twelve_month_return_available"
        else:
            evidence["reason_code"] = "objective_history_evidence_missing"
    return evidence


def load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            for key in ("rows", "data", "results", "stocks", "items"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            raise ValueError(f"{path}: JSON must contain a list of rows")
        return [dict(row) for row in payload if isinstance(row, dict)]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path}: CSV has no header")
        return [dict(row) for row in reader]


def resolve_columns(rows: list[dict[str, Any]]) -> dict[str, str]:
    if not rows:
        return {}
    all_names: set[str] = set()
    for row in rows[:50]:
        all_names.update(str(name) for name in row.keys())
    normalized = {normalize_header(name): name for name in all_names}
    result: dict[str, str] = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            key = normalize_header(alias)
            if key in normalized:
                result[canonical] = normalized[key]
                break
    return result


def load_source_manifest(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = payload.get("sources", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Source manifest must contain a sources list")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_id = str(row.get("id") or row.get("source") or row.get("filename") or "").strip()
        if not source_id:
            continue
        result[source_id] = dict(row)
        filename = str(row.get("filename") or "").strip()
        if filename:
            result[filename] = dict(row)
            result[Path(filename).stem] = dict(row)
    return result


def _source_config(source: str, explicit: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(explicit or {})
    cfg.setdefault("id", source)
    cfg.setdefault("priority", 0)
    lower = source.lower()
    if "percent_unit" not in cfg:
        cfg["percent_unit"] = "ratio" if "ratio" in lower else "percent"
    return cfg


def _raw_value(row: dict[str, Any], columns: dict[str, str], field: str) -> Any:
    name = columns.get(field)
    return row.get(name) if name else None


def canonicalize(row: dict[str, Any], columns: dict[str, str], source: str, source_config: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    ticker = normalize_ticker(_raw_value(row, columns, "ticker"))
    company = str(_raw_value(row, columns, "company") or "").strip()
    if not ticker or len(ticker) > 16 or HARD_NAME_PATTERNS.search(company):
        return None
    cfg = _source_config(source, source_config)
    row_unit = str(_raw_value(row, columns, "percent_unit") or cfg.get("percent_unit") or "").strip().lower()
    if row_unit not in {"percent", "ratio"}:
        raise ValueError(f"{source}: explicit percent_unit must be percent or ratio")
    def pct(field: str) -> float | None:
        return parse_percent(_raw_value(row, columns, field), unit=row_unit, field=field)
    r3 = pct("return_3m"); r6 = pct("return_6m"); r12 = pct("return_12m")
    history_sessions = parse_number(_raw_value(row, columns, "trading_history_days"))
    security_type = str(_raw_value(row, columns, "security_type") or "").strip().lower()
    instrument_status = str(_raw_value(row, columns, "instrument_status") or "").strip().lower()
    listing_date = str(_raw_value(row, columns, "listing_date") or "").strip() or None
    as_of = _raw_value(row, columns, "as_of") or cfg.get("as_of")
    retrieved_at = _raw_value(row, columns, "retrieved_at") or cfg.get("retrieved_at")
    history_status = normalize_momentum_history_status(_raw_value(row, columns, "momentum_history_status"), history_sessions, r3, r6, r12, listing_date, as_of)
    result: dict[str, Any] = {
        "ticker": ticker, "company": company, "contract_id": str(_raw_value(row, columns, "contract_id") or "").strip() or None,
        "security_type": security_type, "instrument_status": instrument_status, "exchange": str(_raw_value(row, columns, "exchange") or "").strip(), "sector": str(_raw_value(row, columns, "sector") or "").strip(),
        "price": parse_number(_raw_value(row, columns, "price")), "market_cap": parse_number(_raw_value(row, columns, "market_cap")), "avg_dollar_volume": parse_number(_raw_value(row, columns, "avg_dollar_volume")),
        "return_1m": pct("return_1m"), "return_3m": r3, "return_6m": r6, "return_12m": r12, "drawdown_52w": pct("drawdown_52w"), "historical_volatility": pct("historical_volatility"),
        "profitable": parse_bool(_raw_value(row, columns, "profitable")), "fcf_positive": parse_bool(_raw_value(row, columns, "fcf_positive")), "quality_seed": parse_quality_seed(_raw_value(row, columns, "quality_seed")),
        "quality_prior_score": parse_number(_raw_value(row, columns, "quality_prior_score")), "trading_history_days": history_sessions, "listing_date": listing_date,
        "as_of": str(as_of) if not is_missing(as_of) else None, "retrieved_at": str(retrieved_at) if not is_missing(retrieved_at) else None, "momentum_history_status": history_status,
        "history_evidence": history_evidence(history_sessions, listing_date, as_of, history_status), "sources": [str(cfg.get("id") or source)], "source_conflicts": [],
    }
    if result["drawdown_52w"] is not None: result["drawdown_52w"] = abs(float(result["drawdown_52w"]))
    if result["quality_prior_score"] is not None: result["quality_prior_score"] = clamp(float(result["quality_prior_score"]))
    priority = parse_number(_raw_value(row, columns, "source_priority"))
    meta = {"id": str(cfg.get("id") or source), "priority": int(priority if priority is not None else cfg.get("priority", 0)), "as_of": str(as_of) if not is_missing(as_of) else None, "retrieved_at": str(retrieved_at) if not is_missing(retrieved_at) else None, "percent_unit": row_unit, "conflict_tolerance_pct": float(cfg.get("conflict_tolerance_pct", CRITICAL_NUMERIC_TOLERANCE_PCT))}
    result["_field_meta"] = {field: dict(meta) for field, value in result.items() if field not in {"sources", "source_conflicts", "_field_meta"} and value not in (None, "")}
    return result


def _values_equal(field: str, a: Any, b: Any, tolerance_pct: float = CRITICAL_NUMERIC_TOLERANCE_PCT) -> bool:
    if field in NUMERIC_CONFLICT_FIELDS:
        try: left = float(a); right = float(b)
        except (TypeError, ValueError): return a == b
        if not math.isfinite(left) or not math.isfinite(right): return False
        if math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12): return True
        scale = max(abs(left), abs(right), 1e-12)
        return 100.0 * abs(left - right) / scale <= max(0.0, float(tolerance_pct))
    return a == b


def _merge_item(base: dict[str, Any], item: dict[str, Any]) -> None:
    for source in item.get("sources", []):
        if source not in base.setdefault("sources", []): base["sources"].append(source)
    base.setdefault("source_conflicts", []); base.setdefault("_field_meta", {})
    for field, value in item.items():
        if field in {"sources", "source_conflicts", "_field_meta", "momentum_history_status"} or value in (None, ""): continue
        current = base.get(field); incoming_meta = item.get("_field_meta", {}).get(field, {"id": item.get("sources", [""])[0]}); current_meta = base.get("_field_meta", {}).get(field, {"id": ""})
        if current in (None, ""):
            base[field] = value; base["_field_meta"][field] = incoming_meta; continue
        tolerance = min(float(current_meta.get("conflict_tolerance_pct", CRITICAL_NUMERIC_TOLERANCE_PCT)), float(incoming_meta.get("conflict_tolerance_pct", CRITICAL_NUMERIC_TOLERANCE_PCT)))
        if _values_equal(field, current, value, tolerance):
            if source_rank(incoming_meta, str(incoming_meta.get("id") or "")) > source_rank(current_meta, str(current_meta.get("id") or "")):
                base[field] = value; base["_field_meta"][field] = incoming_meta
            continue
        conflict = {"field": field, "existing_value": current, "incoming_value": value, "existing_source": current_meta.get("id"), "incoming_source": incoming_meta.get("id")}
        if conflict not in base["source_conflicts"]: base["source_conflicts"].append(conflict)
        if source_rank(incoming_meta, str(incoming_meta.get("id") or "")) > source_rank(current_meta, str(current_meta.get("id") or "")):
            base[field] = value; base["_field_meta"][field] = incoming_meta
    base["quality_seed"] = bool(base.get("quality_seed")) or bool(item.get("quality_seed"))
    base["momentum_history_status"] = normalize_momentum_history_status(None, base.get("trading_history_days"), base.get("return_3m"), base.get("return_6m"), base.get("return_12m"), base.get("listing_date"), base.get("as_of"))
    base["history_evidence"] = history_evidence(base.get("trading_history_days"), base.get("listing_date"), base.get("as_of"), base["momentum_history_status"])


def merge_rows(inputs: list[Path], source_manifest: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = dict(source_manifest or {}); by_key: dict[str, dict[str, Any]] = {}; key_by_ticker: dict[str, str] = {}; key_by_contract: dict[str, str] = {}; source_meta: list[dict[str, Any]] = []
    for path in sorted(inputs, key=lambda p: (p.name.lower(), str(p))):
        rows = load_rows(path); columns = resolve_columns(rows)
        if "ticker" not in columns: raise ValueError(f"{path}: ticker/symbol column not found")
        cfg = manifest.get(path.name) or manifest.get(path.stem) or manifest.get(str(path)) or {"id": path.name}
        source_meta.append({"source": str(path), "rows": len(rows), "resolved_columns": columns, "config": cfg})
        for row in rows:
            item = canonicalize(row, columns, path.name, cfg)
            if item is None: continue
            ticker = str(item["ticker"]); contract_id = str(item.get("contract_id") or "").strip(); contract_key = key_by_contract.get(contract_id) if contract_id else None; ticker_key = key_by_ticker.get(ticker)
            if contract_key and ticker_key and contract_key != ticker_key:
                for conflict_key in (contract_key, ticker_key):
                    target = by_key[conflict_key]; target.setdefault("source_conflicts", []).append({"field": "contract_id", "existing_value": target.get("contract_id"), "incoming_value": contract_id, "existing_source": "merged_identity", "incoming_source": (item.get("sources") or [""])[0]})
                continue
            key = contract_key or ticker_key or contract_id or ticker
            if key not in by_key: by_key[key] = item
            else: _merge_item(by_key[key], item)
            key_by_ticker[ticker] = key
            merged_contract = str(by_key[key].get("contract_id") or "").strip()
            if merged_contract: key_by_contract[merged_contract] = key
    result = list(by_key.values())
    for row in result:
        row["sources"] = sorted(set(row.get("sources", []))); row["source_conflicts"] = sorted(row.get("source_conflicts", []), key=lambda x: (str(x.get("field")), str(x.get("existing_source")), str(x.get("incoming_source"))))
    return result, source_meta


def structurally_rankable(row: dict[str, Any]) -> bool:
    security_type = str(row.get("security_type") or "").lower(); instrument_status = str(row.get("instrument_status") or "").lower()
    if security_type in PROHIBITED_SECURITY_TYPES or (security_type and security_type not in ELIGIBLE_SECURITY_TYPES): return False
    if instrument_status in {"resolution_required", "ineligible", "prohibited"}: return False
    price = parse_number(row.get("price")); adv = parse_number(row.get("avg_dollar_volume")); market_cap = parse_number(row.get("market_cap"))
    if price is None or price < 3: return False
    if adv is None or adv < 2_000_000: return False
    if security_type != "etf" and (market_cap is None or market_cap < 250_000_000): return False
    return True


def expected_fields(row: dict[str, Any]) -> tuple[str, ...]:
    history = str(row.get("momentum_history_status") or "unknown")
    if history == "full": return COVERAGE_FIELDS
    if history == "limited_but_usable": return NON_HISTORY_CORE_FIELDS + ("return_3m", "return_6m")
    return NON_HISTORY_CORE_FIELDS + ("return_3m", "return_6m")


def unresolved_fields(row: dict[str, Any]) -> list[str]:
    missing: list[str] = []; security_type = str(row.get("security_type") or "").lower()
    if security_type in PROHIBITED_SECURITY_TYPES or str(row.get("instrument_status") or "").lower() == "resolution_required": missing.append("instrument_resolution")
    history = str(row.get("momentum_history_status") or "unknown")
    for field in expected_fields(row):
        if row.get(field) is None: missing.append(field)
    if history == "unknown": missing.append("objective_history_evidence")
    if history == "full" and row.get("return_12m") is None and "return_12m" not in missing: missing.append("return_12m")
    conflict_fields = {str(item.get("field")) for item in row.get("source_conflicts", [])}
    for field in set(expected_fields(row)).union(STRUCTURAL_CONFLICT_FIELDS):
        if field in conflict_fields: missing.append(f"source_conflict:{field}")
    return sorted(set(missing))


def coverage(rows: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, float], list[dict[str, Any]], list[dict[str, Any]]]:
    structural = [row for row in rows if structurally_rankable(row)]; unresolved: list[dict[str, Any]] = []; insufficient: list[dict[str, Any]] = []
    for row in structural:
        history = str(row.get("momentum_history_status") or "unknown")
        if history == "insufficient": insufficient.append(row); continue
        missing = unresolved_fields(row)
        if missing: unresolved.append({**row, "unresolved_fields": missing})
    raw: dict[str, float] = {}; effective: dict[str, float] = {}; denominator = len(structural)
    for field in COVERAGE_FIELDS:
        raw[field] = 100.0 * sum(row.get(field) is not None for row in structural) / denominator if denominator else 0.0
        expected_rows = [row for row in structural if field in expected_fields(row)]
        effective[field] = 100.0 * sum(row.get(field) is not None for row in expected_rows) / len(expected_rows) if expected_rows else 100.0
    return raw, effective, unresolved, insufficient


def return_points_3m(value: float) -> float:
    if value <= -60: return 0.0
    if value <= -35: return 4.0
    if value <= -15: return 8.0
    if value <= 5: return 12.0
    if value <= 20: return 15.0
    return 12.0


def return_points_6m(value: float) -> float:
    if value <= -70: return 0.0
    if value <= -40: return 3.0
    if value <= -15: return 7.0
    if value <= 10: return 11.0
    if value <= 35: return 14.0
    return 11.0


def return_points_12m(value: float) -> float:
    if value <= -80: return 0.0
    if value <= -45: return 2.0
    if value <= -10: return 6.0
    if value <= 20: return 10.0
    if value <= 80: return 14.0
    return 10.0


def prior_growth_points(row: dict[str, Any]) -> float:
    values = [row.get("return_3m"), row.get("return_6m")]; scores = []
    if values[0] is not None: scores.append(return_points_3m(float(values[0])))
    if values[1] is not None: scores.append(return_points_6m(float(values[1])))
    if row.get("return_12m") is not None: scores.append(return_points_12m(float(row["return_12m"])))
    if not scores: return 0.0
    return min(20.0, max(scores) + 0.25 * (sum(scores) - max(scores)))


def pullback_points(drawdown: float) -> float:
    dd = abs(drawdown)
    if dd < 5: return 2.0
    if dd < 8: return 8.0
    if dd <= 20: return 20.0 + (dd - 8) / 12 * 3.0
    if dd <= 35: return 23.0 + (dd - 20) / 15 * 2.0
    if dd <= 50: return 25.0 - (dd - 35) / 15 * 1.0
    if dd <= 65: return 18.0 - (dd - 50) / 15 * 8.0
    return max(0.0, 10.0 - (dd - 65) / 2.0)


def quality_points(row: dict[str, Any]) -> float:
    total = 0.0
    if row.get("profitable") is True: total += 9.0
    if row.get("fcf_positive") is True: total += 9.0
    if row.get("quality_seed") is True: total += 7.0
    prior = row.get("quality_prior_score")
    if prior is not None: total += 5.0 * clamp(float(prior)) / 100.0
    return min(30.0, total)


def liquidity_points(row: dict[str, Any]) -> float:
    adv = float(row.get("avg_dollar_volume") or 0)
    if adv >= 1_000_000_000: return 15.0
    if adv >= 250_000_000: return 14.0
    if adv >= 50_000_000: return 12.0
    if adv >= 10_000_000: return 9.0
    if adv >= 2_000_000: return 5.0
    return 0.0


def market_cap_points(row: dict[str, Any]) -> float:
    if str(row.get("security_type") or "") == "etf": return 5.0
    cap = float(row.get("market_cap") or 0)
    if cap >= 50_000_000_000: return 5.0
    if cap >= 10_000_000_000: return 4.5
    if cap >= 2_000_000_000: return 3.5
    if cap >= 250_000_000: return 2.0
    return 0.0


def data_confidence_points(row: dict[str, Any]) -> float:
    expected = expected_fields(row); present = sum(row.get(field) is not None for field in expected); base = 10.0 * present / len(expected) if expected else 0.0
    if row.get("momentum_history_status") == "limited_but_usable": base -= 1.5
    if row.get("source_conflicts"): base -= min(5.0, len(row["source_conflicts"]) * 2.0)
    return max(0.0, base)


def score(row: dict[str, Any]) -> float:
    if not structurally_rankable(row) or unresolved_fields(row): return 0.0
    dd = float(row.get("drawdown_52w") or 0)
    result = quality_points(row) + pullback_points(dd) + prior_growth_points(row) + liquidity_points(row) + market_cap_points(row) + data_confidence_points(row)
    hv = float(row.get("historical_volatility") or 0)
    if hv > 250: result -= min(10.0, (hv - 250) / 20.0)
    if dd > 80: result -= min(10.0, dd - 80)
    return round(clamp(result), 4)


def ranking_key(score_value: float, row: dict[str, Any]) -> tuple[Any, ...]:
    ticker = str(row.get("ticker") or ""); ticker_inverse = tuple(-ord(char) for char in ticker)
    return (score_value, quality_points(row), data_confidence_points(row), liquidity_points(row), float(row.get("avg_dollar_volume") or 0), float(row.get("market_cap") or 0), ticker_inverse)


def qualifies_for_l1(row: dict[str, Any], score_value: float, minimum_score: float = 45.0) -> bool:
    if not structurally_rankable(row) or unresolved_fields(row): return False
    if row.get("momentum_history_status") == "insufficient": return False
    return score_value >= minimum_score


def qualified_l1(row: dict[str, Any], score_value: float, minimum_score: float = 45.0) -> bool:
    return qualifies_for_l1(row, score_value, minimum_score)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); fields: list[str] = []
    for row in rows:
        for key in row:
            if key.startswith("_"): continue
            if key not in fields: fields.append(key)
    if not fields: fields = ["ticker"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore"); writer.writeheader()
        for row in rows:
            rendered = {}
            for key in fields:
                value = row.get(key)
                if isinstance(value, (list, dict)): rendered[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
                elif value is None: rendered[key] = ""
                else: rendered[key] = value
            writer.writerow(rendered)


def render(row: dict[str, Any], score_value: float | None, status: str) -> dict[str, Any]:
    output = {key: value for key, value in row.items() if not key.startswith("_")}
    output.update({"l1_score": score_value, "l1_status": status, "history_sufficiency": row.get("momentum_history_status"), "opportunity_coverage_pct": 100.0 if not unresolved_fields(row) else 0.0, "rankable": status in {"ranked", "qualified_not_selected", "below_research_threshold"}, "selected_for_next_stage": status == "ranked"})
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path); parser.add_argument("--source-manifest", type=Path); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--pending-output", type=Path); parser.add_argument("--summary", type=Path)
    parser.add_argument("--minimum-input", type=int, default=3000); parser.add_argument("--keep", type=int, default=500); parser.add_argument("--keep-max", type=int, default=800); parser.add_argument("--minimum-score", type=float, default=45.0); parser.add_argument("--coverage-min", type=float, default=100.0); parser.add_argument("--unresolved-max-pct", type=float, default=0.0)
    args = parser.parse_args()
    if not math.isclose(args.coverage_min, 100.0) or not math.isclose(args.unresolved_max_pct, 0.0): parser.error("Production core coverage is fixed at 100 percent with zero unresolved mature rows")
    if args.keep < 0 or args.keep > args.keep_max or args.keep_max > 800: parser.error("L1 finalist ceiling must satisfy 0 <= keep <= keep_max <= 800")
    if args.minimum_input <= 0: parser.error("minimum-input must be positive")
    manifest = load_source_manifest(args.source_manifest); rows, sources = merge_rows(args.inputs, manifest); raw_cov, effective_cov, unresolved, insufficient = coverage(rows)
    structural = [row for row in rows if structurally_rankable(row)]
    unresolved_keys = {str(row.get("contract_id") or row.get("ticker")) for row in unresolved}; insufficient_keys = {str(row.get("contract_id") or row.get("ticker")) for row in insufficient}
    rankable = [row for row in structural if str(row.get("contract_id") or row.get("ticker")) not in unresolved_keys and str(row.get("contract_id") or row.get("ticker")) not in insufficient_keys]
    scored = [(score(row), row) for row in rankable]; qualified = [(value, row) for value, row in scored if qualifies_for_l1(row, value, args.minimum_score)]; ranked = sorted(qualified, key=lambda pair: ranking_key(pair[0], pair[1]), reverse=True); retained = ranked[: args.keep]
    selected_keys = {str(row.get("contract_id") or row.get("ticker")) for _, row in retained}
    def identity_key(row: dict[str, Any]) -> str: return str(row.get("contract_id") or row.get("ticker") or "").strip()
    selected_scores = {identity_key(row): value for value, row in retained}; qualified_scores = {identity_key(row): value for value, row in ranked}; all_scores = {identity_key(row): value for value, row in scored}; unresolved_keys = {identity_key(row) for row in unresolved}; insufficient_keys = {identity_key(row) for row in insufficient}; structural_keys = {identity_key(row) for row in structural}
    output_rows: list[dict[str, Any]] = []; pending_rows: list[dict[str, Any]] = []
    for row in rows:
        key = identity_key(row)
        if key in selected_scores:
            rendered = render(row, selected_scores[key], "ranked"); rendered["limited_history_recheck"] = row.get("momentum_history_status") == "limited_but_usable"; output_rows.append(rendered)
        elif key not in structural_keys: pending_rows.append(render(row, None, "structurally_ineligible"))
        elif key in unresolved_keys: pending_rows.append(render(row, None, "needs_enrichment"))
        elif key in insufficient_keys: pending_rows.append(render(row, None, "short_history_recheck"))
        elif key in qualified_scores: pending_rows.append(render(row, qualified_scores[key], "qualified_not_selected"))
        else: pending_rows.append(render(row, all_scores.get(key), "below_research_threshold"))
    output_rows.sort(key=lambda row: ranking_key(float(row["l1_score"]), row), reverse=True); pending_rows.sort(key=lambda row: (str(row.get("ticker") or ""), str(row.get("contract_id") or "")))
    write_csv(args.output, output_rows)
    if args.pending_output: write_csv(args.pending_output, pending_rows)
    production_floor_met = len(rows) >= 3000 and args.minimum_input >= 3000; requested_floor_met = len(rows) >= args.minimum_input; coverage_gate_passed = requested_floor_met and not unresolved; broad_search_complete = production_floor_met and coverage_gate_passed
    if broad_search_complete: blocked_reason = None
    elif not production_floor_met: blocked_reason = "partial_or_sub_3000_run"
    elif unresolved: blocked_reason = "unresolved_mature_core_data"
    elif not requested_floor_met: blocked_reason = "minimum_input_not_met"
    else: blocked_reason = "coverage_gate_failed"
    summary = {"input_rows": sum(item["rows"] for item in sources), "merged_rows": len(rows), "structurally_eligible_rows": len(structural), "rankable_rows": len(rankable), "actual_rankable_rows": len(rankable), "qualified_rows": len(qualified), "unresolved_core_rows": len(unresolved), "unresolved_mature_core_rows": sum(row.get("momentum_history_status") == "full" for row in unresolved), "history_insufficient_rows": len(insufficient), "raw_core_field_coverage_pct": raw_cov, "effective_core_field_coverage_pct": effective_cov, "coverage_gate_passed": coverage_gate_passed, "production_floor_met": production_floor_met, "partial_or_test_mode": not production_floor_met, "ranked_l1": len(output_rows), "retained_l1": len(output_rows), "pending_recheck_rows": sum(row.get("l1_status") in {"needs_enrichment", "short_history_recheck"} for row in pending_rows), "nonselected_rows": len(pending_rows), "assessed_rows": len(output_rows) + len(pending_rows), "minimum_score": args.minimum_score, "broad_search_complete": broad_search_complete, "blocked_reason": blocked_reason, "global_ranking_complete": coverage_gate_passed, "sources": sources}
    if args.summary: atomic_write_json(args.summary, summary)
    else: print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.minimum_input >= 3000 and not coverage_gate_passed: return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
