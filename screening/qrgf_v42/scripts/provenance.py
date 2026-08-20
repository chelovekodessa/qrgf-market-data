#!/usr/bin/env python3
"""V4.2 provenance contracts for market membership and MetricDuck discovery.

The module separates three trust classes:

* market membership is hash-bound to a pinned radar row and an identity map;
* MetricDuck cross-company results are connector-attested, not cryptographically
  signed by the external provider;
* MASTER selection is derived locally and must be recomputed by the publisher.

No function in this module treats a self hash as proof that an external system
actually returned the data.  The trust class remains explicit in every durable
artifact.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

from common import ensure, load_policy, number, semantic_hash
import selection

ARCHITECTURE_VERSION = "4.2.0"
IDENTITY_MAP_KIND = "qrgf_v42_official_identity_map"
MARKET_INDEX_KIND = "qrgf_v42_market_membership_index"
QUERY_RECEIPT_KIND = "qrgf_v42_metricduck_query_receipt"
QUERY_PLAN_KIND = "qrgf_v42_metricduck_query_plan"
CONNECTOR_TRUST_CLASS = "connector_attested"
MARKET_TRUST_CLASS = "pinned_market_row_plus_identity_map"
IDENTITY_SOURCE_KIND = "sec_company_tickers_plus_security_specific_etfs"

_ALLOWED_IDENTITY_STATUS = frozenset({"official", "security_specific", "unresolved"})
_ALLOWED_RESOLVED_STATUS = frozenset({"official", "security_specific"})
_FORBIDDEN_QUERY_NAMES = {
    "price", "current_price", "last", "close", "bid", "ask", "quote",
    "reference_52w_high", "distance_to_high_pct", "drawdown", "drawdown_pct",
    "drawdown_52w_pct", "return_5d_pct", "return_1m_pct", "return_3m_pct",
    "return_6m_pct", "return_12m_pct", "recovery_setup_score", "l2_setup_score",
    "research_priority_score", "rsi", "atr", "momentum", "historical_volatility_pct",
    "setup_prior_growth", "setup_pullback_geometry", "setup_liquidity",
}


def _require_hash(value: Any, label: str) -> str:
    text = str(value or "")
    ensure(len(text) == 64 and all(ch in "0123456789abcdef" for ch in text), f"{label} must be a lowercase SHA-256")
    return text


def _self_hash(value: Mapping[str, Any], field: str, label: str) -> dict[str, Any]:
    result = dict(value)
    body = {key: item for key, item in result.items() if key != field}
    ensure(result.get(field) == semantic_hash(body), f"{label} self hash mismatch")
    return result


def _normal_sector(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text or "Unclassified"


def _normal_security_type(value: Any) -> str:
    text = str(value or "common_equity").strip().lower()
    aliases = {"stock": "common_equity", "common stock": "common_equity", "common": "common_equity"}
    return aliases.get(text, text)


def _field_name_forbidden(value: Any) -> bool:
    name = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    if name in _FORBIDDEN_QUERY_NAMES:
        return True
    if name.startswith(("price_", "quote_", "rsi_", "atr_", "momentum_", "drawdown_", "recovery_", "setup_", "current_market_")):
        return True
    if name.startswith("return_") and not name.startswith("return_on_"):
        return True
    if name.endswith(("_price", "_quote", "_rsi", "_atr", "_momentum", "_drawdown", "_recovery", "_setup")):
        return True
    return name in {
        "open", "high", "low", "prev_close", "previous_close", "last_price", "last_trade_price", "last_sale_price",
        "change_pct", "percent_change", "day_change_pct", "macd", "stochastic", "relative_strength", "relative_volume",
        "moving_average", "sma", "ema", "vwap", "implied_volatility",
    }


def _forbidden_paths(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            dotted = f"{path}.{key}" if path else str(key)
            if _field_name_forbidden(key):
                found.append(dotted)
            found.extend(_forbidden_paths(child, dotted))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_paths(child, f"{path}[{index}]"))
    return found


def build_identity_map(*, source_kind: str, source_snapshot_sha256: str,
                       entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    seen_contracts: set[str] = set()
    seen_tickers: set[str] = set()
    for raw in entries:
        ticker = str(raw.get("ticker") or "").strip().upper()
        contract_id = str(raw.get("contract_id") or "").strip()
        security_type = _normal_security_type(raw.get("security_type"))
        issuer_id = str(raw.get("issuer_id") or "").strip()
        status = str(raw.get("resolution_status") or "").strip().lower()
        ensure(ticker, "identity map entry ticker missing")
        ensure(status in _ALLOWED_RESOLVED_STATUS, "identity map entry must be officially resolved or security-specific")
        if security_type == "etf":
            ensure(status == "security_specific" and issuer_id.startswith("SECURITY:"), "ETF identity must be security-specific")
        else:
            ensure(status == "official" and issuer_id.startswith("CIK:"), "operating-company identity must use an official CIK")
        if contract_id:
            ensure(contract_id not in seen_contracts, "identity map duplicate contract_id")
            seen_contracts.add(contract_id)
        ensure(ticker not in seen_tickers, "identity map duplicate ticker")
        seen_tickers.add(ticker)
        body = {
            "ticker": ticker,
            "contract_id": contract_id or None,
            "security_type": security_type,
            "issuer_id": issuer_id,
            "resolution_status": status,
            "share_class_group": str(raw.get("share_class_group") or issuer_id),
            "security_class": str(raw.get("security_class") or "") or None,
            "source_record_sha256": _require_hash(raw.get("source_record_sha256") or semantic_hash(dict(raw)), "identity source record hash"),
        }
        normalized.append({**body, "identity_entry_sha256": semantic_hash(body)})
    normalized.sort(key=lambda item: (str(item["ticker"]), str(item.get("contract_id") or "")))
    ensure(str(source_kind) == IDENTITY_SOURCE_KIND, "identity map source kind is not the approved pinned official source")
    body = {
        "schema_version": "1.0.0",
        "kind": IDENTITY_MAP_KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "source_kind": str(source_kind),
        "source_snapshot_sha256": _require_hash(source_snapshot_sha256, "identity map source snapshot hash"),
        "publisher_recompute_required": True,
        "entry_count": len(normalized),
        "entries": normalized,
    }
    value = {**body, "identity_map_sha256": semantic_hash(body)}
    validate_identity_map(value)
    return value


def validate_identity_map(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _self_hash(value, "identity_map_sha256", "identity map")
    ensure(result.get("schema_version") == "1.0.0" and result.get("kind") == IDENTITY_MAP_KIND, "invalid V4.2 identity map")
    ensure(result.get("architecture_version") == ARCHITECTURE_VERSION, "identity map architecture mismatch")
    ensure(result.get("source_kind") == IDENTITY_SOURCE_KIND and result.get("publisher_recompute_required") is True, "identity map source/recompute contract invalid")
    _require_hash(result.get("source_snapshot_sha256"), "identity map source snapshot hash")
    rows = result.get("entries")
    ensure(isinstance(rows, list) and int(result.get("entry_count", -1)) == len(rows), "identity map entry count mismatch")
    contracts: set[str] = set()
    tickers: set[str] = set()
    for raw in rows:
        ensure(isinstance(raw, Mapping), "identity map entry invalid")
        row = _self_hash(raw, "identity_entry_sha256", "identity map entry")
        ticker = str(row.get("ticker") or "").upper()
        contract = str(row.get("contract_id") or "")
        status = str(row.get("resolution_status") or "")
        security_type = _normal_security_type(row.get("security_type"))
        issuer_id = str(row.get("issuer_id") or "")
        ensure(ticker and ticker not in tickers, "identity map duplicate or missing ticker")
        tickers.add(ticker)
        if contract:
            ensure(contract not in contracts, "identity map duplicate contract")
            contracts.add(contract)
        ensure(status in _ALLOWED_RESOLVED_STATUS, "identity map unresolved entry is forbidden")
        if security_type == "etf":
            ensure(status == "security_specific" and issuer_id.startswith("SECURITY:"), "ETF identity map entry invalid")
        else:
            ensure(status == "official" and issuer_id.startswith("CIK:"), "operating-company identity map entry invalid")
        _require_hash(row.get("source_record_sha256"), "identity source record hash")
    return result


def _identity_lookup(identity_map: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    by_contract: dict[str, Mapping[str, Any]] = {}
    by_ticker: dict[str, Mapping[str, Any]] = {}
    for row in identity_map["entries"]:
        ticker = str(row["ticker"]).upper()
        by_ticker[ticker] = row
        contract = str(row.get("contract_id") or "")
        if contract:
            by_contract[contract] = row
    return by_contract, by_ticker


def build_market_index(rows: Iterable[Mapping[str, Any]], *, market_session_id: str,
                       source_snapshot_id: str, source_manifest_sha256: str,
                       identity_map_value: Mapping[str, Any]) -> dict[str, Any]:
    identity_map = validate_identity_map(identity_map_value)
    by_contract, by_ticker = _identity_lookup(identity_map)
    allowed_types = set(load_policy()["universe"]["allowed_security_types"])
    memberships: list[dict[str, Any]] = []
    seen_contracts: set[str] = set()
    eligible_universe_size = 0
    for raw in rows:
        ensure(isinstance(raw, Mapping), "market index source row invalid")
        ticker = str(raw.get("ticker") or "").strip().upper()
        contract_id = str(raw.get("contract_id") or "").strip()
        ensure(ticker and contract_id, "market index row requires ticker and contract_id")
        ensure(contract_id not in seen_contracts, "market index duplicate contract_id")
        seen_contracts.add(contract_id)
        security_type = _normal_security_type(raw.get("security_type"))
        instrument_status = str(raw.get("instrument_status") or "").strip().lower()
        if instrument_status == "eligible":
            eligible_universe_size += 1
        identity = by_contract.get(contract_id) or by_ticker.get(ticker)
        resolution_status = "unresolved"
        issuer_id: str | None = None
        share_class_group: str | None = None
        identity_entry_sha256: str | None = None
        if identity is not None and _normal_security_type(identity.get("security_type")) == security_type:
            resolution_status = str(identity.get("resolution_status") or "unresolved")
            issuer_id = str(identity.get("issuer_id") or "") or None
            share_class_group = str(identity.get("share_class_group") or issuer_id or "") or None
            identity_entry_sha256 = str(identity.get("identity_entry_sha256") or "") or None
        overlay = selection.security_overlay({"security_type": security_type})
        research_scope_key = f"{issuer_id}|{overlay}" if issuer_id and resolution_status in _ALLOWED_RESOLVED_STATUS else None
        master_eligible = bool(
            instrument_status == "eligible"
            and security_type in allowed_types
            and resolution_status in _ALLOWED_RESOLVED_STATUS
            and research_scope_key
        )
        body = {
            "ticker": ticker,
            "contract_id": contract_id,
            "company": str(raw.get("company") or raw.get("company_name") or "") or None,
            "security_type": security_type,
            "instrument_status": instrument_status,
            "exchange": str(raw.get("exchange") or "") or None,
            "sector": _normal_sector(raw.get("sector")),
            "industry": str(raw.get("industry") or "") or None,
            "market_cap": number(raw.get("market_cap")),
            "avg_dollar_volume": number(raw.get("avg_dollar_volume")),
            "source_row_sha256": semantic_hash(dict(raw)),
            "identity_resolution_status": resolution_status,
            "identity_entry_sha256": identity_entry_sha256,
            "issuer_id": issuer_id,
            "share_class_group": share_class_group,
            "security_overlay": overlay,
            "research_scope_key": research_scope_key,
            "master_eligible": master_eligible,
        }
        memberships.append({**body, "market_membership_sha256": semantic_hash(body)})
    memberships.sort(key=lambda item: (str(item["ticker"]), str(item["contract_id"])))
    eligible_sectors = sorted({_normal_sector(row.get("sector")) for row in memberships if row.get("master_eligible") is True})
    body = {
        "schema_version": "1.0.0",
        "kind": MARKET_INDEX_KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "market_session_id": str(market_session_id),
        "source_snapshot_id": str(source_snapshot_id),
        "source_manifest_sha256": _require_hash(source_manifest_sha256, "market source manifest hash"),
        "identity_map_sha256": identity_map["identity_map_sha256"],
        "trust_class": MARKET_TRUST_CLASS,
        "publisher_recompute_required": True,
        "row_count": len(memberships),
        "eligible_universe_size": eligible_universe_size,
        "master_eligible_count": sum(row["master_eligible"] is True for row in memberships),
        "eligible_sectors": eligible_sectors,
        "rows": memberships,
    }
    value = {**body, "market_index_sha256": semantic_hash(body)}
    validate_market_index(value)
    return value


def validate_market_index(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _self_hash(value, "market_index_sha256", "market membership index")
    ensure(result.get("schema_version") == "1.0.0" and result.get("kind") == MARKET_INDEX_KIND, "invalid V4.2 market membership index")
    ensure(result.get("architecture_version") == ARCHITECTURE_VERSION, "market index architecture mismatch")
    ensure(result.get("trust_class") == MARKET_TRUST_CLASS, "market index trust class mismatch")
    ensure(result.get("publisher_recompute_required") is True, "market index publisher recomputation is disabled")
    _require_hash(result.get("source_manifest_sha256"), "market source manifest hash")
    _require_hash(result.get("identity_map_sha256"), "market identity map hash")
    rows = result.get("rows")
    ensure(isinstance(rows, list) and int(result.get("row_count", -1)) == len(rows), "market index row count mismatch")
    contracts: set[str] = set()
    master_count = 0
    eligible_count = 0
    sectors: set[str] = set()
    for raw in rows:
        ensure(isinstance(raw, Mapping), "market membership row invalid")
        row = _self_hash(raw, "market_membership_sha256", "market membership row")
        contract = str(row.get("contract_id") or "")
        ticker = str(row.get("ticker") or "")
        ensure(ticker and contract and contract not in contracts, "market membership identity invalid")
        contracts.add(contract)
        _require_hash(row.get("source_row_sha256"), "market source row hash")
        status = str(row.get("identity_resolution_status") or "")
        ensure(status in _ALLOWED_IDENTITY_STATUS, "market membership identity status invalid")
        if str(row.get("instrument_status") or "") == "eligible":
            eligible_count += 1
        if row.get("master_eligible") is True:
            master_count += 1
            ensure(status in _ALLOWED_RESOLVED_STATUS, "master-eligible market row has unresolved issuer")
            ensure(str(row.get("issuer_id") or "") and str(row.get("research_scope_key") or ""), "master-eligible market row identity missing")
            _require_hash(row.get("identity_entry_sha256"), "market identity entry hash")
            sectors.add(_normal_sector(row.get("sector")))
        else:
            if status == "unresolved":
                ensure(row.get("research_scope_key") in (None, ""), "unresolved market row has research scope")
    ensure(int(result.get("eligible_universe_size") or -1) == eligible_count, "market index eligible universe count mismatch")
    ensure(int(result.get("master_eligible_count", -1)) == master_count, "market index master-eligible count mismatch")
    ensure(list(result.get("eligible_sectors") or []) == sorted(sectors), "market index eligible sector set mismatch")
    return result


def validate_market_index_against_identity_map(market_index_value: Mapping[str, Any],
                                               identity_map_value: Mapping[str, Any]) -> dict[str, Any]:
    """Verify that every resolved market membership was derived from the pinned identity map."""
    market_index = validate_market_index(market_index_value)
    identity_map = validate_identity_map(identity_map_value)
    ensure(market_index.get("identity_map_sha256") == identity_map["identity_map_sha256"], "market index identity map binding mismatch")
    by_contract, by_ticker = _identity_lookup(identity_map)
    for row in market_index["rows"]:
        ticker = str(row.get("ticker") or "").upper()
        contract = str(row.get("contract_id") or "")
        security_type = _normal_security_type(row.get("security_type"))
        identity = by_contract.get(contract) or by_ticker.get(ticker)
        status = str(row.get("identity_resolution_status") or "")
        if identity is None or _normal_security_type(identity.get("security_type")) != security_type:
            ensure(status == "unresolved" and row.get("master_eligible") is not True, "market index resolved an identity absent from the pinned identity map")
            continue
        expected_status = str(identity.get("resolution_status") or "")
        expected_issuer = str(identity.get("issuer_id") or "")
        expected_group = str(identity.get("share_class_group") or expected_issuer)
        expected_overlay = selection.security_overlay({"security_type": security_type})
        expected_scope = f"{expected_issuer}|{expected_overlay}"
        ensure(status == expected_status, "market index identity status differs from the pinned identity map")
        ensure(row.get("identity_entry_sha256") == identity.get("identity_entry_sha256"), "market index identity entry hash mismatch")
        ensure(str(row.get("issuer_id") or "") == expected_issuer, "market index issuer differs from the pinned identity map")
        ensure(str(row.get("share_class_group") or "") == expected_group, "market index share-class group mismatch")
        ensure(str(row.get("security_overlay") or "") == expected_overlay, "market index security overlay mismatch")
        ensure(str(row.get("research_scope_key") or "") == expected_scope, "market index research scope mismatch")
    return market_index


def normalize_query_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    lane = str(value.get("lane") or "").strip()
    sector = _normal_sector(value.get("sector"))
    low = number(value.get("market_cap_min"))
    high = number(value.get("market_cap_max"))
    limit = int(value.get("limit") or 0)
    filters = value.get("filters") if isinstance(value.get("filters"), Mapping) else {}
    sort = str(value.get("sort") or "").strip()
    ensure(lane in load_policy()["bootstrap"]["lane_score_weights"], "MetricDuck query lane invalid")
    ensure(low is not None and low >= 0, "MetricDuck query lower market-cap bound invalid")
    ensure(high is None or high > low, "MetricDuck query upper market-cap bound invalid")
    maximum = int(load_policy()["bootstrap"]["metricduck_query_plan"]["connector_max_rows_per_query"])
    ensure(1 <= limit <= maximum, "MetricDuck query limit exceeds connector contract")
    approved = load_policy()["bootstrap"]["metricduck_query_plan"]["approved_lane_filters"].get(lane)
    ensure(isinstance(approved, Mapping) and dict(filters) == dict(approved), "MetricDuck query filters differ from the approved lane profile")
    ensure(not _forbidden_paths(filters), "MetricDuck query filters contain current-market/recovery fields")
    ensure(sort and not _field_name_forbidden(sort), "MetricDuck query sort is missing or market-price based")
    body = {
        "lane": lane,
        "sector": sector,
        "market_cap_min": float(low),
        "market_cap_max": float(high) if high is not None else None,
        "filters": dict(filters),
        "sort": sort,
        "limit": limit,
    }
    return {**body, "query_sha256": semantic_hash(body)}


def _passes_filters(facts: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    for field, rule_raw in filters.items():
        if not isinstance(rule_raw, Mapping):
            return False
        operator = str(rule_raw.get("operator") or "")
        target = rule_raw.get("value")
        actual = facts.get(field)
        if operator in {"gte", "lte"}:
            left, right = number(actual), number(target)
            if left is None or right is None:
                return False
            if operator == "gte" and left < right:
                return False
            if operator == "lte" and left > right:
                return False
        elif operator == "eq":
            if actual != target:
                return False
        else:
            return False
    return True


def build_query_receipt(query_spec_value: Mapping[str, Any], *, result_rows: Iterable[Mapping[str, Any]],
                        matched_count: int, retrieved_at: str,
                        response_handle: str | None = None) -> dict[str, Any]:
    spec = normalize_query_spec(query_spec_value)
    rows: list[dict[str, Any]] = []
    for raw in result_rows:
        ensure(isinstance(raw, Mapping), "MetricDuck result row invalid")
        bad = _forbidden_paths(raw)
        ensure(not bad, f"MetricDuck result contains current-market/recovery fields: {sorted(bad)}")
        ticker = str(raw.get("ticker") or "").strip().upper()
        ensure(ticker, "MetricDuck result ticker missing")
        facts = raw.get("facts") if isinstance(raw.get("facts"), Mapping) else {}
        ensure(_passes_filters(facts, spec["filters"]), "MetricDuck result row does not satisfy its approved lane query filters")
        body = {
            "ticker": ticker,
            "contract_id": str(raw.get("contract_id") or "") or None,
            "company": str(raw.get("company") or raw.get("company_name") or "") or None,
            "sector": _normal_sector(raw.get("sector") or spec["sector"]),
            "facts": dict(facts),
            "periods": dict(raw.get("periods") or {}) if isinstance(raw.get("periods"), Mapping) else {},
        }
        rows.append({**body, "result_row_sha256": semantic_hash(body)})
    rows.sort(key=lambda item: (str(item["ticker"]), str(item.get("contract_id") or ""), str(item["result_row_sha256"])))
    matched = int(matched_count)
    returned = len(rows)
    complete = matched == returned and returned <= int(spec["limit"])
    body = {
        "schema_version": "1.0.0",
        "kind": QUERY_RECEIPT_KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "connector_name": "MetricDuck",
        "connector_tool": "screen_companies",
        "trust_class": CONNECTOR_TRUST_CLASS,
        "external_cryptographic_signature_available": False,
        "query_spec": spec,
        "query_sha256": spec["query_sha256"],
        "matched_count": matched,
        "returned_count": returned,
        "complete": complete,
        "requires_split": not complete,
        "retrieved_at": str(retrieved_at),
        "response_handle": str(response_handle or "") or None,
        "rows": rows,
    }
    value = {**body, "receipt_sha256": semantic_hash(body)}
    validate_query_receipt(value)
    return value


def validate_query_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _self_hash(value, "receipt_sha256", "MetricDuck query receipt")
    ensure(result.get("schema_version") == "1.0.0" and result.get("kind") == QUERY_RECEIPT_KIND, "invalid V4.2 MetricDuck query receipt")
    ensure(result.get("architecture_version") == ARCHITECTURE_VERSION, "MetricDuck receipt architecture mismatch")
    ensure(result.get("connector_name") == "MetricDuck" and result.get("connector_tool") == "screen_companies", "MetricDuck receipt connector mismatch")
    ensure(result.get("trust_class") == CONNECTOR_TRUST_CLASS and result.get("external_cryptographic_signature_available") is False, "MetricDuck receipt trust boundary mismatch")
    ensure(str(result.get("retrieved_at") or "") and str(result.get("response_handle") or ""), "MetricDuck receipt lacks connector response provenance")
    spec = normalize_query_spec(result.get("query_spec") or {})
    ensure(result.get("query_sha256") == spec["query_sha256"], "MetricDuck receipt query hash mismatch")
    rows = result.get("rows")
    ensure(isinstance(rows, list) and int(result.get("returned_count", -1)) == len(rows), "MetricDuck receipt returned count mismatch")
    seen_rows: set[str] = set()
    for raw in rows:
        ensure(isinstance(raw, Mapping), "MetricDuck receipt row invalid")
        row = _self_hash(raw, "result_row_sha256", "MetricDuck result row")
        ensure(str(row.get("ticker") or ""), "MetricDuck receipt row ticker missing")
        ensure(not _forbidden_paths(row), "MetricDuck receipt row contains market/recovery data")
        ensure(_passes_filters(row.get("facts") or {}, spec["filters"]), "MetricDuck receipt row does not satisfy approved lane filters")
        row_hash = str(row["result_row_sha256"])
        ensure(row_hash not in seen_rows, "MetricDuck receipt duplicate result row")
        seen_rows.add(row_hash)
    matched = int(result.get("matched_count") if result.get("matched_count") is not None else -1)
    complete = matched == len(rows) and len(rows) <= int(spec["limit"])
    ensure(result.get("complete") is complete and result.get("requires_split") is (not complete), "MetricDuck receipt completeness declaration mismatch")
    return result


def _partition_coverage(specs: list[Mapping[str, Any]], *, lane: str, sector: str) -> None:
    ordered = sorted(specs, key=lambda item: float(item["market_cap_min"]))
    ensure(ordered, f"MetricDuck plan missing partition for {lane}/{sector}")
    expected_low = 0.0
    for index, spec in enumerate(ordered):
        low = float(spec["market_cap_min"])
        high = spec.get("market_cap_max")
        ensure(math.isclose(low, expected_low, rel_tol=0, abs_tol=1e-9), f"MetricDuck plan market-cap gap or overlap for {lane}/{sector}")
        if high is None:
            ensure(index == len(ordered) - 1, f"MetricDuck plan unbounded range is not final for {lane}/{sector}")
            expected_low = math.inf
        else:
            expected_low = float(high)
    ensure(math.isinf(expected_low), f"MetricDuck plan does not cover the unbounded upper range for {lane}/{sector}")


def build_query_plan(market_index_value: Mapping[str, Any], *, leaves: Iterable[Mapping[str, Any]],
                     receipts: Iterable[Mapping[str, Any]],
                     regression_expectations: Iterable[str] = ()) -> dict[str, Any]:
    index = validate_market_index(market_index_value)
    specs = [normalize_query_spec(raw) for raw in leaves]
    receipt_rows = [validate_query_receipt(raw) for raw in receipts]
    by_query: dict[str, dict[str, Any]] = {}
    for receipt in receipt_rows:
        key = str(receipt["query_sha256"])
        ensure(key not in by_query, "MetricDuck plan duplicate receipt")
        by_query[key] = receipt
    ensure(len({spec["query_sha256"] for spec in specs}) == len(specs), "MetricDuck plan duplicate leaf query")
    ensure(set(by_query) == {str(spec["query_sha256"]) for spec in specs}, "MetricDuck plan leaf/receipt set mismatch")
    lanes = sorted(load_policy()["bootstrap"]["lane_score_weights"])
    sectors = list(index["eligible_sectors"])
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for spec in specs:
        grouped[(str(spec["lane"]), str(spec["sector"]))].append(spec)
    for lane in lanes:
        for sector in sectors:
            _partition_coverage(grouped.get((lane, sector), []), lane=lane, sector=sector)
    ensure(set(grouped) == {(lane, sector) for lane in lanes for sector in sectors}, "MetricDuck plan contains unexpected lane/sector partitions")
    for receipt in receipt_rows:
        ensure(receipt.get("complete") is True and receipt.get("requires_split") is False, "MetricDuck plan contains a saturated or incomplete leaf query")
    specs.sort(key=lambda item: (str(item["lane"]), str(item["sector"]), float(item["market_cap_min"])))
    receipt_rows.sort(key=lambda item: str(item["query_sha256"]))
    body = {
        "schema_version": "1.0.0",
        "kind": QUERY_PLAN_KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "market_session_id": index["market_session_id"],
        "market_index_sha256": index["market_index_sha256"],
        "required_lanes": lanes,
        "eligible_sectors": sectors,
        "partition_dimension": "market_cap",
        "connector_trust_class": CONNECTOR_TRUST_CLASS,
        "external_cryptographic_signature_available": False,
        "partition_coverage_complete": True,
        "leaf_count": len(specs),
        "receipt_count": len(receipt_rows),
        "regression_expectations": sorted({str(item) for item in regression_expectations if str(item)}),
        "leaves": specs,
        "receipts": receipt_rows,
    }
    value = {**body, "query_plan_sha256": semantic_hash(body)}
    validate_query_plan(value, market_index_value=index)
    return value


def validate_query_plan(value: Mapping[str, Any], *, market_index_value: Mapping[str, Any]) -> dict[str, Any]:
    index = validate_market_index(market_index_value)
    result = _self_hash(value, "query_plan_sha256", "MetricDuck query plan")
    ensure(result.get("schema_version") == "1.0.0" and result.get("kind") == QUERY_PLAN_KIND, "invalid V4.2 MetricDuck query plan")
    ensure(result.get("architecture_version") == ARCHITECTURE_VERSION, "MetricDuck query plan architecture mismatch")
    ensure(result.get("market_session_id") == index["market_session_id"] and result.get("market_index_sha256") == index["market_index_sha256"], "MetricDuck query plan market binding mismatch")
    ensure(result.get("connector_trust_class") == CONNECTOR_TRUST_CLASS and result.get("external_cryptographic_signature_available") is False, "MetricDuck plan trust boundary mismatch")
    ensure(result.get("partition_dimension") == "market_cap" and result.get("partition_coverage_complete") is True, "MetricDuck plan partition declaration invalid")
    specs = [normalize_query_spec(raw) for raw in result.get("leaves") or []]
    receipts = [validate_query_receipt(raw) for raw in result.get("receipts") or []]
    ensure(int(result.get("leaf_count", -1)) == len(specs) and int(result.get("receipt_count", -1)) == len(receipts), "MetricDuck plan counts mismatch")
    ensure(list(result.get("required_lanes") or []) == sorted(load_policy()["bootstrap"]["lane_score_weights"]), "MetricDuck plan required lane set mismatch")
    ensure(list(result.get("eligible_sectors") or []) == list(index["eligible_sectors"]), "MetricDuck plan sector set mismatch")
    by_query = {str(receipt["query_sha256"]): receipt for receipt in receipts}
    ensure(len(by_query) == len(receipts) and set(by_query) == {str(spec["query_sha256"]) for spec in specs}, "MetricDuck plan receipt coverage mismatch")
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for spec in specs:
        grouped[(str(spec["lane"]), str(spec["sector"]))].append(spec)
        receipt = by_query[str(spec["query_sha256"])]
        ensure(receipt["query_spec"] == spec, "MetricDuck receipt query spec differs from plan leaf")
        ensure(receipt.get("complete") is True and receipt.get("requires_split") is False, "MetricDuck plan contains incomplete query receipt")
    expected = {(lane, sector) for lane in result["required_lanes"] for sector in result["eligible_sectors"]}
    ensure(set(grouped) == expected, "MetricDuck plan lane/sector coverage mismatch")
    for lane, sector in sorted(expected):
        _partition_coverage(grouped[(lane, sector)], lane=lane, sector=sector)
    ensure(isinstance(result.get("regression_expectations"), list), "MetricDuck plan regression expectations missing")
    return result


def market_lookup(index_value: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, list[Mapping[str, Any]]]]:
    index = validate_market_index(index_value)
    by_contract: dict[str, Mapping[str, Any]] = {}
    by_ticker: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in index["rows"]:
        by_contract[str(row["contract_id"])] = row
        by_ticker[str(row["ticker"]).upper()].append(row)
    return by_contract, dict(by_ticker)


def receipt_lane_membership(plan_value: Mapping[str, Any], *, market_index_value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return query receipt metadata keyed by immutable receipt hash."""
    plan = validate_query_plan(plan_value, market_index_value=market_index_value)
    output: dict[str, dict[str, Any]] = {}
    for receipt in plan["receipts"]:
        output[str(receipt["receipt_sha256"])] = {
            "lane": str(receipt["query_spec"]["lane"]),
            "sector": str(receipt["query_spec"]["sector"]),
            "query_sha256": str(receipt["query_sha256"]),
            "rows": list(receipt["rows"]),
        }
    return output
