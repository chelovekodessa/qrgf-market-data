#!/usr/bin/env python3
"""Quality-first Core500 bootstrap ranking.

This module never reads current price performance, drawdown, or recovery setup.
It ranks a research cohort only; it does not declare Structural Quality pass/fail.
"""
from __future__ import annotations

import csv
import math
import re
from collections import Counter
from typing import Any, Iterable, Mapping

from common import ROOT, clamp, file_hash, load_policy, number, piecewise, semantic_hash
from contracts import validate as validate_contract
import provenance, selection

FORBIDDEN_RECOVERY_FIELDS = {
    "price", "current_price", "last", "close", "bid", "ask", "quote",
    "reference_52w_high", "distance_to_high_pct", "drawdown", "drawdown_pct",
    "drawdown_52w_pct", "return_5d_pct", "return_1m_pct", "return_3m_pct",
    "return_6m_pct", "return_12m_pct", "recovery_setup_score", "l2_setup_score",
    "research_priority_score", "rsi", "atr", "momentum", "historical_volatility_pct",
    "setup_prior_growth", "setup_pullback_geometry", "setup_liquidity",
}


def _forbidden_field_name(value: Any) -> bool:
    """Fail closed for common aliases of current-market/recovery inputs.

    The selector accepts structural facts, not a merely renamed price series.
    ``return_on_*`` remains a permitted accounting metric; market returns do
    not.
    """
    name = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    if name in FORBIDDEN_RECOVERY_FIELDS:
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
    """Return every forbidden current-market/recovery field below a source row.

    This is deliberately recursive.  A producer cannot evade the selector
    boundary by moving price or recovery data beneath ``facts`` or another
    nested payload.
    """
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            dotted = f"{path}.{key}" if path else str(key)
            if _forbidden_field_name(key_text):
                found.append(dotted)
            found.extend(_forbidden_paths(child, dotted))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_paths(child, f"{path}[{index}]"))
    return found


def _pct(value: Any) -> float | None:
    v = number(value)
    if v is None:
        return None
    return v * 100.0 if -1.5 <= v <= 1.5 else v


def _higher(value: Any, points: list[tuple[float, float]], *, pct: bool = False) -> float | None:
    v = _pct(value) if pct else number(value)
    return piecewise(v, points) if v is not None else None


def _lower_leverage(value: Any) -> float | None:
    v = number(value)
    if v is None: return None
    if v <= 0: return 98.0
    return piecewise(v, [(0,98),(1,92),(2,82),(3,68),(4,52),(6,28),(10,5)])


def _cash_to_debt(facts: Mapping[str, Any]) -> float | None:
    cash, debt = number(facts.get("cash")), number(facts.get("debt"))
    if cash is None or debt is None: return None
    if debt <= 0: return 99.0
    return piecewise(cash / debt, [(0,15),(0.25,38),(0.5,58),(1,80),(2,94),(5,99)])


def _dilution(value: Any) -> float | None:
    v = _pct(value)
    if v is None: return None
    if v <= 0: return 99.0
    return piecewise(v, [(0,99),(2,94),(5,82),(8,68),(15,42),(25,18),(40,5)])


def _concentration(value: Any) -> float | None:
    v = _pct(value)
    if v is None: return None
    return piecewise(v, [(0,99),(10,95),(25,85),(35,70),(50,50),(70,25),(100,5)])


def _market_scale(value: Any) -> float | None:
    cap = number(value)
    if cap is None or cap <= 0: return None
    # Log scale: $250m ~= 35, $2b ~= 55, $10b ~= 70, $100b ~= 88, $1t ~= 99.
    x = math.log10(cap)
    return piecewise(x, [(8.39794,35),(9.30103,55),(10,70),(11,88),(12,99)])


def _metric_scores(row: Mapping[str, Any]) -> dict[str, float | None]:
    f = row.get("facts") if isinstance(row.get("facts"), Mapping) else {}
    return {
        "roic": _higher(f.get("roic"), [(-10,8),(0,35),(5,58),(10,74),(15,87),(25,97),(40,99)], pct=True),
        "operating_margin_pct": _higher(f.get("operating_margin_pct"), [(-30,5),(-10,20),(0,45),(5,60),(10,74),(20,90),(35,98)], pct=True),
        "fcf_margin_pct": _higher(f.get("fcf_margin_pct"), [(-30,5),(-10,20),(0,48),(5,62),(10,76),(20,91),(35,98)], pct=True),
        "revenue_cagr_3y_pct": _higher(f.get("revenue_cagr_3y_pct"), [(-20,8),(0,42),(5,58),(10,72),(20,86),(30,94),(50,99)], pct=True),
        "net_debt_to_ebitda": _lower_leverage(f.get("net_debt_to_ebitda")),
        "cash_to_debt": _cash_to_debt(f),
        "dilution_pct_yoy": _dilution(f.get("dilution_pct_yoy")),
        "market_scale": _market_scale(row.get("connector_market_cap")),
        "roa_pct": _higher(f.get("roa_pct"), [(-2,5),(0,35),(0.7,65),(1,78),(1.5,92),(2.5,99)], pct=True),
        "roe_pct": _higher(f.get("roe_pct"), [(-10,5),(0,35),(7,62),(10,76),(15,91),(22,99)], pct=True),
        "cet1_ratio_pct": _higher(f.get("cet1_ratio_pct"), [(5,5),(7,30),(9,58),(11,76),(13,90),(16,98)], pct=True),
        "nonperforming_assets_pct": _higher(f.get("nonperforming_assets_pct"), [(0,99),(0.5,92),(1,80),(2,60),(3,40),(5,15),(10,5)], pct=True),
        "holdings_quality_score": number(f.get("holdings_quality_score")),
        "sector_fundamental_quality_score": number(f.get("sector_fundamental_quality_score")),
        "breadth_quality_score": number(f.get("breadth_quality_score")),
        "fund_structure_quality_score": number(f.get("fund_structure_quality_score")),
        "top10_concentration_control": _concentration(f.get("top10_weight_pct")),
        "fund_aum_scale": _market_scale(f.get("fund_aum")),
    }


def _weighted_lane(scores: Mapping[str, float | None], weights: Mapping[str, Any]) -> tuple[float | None, float, dict[str, float]]:
    concrete = {k: number(scores.get(k)) for k in weights if k != "data_completeness"}
    total_possible = sum(float(w) for k,w in weights.items() if k != "data_completeness")
    known_weight = sum(float(weights[k]) for k,v in concrete.items() if v is not None)
    coverage = 100.0 * known_weight / total_possible if total_possible else 0.0
    if known_weight <= 0:
        return None, 0.0, {}
    known_score = sum(clamp(float(v)) * float(weights[k]) for k,v in concrete.items() if v is not None) / known_weight
    # Missing facts reduce priority but never become a structural fail.
    confidence_multiplier = 0.70 + 0.30 * coverage / 100.0
    data_weight = float(weights.get("data_completeness", 0))
    base_weight = sum(float(w) for w in weights.values()) - data_weight
    adjusted = known_score * confidence_multiplier
    if data_weight > 0 and base_weight > 0:
        adjusted = (adjusted * base_weight + coverage * data_weight) / (base_weight + data_weight)
    return round(clamp(adjusted),4), round(coverage,2), {k:round(float(v),4) for k,v in concrete.items() if v is not None}


def _lane_allowed(lane: str, row: Mapping[str, Any]) -> bool:
    sec = str(row.get("security_type") or "common_equity").lower()
    if lane == "etf":
        return sec == "etf" and list(row.get("quality_candidate_lanes") or []) == ["etf"]
    if sec == "etf":
        return False
    # V4.2.2 lane eligibility comes only from complete connector receipts.
    # Neither Radar sector nor a model guess may create a lane.
    discovered = {str(x) for x in row.get("quality_candidate_lanes") or []}
    return lane in discovered


def score_candidate(raw: Mapping[str, Any]) -> dict[str, Any]:
    # Reject accidental recovery contamination at the boundary instead of silently ignoring it.
    bad = sorted(_forbidden_paths(raw))
    if bad:
        raise ValueError(f"bootstrap candidate contains recovery/current-price fields: {bad}")
    row = selection.normalize_candidate(raw)
    scores = _metric_scores(row)
    policy = load_policy()["bootstrap"]
    lanes: list[dict[str, Any]] = []
    for lane, weights in policy["lane_score_weights"].items():
        if not _lane_allowed(lane, row):
            continue
        lane_score, coverage, used = _weighted_lane(scores, weights)
        lanes.append({"lane":lane,"score":lane_score,"coverage_pct":coverage,"used_scores":used})
    valid = [x for x in lanes if x["score"] is not None and x["coverage_pct"] >= float(policy["minimum_fact_coverage_pct"])]
    best = max(valid, key=lambda x:(float(x["score"]),float(x["coverage_pct"]),x["lane"])) if valid else None
    row["bootstrap_lane_scores"] = lanes
    row["bootstrap_best_lane"] = best["lane"] if best else None
    row["bootstrap_priority_score"] = best["score"] if best else None
    row["bootstrap_fact_coverage_pct"] = best["coverage_pct"] if best else 0.0
    row["bootstrap_model_version"] = policy["selection_model_version"]
    return row


def _rank_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    score = number(row.get("bootstrap_priority_score"))
    coverage = number(row.get("bootstrap_fact_coverage_pct"))
    cap = number(row.get("connector_market_cap"))
    liq = number(row.get("avg_dollar_volume"))
    ticker, contract = selection.identity(row)
    return (
        -(score if score is not None else -1),
        -(coverage if coverage is not None else -1),
        -(cap if cap is not None else -1),
        -(liq if liq is not None else -1),
        ticker,
        contract,
    )

MASTER_SIZE = 500
ARCHITECTURE_VERSION = provenance.ARCHITECTURE_VERSION
MASTER_KIND = "qrgf_v42_master_core500"
CERTIFICATE_KIND = "qrgf_v42_master_core500_selector_certificate"
SOURCE_KIND = "qrgf_v42_quality_candidate_source"
BUNDLE_KIND = "qrgf_v42_master_core500_bundle"
BUILD_REQUEST_KIND = "qrgf_v42_master_core500_build_request"
POINTER_KIND = "qrgf_v42_master_core500_pointer"
SELECTOR_ORDERING_MODEL = "bootstrap_priority_score_then_coverage_connector_market_cap_liquidity_identity-v3"
SOURCE_DERIVATION_MODEL = "identity_map_plus_radar_membership_plus_metricduck_classification_and_quality_plan_plus_etf_catalog-v4"


def _exact_master_size() -> int:
    size = int(load_policy()["bootstrap"]["master_core500_exact_size"])
    if size != MASTER_SIZE:
        raise ValueError("production MASTER CORE500 must be exactly 500")
    return size


def _require_hash(value: Any, label: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return text


def _membership_for_result(row: Mapping[str, Any], *, by_contract: Mapping[str, Mapping[str, Any]],
                           by_ticker: Mapping[str, list[Mapping[str, Any]]]) -> Mapping[str, Any] | None:
    """Bind a connector row to the pinned market when unambiguous.

    Connector-wide discovery legitimately contains companies outside the current
    Radar.  Those rows are explicitly accounted as connector-only evidence and
    can never enter MASTER.  A contradictory ticker/contract pair remains fatal.
    """
    contract = str(row.get("contract_id") or "")
    ticker = str(row.get("ticker") or "").upper()
    if contract:
        membership = by_contract.get(contract)
        if membership is None:
            return None
        if str(membership.get("ticker") or "").upper() != ticker:
            raise ValueError("MetricDuck result ticker/contract identity mismatch")
        return membership
    matches = list(by_ticker.get(ticker) or [])
    return matches[0] if len(matches) == 1 else None


def _approved_etf_rows() -> tuple[dict[tuple[str, str], dict[str, Any]], str]:
    path = ROOT / "config" / "approved-etfs.csv"
    approved: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            ticker = str(raw.get("ticker") or "").strip().upper()
            contract = str(raw.get("contract_id") or "").strip()
            status = str(raw.get("status") or "").strip().lower()
            inverse = str(raw.get("inverse") or "").strip().lower() == "true"
            daily_reset = str(raw.get("daily_reset") or "").strip().lower() == "true"
            leverage = number(raw.get("leverage_multiple"))
            if ticker and contract and status == "approved" and not inverse and not daily_reset and leverage == 1:
                body = {str(k): v for k, v in raw.items()}
                approved[(ticker, contract)] = {**body, "approved_etf_row_sha256": semantic_hash(body)}
    return approved, file_hash(path)


def build_candidate_source(identity_map_value: Mapping[str, Any], market_index_value: Mapping[str, Any], query_plan_value: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the production candidate source from evidence without Radar classification.

    The classification catalog is connector-wide and sectorless.  Its sector code
    and market cap are bound to market membership before quality-lane results may
    enter the candidate union.  Radar reference fields are preserved only as
    optional observed facts and never drive connector routing or lane assignment.
    """
    identity_map = provenance.validate_identity_map(identity_map_value)
    market_index = provenance.validate_market_index_against_identity_map(market_index_value, identity_map)
    query_plan = provenance.validate_query_plan(query_plan_value, market_index_value=market_index)
    by_contract, by_ticker = provenance.market_lookup(market_index)

    classification_by_membership: dict[str, dict[str, Any]] = {}
    classification_connector_only = 0
    total_query_rows = 0
    for receipt in query_plan["receipts"]:
        spec = receipt["query_spec"]
        if spec["purpose"] != provenance.QUERY_PURPOSE_CLASSIFICATION:
            continue
        receipt_sha = str(receipt["receipt_sha256"])
        query_sha = str(receipt["query_sha256"])
        for result_row in receipt["rows"]:
            total_query_rows += 1
            membership = _membership_for_result(result_row, by_contract=by_contract, by_ticker=by_ticker)
            if membership is None:
                classification_connector_only += 1
                continue
            key = str(membership["market_membership_sha256"])
            binding_body = {
                "market_membership_sha256": key,
                "ticker": membership["ticker"],
                "contract_id": membership["contract_id"],
                "sector_code": str(result_row["sector_code"]),
                "connector_market_cap": number(result_row.get("connector_market_cap")),
                "classification_receipt_sha256": receipt_sha,
                "classification_query_sha256": query_sha,
                "classification_result_row_sha256": str(result_row["result_row_sha256"]),
                "trust_class": "connector_attested",
            }
            binding = {**binding_body, "classification_binding_sha256": semantic_hash(binding_body)}
            if key in classification_by_membership:
                raise ValueError(f"duplicate MetricDuck classification binding for {membership['ticker']}")
            classification_by_membership[key] = binding

    candidates: dict[str, dict[str, Any]] = {}
    excluded: list[dict[str, Any]] = []
    quality_result_row_count = 0
    for receipt in query_plan["receipts"]:
        spec = receipt["query_spec"]
        if spec["purpose"] != provenance.QUERY_PURPOSE_QUALITY:
            continue
        lane = str(spec["lane"])
        receipt_sha = str(receipt["receipt_sha256"])
        query_sha = str(receipt["query_sha256"])
        cap_low = float(spec["market_cap_min"])
        cap_high = spec.get("market_cap_max")
        for result_row in receipt["rows"]:
            total_query_rows += 1
            quality_result_row_count += 1
            membership = _membership_for_result(result_row, by_contract=by_contract, by_ticker=by_ticker)
            if membership is None:
                excluded.append({
                    "ticker": result_row["ticker"],
                    "contract_id": result_row.get("contract_id"),
                    "result_row_sha256": result_row["result_row_sha256"],
                    "receipt_sha256": receipt_sha,
                    "reason": "connector_result_not_in_pinned_market",
                })
                continue
            connector_cap = number(result_row.get("connector_market_cap"))
            if connector_cap is None or connector_cap < cap_low or (cap_high is not None and connector_cap >= float(cap_high)):
                raise ValueError("MetricDuck result falls outside its declared connector market-cap partition")
            if membership.get("master_eligible") is not True:
                excluded.append({
                    "ticker": membership["ticker"],
                    "contract_id": membership["contract_id"],
                    "market_membership_sha256": membership["market_membership_sha256"],
                    "result_row_sha256": result_row["result_row_sha256"],
                    "receipt_sha256": receipt_sha,
                    "reason": "market_identity_or_eligibility_unresolved",
                })
                continue
            key = str(membership["market_membership_sha256"])
            classification = classification_by_membership.get(key)
            if classification is None:
                raise ValueError(f"MASTER candidate lacks MetricDuck classification binding: {membership['ticker']}")
            if str(result_row.get("sector_code") or "") != str(classification["sector_code"]):
                raise ValueError(f"MetricDuck classification conflict for {membership['ticker']}")
            if key not in candidates:
                candidates[key] = {
                    "ticker": membership["ticker"],
                    "contract_id": membership["contract_id"],
                    "company": membership.get("company"),
                    "issuer_id": membership["issuer_id"],
                    "share_class_group": membership.get("share_class_group"),
                    "security_type": membership["security_type"],
                    "security_overlay": membership["security_overlay"],
                    "research_scope_key": membership["research_scope_key"],
                    "identity_resolution_status": membership["identity_resolution_status"],
                    "market_sector": membership.get("sector"),
                    "market_industry": membership.get("industry"),
                    "market_market_cap": membership.get("market_cap"),
                    "market_sector_status": membership.get("market_sector_status"),
                    "market_cap_status": membership.get("market_cap_status"),
                    "sector_code": classification["sector_code"],
                    "connector_market_cap": classification["connector_market_cap"],
                    "classification_binding_sha256": classification["classification_binding_sha256"],
                    "classification_receipt_sha256": classification["classification_receipt_sha256"],
                    "classification_query_sha256": classification["classification_query_sha256"],
                    "classification_result_row_sha256": classification["classification_result_row_sha256"],
                    "avg_dollar_volume": membership.get("avg_dollar_volume"),
                    "market_membership_sha256": membership["market_membership_sha256"],
                    "market_source_row_sha256": membership["source_row_sha256"],
                    "market_index_sha256": market_index["market_index_sha256"],
                    "query_plan_sha256": query_plan["query_plan_sha256"],
                    "market_membership_bound": True,
                    "classification_bound": True,
                    "provenance_class": "market_bound_connector_attested",
                    "quality_candidate_lanes": [],
                    "query_receipt_sha256s": [],
                    "query_sha256s": [],
                    "result_row_sha256s": [],
                    "facts": {},
                }
            candidate = candidates[key]
            candidate["quality_candidate_lanes"].append(lane)
            candidate["query_receipt_sha256s"].append(receipt_sha)
            candidate["query_sha256s"].append(query_sha)
            candidate["result_row_sha256s"].append(str(result_row["result_row_sha256"]))
            for field, value in dict(result_row.get("facts") or {}).items():
                if field in candidate["facts"] and semantic_hash(candidate["facts"][field]) != semantic_hash(value):
                    raise ValueError(f"conflicting MetricDuck fact for {candidate['ticker']}: {field}")
                candidate["facts"][field] = value

    approved_etfs, approved_etf_catalog_sha256 = _approved_etf_rows()
    approved_etf_candidate_count = 0
    for membership in market_index["rows"]:
        if membership.get("master_eligible") is not True or str(membership.get("security_type") or "").lower() != "etf":
            continue
        key_tuple = (str(membership.get("ticker") or "").upper(), str(membership.get("contract_id") or ""))
        catalog_row = approved_etfs.get(key_tuple)
        if catalog_row is None:
            continue
        key = str(membership["market_membership_sha256"])
        if key in candidates:
            raise ValueError("ETF candidate unexpectedly overlaps a MetricDuck company row")
        candidates[key] = {
            "ticker": membership["ticker"],
            "contract_id": membership["contract_id"],
            "company": membership.get("company"),
            "issuer_id": membership["issuer_id"],
            "share_class_group": membership.get("share_class_group"),
            "security_type": membership["security_type"],
            "security_overlay": membership["security_overlay"],
            "research_scope_key": membership["research_scope_key"],
            "identity_resolution_status": membership["identity_resolution_status"],
            "market_sector": membership.get("sector"),
            "market_industry": membership.get("industry"),
            "market_market_cap": membership.get("market_cap"),
            "market_sector_status": membership.get("market_sector_status"),
            "market_cap_status": membership.get("market_cap_status"),
            "sector_code": None,
            "connector_market_cap": None,
            "avg_dollar_volume": membership.get("avg_dollar_volume"),
            "market_membership_sha256": membership["market_membership_sha256"],
            "market_source_row_sha256": membership["source_row_sha256"],
            "market_index_sha256": market_index["market_index_sha256"],
            "query_plan_sha256": query_plan["query_plan_sha256"],
            "market_membership_bound": True,
            "classification_bound": False,
            "provenance_class": "market_bound_approved_etf_catalog",
            "approved_etf_catalog_sha256": approved_etf_catalog_sha256,
            "approved_etf_row_sha256": catalog_row["approved_etf_row_sha256"],
            "quality_candidate_lanes": ["etf"],
            "query_receipt_sha256s": [],
            "query_sha256s": [],
            "result_row_sha256s": [],
            "facts": {},
        }
        approved_etf_candidate_count += 1

    rows: list[dict[str, Any]] = []
    lane_counts: Counter[str] = Counter()
    for raw in candidates.values():
        raw["quality_candidate_lanes"] = sorted(set(raw["quality_candidate_lanes"]))
        raw["query_receipt_sha256s"] = sorted(set(raw["query_receipt_sha256s"]))
        raw["query_sha256s"] = sorted(set(raw["query_sha256s"]))
        raw["result_row_sha256s"] = sorted(set(raw["result_row_sha256s"]))
        for lane in raw["quality_candidate_lanes"]:
            lane_counts[lane] += 1
        body = dict(raw)
        rows.append({**body, "candidate_row_sha256": semantic_hash(body)})
    rows.sort(key=lambda row: (str(row["ticker"]), str(row["contract_id"])))
    excluded.sort(key=lambda row: (str(row.get("ticker") or ""), str(row.get("contract_id") or ""), str(row.get("receipt_sha256") or "")))

    master_eligible_non_etf = [row for row in market_index["rows"] if row.get("master_eligible") is True and str(row.get("security_type") or "").lower() != "etf"]
    unclassified_market_count = sum(str(row["market_membership_sha256"]) not in classification_by_membership for row in master_eligible_non_etf)
    identity = {
        "kind": SOURCE_DERIVATION_MODEL,
        "identity_map_sha256": identity_map["identity_map_sha256"],
        "market_index_sha256": market_index["market_index_sha256"],
        "query_plan_sha256": query_plan["query_plan_sha256"],
        "market_snapshot_sha256": market_index["source_manifest_sha256"],
        "trust_class": "market_bound_connector_attested",
        "external_cryptographic_signature_available": False,
        "approved_etf_catalog_sha256": approved_etf_catalog_sha256,
        "radar_classification_used": False,
    }
    body = {
        "schema_version": "3.0.0",
        "kind": SOURCE_KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "market_session_id": market_index["market_session_id"],
        "eligible_universe_size": market_index["eligible_universe_size"],
        "master_eligible_market_rows": market_index["master_eligible_count"],
        "candidate_source_identity": identity,
        "identity_map_sha256": identity_map["identity_map_sha256"],
        "market_index_sha256": market_index["market_index_sha256"],
        "query_plan_sha256": query_plan["query_plan_sha256"],
        "query_receipt_count": query_plan["receipt_count"],
        "query_result_row_count": total_query_rows,
        "quality_query_result_row_count": quality_result_row_count,
        "classification_bound_market_rows": len(classification_by_membership),
        "classification_connector_only_rows": classification_connector_only,
        "master_eligible_non_etf_without_classification_count": unclassified_market_count,
        "approved_etf_candidate_count": approved_etf_candidate_count,
        "approved_etf_catalog_sha256": approved_etf_catalog_sha256,
        "excluded_query_row_count": len(excluded),
        "quality_candidate_union_size": len(rows),
        "lane_counts": dict(sorted(lane_counts.items())),
        "current_recovery_used": False,
        "radar_classification_used": False,
        "forbidden_recovery_fields": [],
        "source_derivation_model": SOURCE_DERIVATION_MODEL,
        "publisher_recompute_required": True,
        "regression_expectations": list(query_plan["regression_expectations"]),
        "excluded_query_rows": excluded,
        "candidates": rows,
    }
    value = {**body, "source_sha256": semantic_hash(body)}
    validate_candidate_source(value)
    return value


def validate_candidate_source(value: Mapping[str, Any]) -> dict[str, Any]:
    v = dict(value)
    body = {k: x for k, x in v.items() if k != "source_sha256"}
    if v.get("schema_version") != "3.0.0" or v.get("kind") != SOURCE_KIND:
        raise ValueError("invalid V4.2 quality candidate source")
    if v.get("architecture_version") != ARCHITECTURE_VERSION or v.get("source_sha256") != semantic_hash(body):
        raise ValueError("quality candidate source hash or architecture mismatch")
    if not str(v.get("market_session_id") or ""):
        raise ValueError("quality candidate source market session missing")
    if v.get("current_recovery_used") is not False or v.get("radar_classification_used") is not False or list(v.get("forbidden_recovery_fields") or []) != []:
        raise ValueError("quality candidate source must not use recovery/current price/Radar classification")
    if v.get("source_derivation_model") != SOURCE_DERIVATION_MODEL or v.get("publisher_recompute_required") is not True:
        raise ValueError("quality candidate source is not a publisher-recomputed derivation")
    _require_hash(v.get("identity_map_sha256"), "candidate source identity map hash")
    _require_hash(v.get("market_index_sha256"), "candidate source market index hash")
    _require_hash(v.get("query_plan_sha256"), "candidate source query plan hash")
    identity = v.get("candidate_source_identity")
    if not isinstance(identity, Mapping) or identity.get("kind") != SOURCE_DERIVATION_MODEL:
        raise ValueError("quality candidate source identity is missing")
    if identity.get("identity_map_sha256") != v["identity_map_sha256"] or identity.get("market_index_sha256") != v["market_index_sha256"] or identity.get("query_plan_sha256") != v["query_plan_sha256"]:
        raise ValueError("quality candidate source identity hash binding mismatch")
    if identity.get("radar_classification_used") is not False:
        raise ValueError("candidate source identity improperly depends on Radar classification")
    _require_hash(identity.get("market_snapshot_sha256"), "candidate source market snapshot hash")
    if identity.get("trust_class") != "market_bound_connector_attested" or identity.get("external_cryptographic_signature_available") is not False:
        raise ValueError("quality candidate source trust boundary mismatch")
    _require_hash(identity.get("approved_etf_catalog_sha256"), "candidate source ETF catalog hash")
    if identity.get("approved_etf_catalog_sha256") != v.get("approved_etf_catalog_sha256"):
        raise ValueError("candidate source ETF catalog binding mismatch")
    rows = v.get("candidates")
    if not isinstance(rows, list) or len(rows) < MASTER_SIZE:
        raise ValueError("quality candidate source must contain at least 500 rows")
    if int(v.get("quality_candidate_union_size") or -1) != len(rows):
        raise ValueError("quality candidate union size mismatch")
    if int(v.get("eligible_universe_size") or 0) < len(rows):
        raise ValueError("quality candidate source universe is smaller than its union")
    if int(v.get("master_eligible_market_rows") or 0) < len(rows):
        raise ValueError("quality candidate source exceeds resolved market membership")
    lane_counts = v.get("lane_counts")
    if not isinstance(lane_counts, Mapping) or not lane_counts:
        raise ValueError("quality candidate source lane coverage missing")
    observed: Counter[str] = Counter()
    contracts: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"quality candidate source row {index} is invalid")
        row = dict(raw)
        row_body = {k: x for k, x in row.items() if k != "candidate_row_sha256"}
        if row.get("candidate_row_sha256") != semantic_hash(row_body):
            raise ValueError("quality candidate source row self hash mismatch")
        bad = _forbidden_paths(row)
        if bad:
            raise ValueError(f"quality candidate source contains recovery/current-price fields: {sorted(bad)}")
        contract = str(row.get("contract_id") or "")
        if not contract or contract in contracts:
            raise ValueError("quality candidate source duplicate or missing contract")
        contracts.add(contract)
        if row.get("market_membership_bound") is not True:
            raise ValueError("quality candidate source row is not market bound")
        is_etf = str(row.get("security_type") or "").lower() == "etf"
        expected_provenance = "market_bound_approved_etf_catalog" if is_etf else "market_bound_connector_attested"
        if row.get("provenance_class") != expected_provenance:
            raise ValueError("quality candidate source row provenance class mismatch")
        if row.get("market_index_sha256") != v["market_index_sha256"] or row.get("query_plan_sha256") != v["query_plan_sha256"]:
            raise ValueError("quality candidate source row evidence hash mismatch")
        _require_hash(row.get("market_membership_sha256"), "candidate market membership hash")
        _require_hash(row.get("market_source_row_sha256"), "candidate market source row hash")
        if str(row.get("identity_resolution_status") or "") not in {"official", "security_specific"}:
            raise ValueError("quality candidate source row issuer identity is unresolved")
        issuer = str(row.get("issuer_id") or "")
        overlay = str(row.get("security_overlay") or "")
        if not issuer.startswith(("CIK:", "SECURITY:")) or row.get("research_scope_key") != f"{issuer}|{overlay}":
            raise ValueError("quality candidate source row issuer scope invalid")
        lanes = row.get("quality_candidate_lanes")
        receipts = row.get("query_receipt_sha256s")
        queries = row.get("query_sha256s")
        result_rows = row.get("result_row_sha256s")
        if not isinstance(lanes, list) or not lanes or any(str(lane) not in load_policy()["bootstrap"]["lane_score_weights"] for lane in lanes):
            raise ValueError("quality candidate source row has no valid quality lane")
        if not isinstance(receipts, list) or not isinstance(queries, list) or not isinstance(result_rows, list):
            raise ValueError("quality candidate source row query provenance containers invalid")
        if is_etf:
            if receipts or queries or result_rows or lanes != ["etf"] or row.get("classification_bound") is not False:
                raise ValueError("ETF candidate must use only the approved ETF catalog lane")
            _require_hash(row.get("approved_etf_catalog_sha256"), "ETF catalog hash")
            _require_hash(row.get("approved_etf_row_sha256"), "ETF catalog row hash")
        else:
            if not receipts or not queries or not result_rows or row.get("classification_bound") is not True:
                raise ValueError("MASTER candidate has incomplete MetricDuck quality/classification binding")
            if str(row.get("sector_code") or "") not in provenance._supported_sector_codes():
                raise ValueError("MASTER candidate has unsupported connector sector code")
            if number(row.get("connector_market_cap")) is None:
                raise ValueError("MASTER candidate lacks connector-derived market cap")
            for field in ("classification_binding_sha256", "classification_receipt_sha256", "classification_query_sha256", "classification_result_row_sha256"):
                _require_hash(row.get(field), f"candidate {field}")
            for digest in [*receipts, *queries, *result_rows]:
                _require_hash(digest, "candidate query provenance hash")
        for lane in sorted(set(str(x) for x in lanes)):
            observed[lane] += 1
    if {str(k): int(vv) for k, vv in lane_counts.items()} != dict(sorted(observed.items())):
        raise ValueError("quality candidate source lane counts mismatch")
    expectations = list(v.get("regression_expectations") or [])
    minimum = int(load_policy()["validation_framework"]["minimum_regression_expectations"])
    if len(set(str(item) for item in expectations if str(item))) < minimum:
        raise ValueError("quality candidate source regression expectations are incomplete")
    return v


def validate_candidate_source_against_evidence(value: Mapping[str, Any], *, identity_map_value: Mapping[str, Any],
                                               market_index_value: Mapping[str, Any],
                                               query_plan_value: Mapping[str, Any]) -> dict[str, Any]:
    actual = validate_candidate_source(value)
    expected = build_candidate_source(identity_map_value, market_index_value, query_plan_value)
    if actual != expected:
        raise ValueError("quality candidate source is not the canonical derivation of identity map, market index and MetricDuck plan")
    return actual


def _representatives(source: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scored = [score_candidate(row) for row in source["candidates"]]
    eligible = [row for row in scored if number(row.get("bootstrap_priority_score")) is not None]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in eligible:
        groups.setdefault(str(row["research_scope_key"]), []).append(row)
    representatives: list[dict[str, Any]] = []
    for scope, members in groups.items():
        members.sort(key=_rank_key)
        representative = dict(members[0])
        representative["member_tickers"] = sorted({str(x.get("ticker") or "").upper() for x in members if x.get("ticker")})
        representative["member_contract_ids"] = sorted({str(x.get("contract_id") or "") for x in members if x.get("contract_id")})
        representative["member_market_membership_sha256s"] = sorted({str(x.get("market_membership_sha256") or "") for x in members})
        representatives.append(representative)
    representatives.sort(key=_rank_key)
    diagnostics = {
        "raw_quality_candidate_count": len(scored),
        "bootstrap_score_eligible_count": len(eligible),
        "unique_research_scope_count": len(representatives),
        "issuer_dedup_removed_rows": len(eligible) - len(representatives),
        "cutoff_rank": MASTER_SIZE,
        "cutoff_score": representatives[MASTER_SIZE - 1]["bootstrap_priority_score"] if len(representatives) >= MASTER_SIZE else None,
        "next_excluded_score": representatives[MASTER_SIZE]["bootstrap_priority_score"] if len(representatives) > MASTER_SIZE else None,
    }
    return representatives, diagnostics


def _compact_scope(row: Mapping[str, Any], rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "ticker": row["ticker"],
        "contract_id": row.get("contract_id"),
        "issuer_id": row["issuer_id"],
        "security_overlay": row["security_overlay"],
        "research_scope_key": row["research_scope_key"],
        "security_type": row.get("security_type"),
        "identity_resolution_status": row.get("identity_resolution_status"),
        "sector_code": row.get("sector_code"),
        "market_sector": row.get("market_sector"),
        "connector_market_cap": row.get("connector_market_cap"),
        "avg_dollar_volume": row.get("avg_dollar_volume"),
        "member_tickers": row.get("member_tickers") or [row["ticker"]],
        "member_contract_ids": row.get("member_contract_ids") or [row.get("contract_id")],
        "member_market_membership_sha256s": row.get("member_market_membership_sha256s") or [row.get("market_membership_sha256")],
        "bootstrap_best_lane": row.get("bootstrap_best_lane"),
        "bootstrap_priority_score": row.get("bootstrap_priority_score"),
        "bootstrap_fact_coverage_pct": row.get("bootstrap_fact_coverage_pct"),
    }


def _selection_config_hash() -> str:
    p = load_policy()
    return semantic_hash({
        "architecture_version": ARCHITECTURE_VERSION,
        "source_derivation_model": SOURCE_DERIVATION_MODEL,
        "bootstrap": {
            "selection_model_version": p["bootstrap"]["selection_model_version"],
            "minimum_fact_coverage_pct": p["bootstrap"]["minimum_fact_coverage_pct"],
            "lane_score_weights": p["bootstrap"]["lane_score_weights"],
            "master_core500_exact_size": p["bootstrap"]["master_core500_exact_size"],
        },
        "campaign": {
            "canary_scope_count": p["campaign"]["canary_scope_count"],
            "pilot_scope_count": p["campaign"]["pilot_scope_count"],
        },
    })


def _derive_master_bundle(source: Mapping[str, Any]) -> dict[str, Any]:
    _exact_master_size()
    representatives, diagnostics = _representatives(source)
    if len(representatives) < MASTER_SIZE:
        raise ValueError("quality candidate source cannot produce exactly 500 unique research scopes")
    scopes = [_compact_scope(row, rank) for rank, row in enumerate(representatives[:MASTER_SIZE], 1)]
    canary_count = int(load_policy()["campaign"]["canary_scope_count"])
    pilot_count = int(load_policy()["campaign"]["pilot_scope_count"])
    content = {
        "schema_version": "2.0.0",
        "kind": MASTER_KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "market_session_id": source["market_session_id"],
        "selection_model_version": load_policy()["bootstrap"]["selection_model_version"],
        "requested_size": MASTER_SIZE,
        "selected_scope_count": MASTER_SIZE,
        "core500_is_research_bootstrap_not_whitelist": True,
        "current_recovery_used": False,
        "candidate_source_sha256": source["source_sha256"],
        "identity_map_sha256": source["identity_map_sha256"],
        "market_index_sha256": source["market_index_sha256"],
        "query_plan_sha256": source["query_plan_sha256"],
        "selector_config_sha256": _selection_config_hash(),
        "source_derivation_model": SOURCE_DERIVATION_MODEL,
        "publisher_recompute_required": True,
        "canary_scope_keys": [x["research_scope_key"] for x in scopes[:canary_count]],
        "pilot_scope_keys": [x["research_scope_key"] for x in scopes[:pilot_count]],
        "scopes": scopes,
    }
    master_content_sha256 = semantic_hash(content)
    selected_keys = {x["research_scope_key"] for x in scopes}
    expectations = list(source.get("regression_expectations") or [])
    certificate_body = {
        "schema_version": "2.0.0",
        "kind": CERTIFICATE_KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "master_content_sha256": master_content_sha256,
        "market_session_id": source["market_session_id"],
        "candidate_source_identity": source["candidate_source_identity"],
        "candidate_source_sha256": source["source_sha256"],
        "identity_map_sha256": source["identity_map_sha256"],
        "market_index_sha256": source["market_index_sha256"],
        "query_plan_sha256": source["query_plan_sha256"],
        "eligible_universe_size": source["eligible_universe_size"],
        "quality_candidate_union_size": source["quality_candidate_union_size"],
        "lane_counts": source["lane_counts"],
        "selector_model_version": load_policy()["bootstrap"]["selection_model_version"],
        "selector_config_sha256": _selection_config_hash(),
        "source_derivation_model": SOURCE_DERIVATION_MODEL,
        "publisher_recompute_required": True,
        "connector_trust_class": provenance.CONNECTOR_TRUST_CLASS,
        "external_cryptographic_signature_available": False,
        "fact_coverage": {
            "minimum_pct": load_policy()["bootstrap"]["minimum_fact_coverage_pct"],
            "eligible_scope_count": diagnostics["bootstrap_score_eligible_count"],
        },
        "current_recovery_used": False,
        "forbidden_recovery_fields": [],
        "issuer_dedup": {
            "enabled": True,
            "stats": {key: diagnostics[key] for key in (
                "raw_quality_candidate_count", "bootstrap_score_eligible_count", "unique_research_scope_count", "issuer_dedup_removed_rows"
            )},
        },
        "requested_size": MASTER_SIZE,
        "selected_scope_count": MASTER_SIZE,
        "cohort_scope_keys_sha256": semantic_hash([x["research_scope_key"] for x in scopes]),
        "deterministic_ordering_model": SELECTOR_ORDERING_MODEL,
        "cutoff_diagnostics": diagnostics,
        "regression_expectations": expectations,
        "regression_expectations_satisfied": all(str(key) in selected_keys for key in expectations),
    }
    certificate = {**certificate_body, "certificate_sha256": semantic_hash(certificate_body)}
    master_body = {**content, "master_content_sha256": master_content_sha256, "selector_certificate_sha256": certificate["certificate_sha256"]}
    master = {**master_body, "master_sha256": semantic_hash(master_body)}
    return {
        "schema_version": "2.0.0",
        "kind": BUNDLE_KIND,
        "candidate_source": dict(source),
        "master": master,
        "selector_certificate": certificate,
    }


def build_master_bundle(source_value: Mapping[str, Any], *, identity_map_value: Mapping[str, Any],
                        market_index_value: Mapping[str, Any], query_plan_value: Mapping[str, Any]) -> dict[str, Any]:
    source = validate_candidate_source_against_evidence(source_value, identity_map_value=identity_map_value, market_index_value=market_index_value, query_plan_value=query_plan_value)
    bundle = _derive_master_bundle(source)
    validate_master_bundle(bundle)
    return bundle


def build_master_bundle_from_evidence(identity_map_value: Mapping[str, Any], market_index_value: Mapping[str, Any],
                                      query_plan_value: Mapping[str, Any]) -> dict[str, Any]:
    source = build_candidate_source(identity_map_value, market_index_value, query_plan_value)
    return build_master_bundle(source, identity_map_value=identity_map_value, market_index_value=market_index_value, query_plan_value=query_plan_value)


def validate_master(master_value: Mapping[str, Any]) -> dict[str, Any]:
    v = dict(master_value)
    body = {k: x for k, x in v.items() if k != "master_sha256"}
    if v.get("schema_version") != "2.0.0" or v.get("kind") != MASTER_KIND or v.get("architecture_version") != ARCHITECTURE_VERSION:
        raise ValueError("invalid V4.2 MASTER CORE500")
    if v.get("master_sha256") != semantic_hash(body):
        raise ValueError("MASTER CORE500 self hash mismatch")
    content = {k: x for k, x in v.items() if k not in {"master_content_sha256", "selector_certificate_sha256", "master_sha256"}}
    if v.get("master_content_sha256") != semantic_hash(content):
        raise ValueError("MASTER CORE500 content hash mismatch")
    if int(v.get("requested_size") or -1) != MASTER_SIZE or int(v.get("selected_scope_count") or -1) != MASTER_SIZE:
        raise ValueError("production MASTER CORE500 must have requested_size and selected_scope_count equal to 500")
    scopes = v.get("scopes")
    if not isinstance(scopes, list) or len(scopes) != MASTER_SIZE:
        raise ValueError("production MASTER CORE500 must contain exactly 500 scopes")
    keys = [str(x.get("research_scope_key") or "") for x in scopes]
    if any(not key for key in keys) or len(set(keys)) != MASTER_SIZE:
        raise ValueError("MASTER CORE500 has duplicate or missing research scopes")
    if [int(x.get("rank") or -1) for x in scopes] != list(range(1, MASTER_SIZE + 1)):
        raise ValueError("MASTER CORE500 ranks are not deterministic 1..500")
    if v.get("current_recovery_used") is not False or v.get("core500_is_research_bootstrap_not_whitelist") is not True:
        raise ValueError("MASTER CORE500 semantic invariant failed")
    if v.get("source_derivation_model") != SOURCE_DERIVATION_MODEL or v.get("publisher_recompute_required") is not True:
        raise ValueError("MASTER CORE500 derivation contract invalid")
    canary = list(v.get("canary_scope_keys") or [])
    pilot = list(v.get("pilot_scope_keys") or [])
    if len(canary) != int(load_policy()["campaign"]["canary_scope_count"]) or len(pilot) != int(load_policy()["campaign"]["pilot_scope_count"]):
        raise ValueError("MASTER CORE500 phase scope counts invalid")
    if canary != keys[:len(canary)] or pilot != keys[:len(pilot)] or not set(canary).issubset(pilot):
        raise ValueError("MASTER CORE500 phase scopes are not deterministic subsets")
    for field in ("candidate_source_sha256", "identity_map_sha256", "market_index_sha256", "query_plan_sha256", "selector_config_sha256", "selector_certificate_sha256"):
        _require_hash(v.get(field), f"MASTER {field}")
    return v


def validate_selector_certificate(value: Mapping[str, Any], *, master: Mapping[str, Any]) -> dict[str, Any]:
    m = validate_master(master)
    v = dict(value)
    body = {k: x for k, x in v.items() if k != "certificate_sha256"}
    if v.get("schema_version") != "2.0.0" or v.get("kind") != CERTIFICATE_KIND or v.get("architecture_version") != ARCHITECTURE_VERSION:
        raise ValueError("invalid V4.2 selector certificate")
    if v.get("certificate_sha256") != semantic_hash(body):
        raise ValueError("selector certificate self hash mismatch")
    if v.get("certificate_sha256") != m["selector_certificate_sha256"] or v.get("master_content_sha256") != m["master_content_sha256"]:
        raise ValueError("selector certificate and MASTER CORE500 are not bound")
    if v.get("market_session_id") != m["market_session_id"]:
        raise ValueError("selector certificate market session mismatch")
    for field in ("candidate_source_sha256", "identity_map_sha256", "market_index_sha256", "query_plan_sha256", "selector_config_sha256"):
        if v.get(field) != m.get(field):
            raise ValueError("selector certificate selector provenance mismatch")
    if int(v.get("requested_size") or -1) != MASTER_SIZE or int(v.get("selected_scope_count") or -1) != MASTER_SIZE:
        raise ValueError("selector certificate does not prove exactly 500 scopes")
    if v.get("current_recovery_used") is not False or list(v.get("forbidden_recovery_fields") or []) != []:
        raise ValueError("selector certificate permits recovery/current-price contamination")
    if v.get("selector_model_version") != load_policy()["bootstrap"]["selection_model_version"]:
        raise ValueError("selector certificate model version mismatch")
    if v.get("deterministic_ordering_model") != SELECTOR_ORDERING_MODEL:
        raise ValueError("selector certificate ordering model mismatch")
    if v.get("source_derivation_model") != SOURCE_DERIVATION_MODEL or v.get("publisher_recompute_required") is not True:
        raise ValueError("selector certificate publisher recomputation contract invalid")
    if v.get("connector_trust_class") != provenance.CONNECTOR_TRUST_CLASS or v.get("external_cryptographic_signature_available") is not False:
        raise ValueError("selector certificate connector trust boundary mismatch")
    if not isinstance(v.get("candidate_source_identity"), Mapping) or not isinstance(v.get("lane_counts"), Mapping):
        raise ValueError("selector certificate provenance or lanes missing")
    fact_coverage = v.get("fact_coverage")
    if not isinstance(fact_coverage, Mapping) or float(fact_coverage.get("minimum_pct") or -1) != float(load_policy()["bootstrap"]["minimum_fact_coverage_pct"]) or int(fact_coverage.get("eligible_scope_count") or -1) < MASTER_SIZE:
        raise ValueError("selector certificate fact coverage evidence invalid")
    dedup = v.get("issuer_dedup")
    if not isinstance(dedup, Mapping) or dedup.get("enabled") is not True or not isinstance(dedup.get("stats"), Mapping):
        raise ValueError("selector certificate issuer dedup evidence missing")
    stats = dedup["stats"]
    stat_keys = ("raw_quality_candidate_count", "bootstrap_score_eligible_count", "unique_research_scope_count", "issuer_dedup_removed_rows")
    if any(key not in stats for key in stat_keys):
        raise ValueError("selector certificate issuer dedup stats incomplete")
    raw_count, eligible_count, unique_count, removed_count = (int(stats[key]) for key in stat_keys)
    if raw_count < eligible_count or eligible_count < unique_count or unique_count < MASTER_SIZE or removed_count != eligible_count - unique_count:
        raise ValueError("selector certificate issuer dedup stats inconsistent")
    cutoff = v.get("cutoff_diagnostics")
    if not isinstance(cutoff, Mapping) or int(cutoff.get("cutoff_rank") or -1) != MASTER_SIZE or any(key not in cutoff for key in ("cutoff_score", "next_excluded_score")):
        raise ValueError("selector certificate cutoff diagnostics invalid")
    if any(int(cutoff.get(key) if cutoff.get(key) is not None else -1) != int(stats[key]) for key in stat_keys):
        raise ValueError("selector certificate cutoff and dedup diagnostics disagree")
    expectations = list(v.get("regression_expectations") or [])
    if len(set(expectations)) < int(load_policy()["validation_framework"]["minimum_regression_expectations"]):
        raise ValueError("selector certificate regression expectations incomplete")
    if v.get("cohort_scope_keys_sha256") != semantic_hash([x["research_scope_key"] for x in m["scopes"]]):
        raise ValueError("selector certificate cohort scope hash mismatch")
    if v.get("regression_expectations_satisfied") is not True:
        raise ValueError("selector certificate regression expectations failed")
    return v


def validate_master_bundle(value: Mapping[str, Any]) -> dict[str, Any]:
    v = dict(value)
    if v.get("schema_version") != "2.0.0" or v.get("kind") != BUNDLE_KIND:
        raise ValueError("invalid V4.2 MASTER CORE500 bundle")
    source = validate_candidate_source(v.get("candidate_source") or {})
    master = validate_master(v.get("master") or {})
    certificate = validate_selector_certificate(v.get("selector_certificate") or {}, master=master)
    if source["source_sha256"] != master["candidate_source_sha256"] or source["source_sha256"] != certificate["candidate_source_sha256"]:
        raise ValueError("MASTER bundle candidate source hash mismatch")
    if source["market_session_id"] != master["market_session_id"] or source["market_session_id"] != certificate["market_session_id"]:
        raise ValueError("MASTER bundle candidate source market session mismatch")
    if source["identity_map_sha256"] != master["identity_map_sha256"] or source["market_index_sha256"] != master["market_index_sha256"] or source["query_plan_sha256"] != master["query_plan_sha256"]:
        raise ValueError("MASTER bundle identity/market/query provenance mismatch")
    if int(certificate["issuer_dedup"]["stats"]["raw_quality_candidate_count"]) != len(source["candidates"]):
        raise ValueError("MASTER bundle issuer dedup source count mismatch")
    expected = _derive_master_bundle(source)
    if master != expected["master"]:
        raise ValueError("MASTER CORE500 is not the canonical deterministic derivation of its candidate source")
    if certificate != expected["selector_certificate"]:
        raise ValueError("selector certificate is not the canonical deterministic derivation of its candidate source")
    return {**v, "candidate_source": source, "master": master, "selector_certificate": certificate}


def build_publish_request(identity_map_value: Mapping[str, Any], market_index_value: Mapping[str, Any],
                          query_plan_value: Mapping[str, Any]) -> dict[str, Any]:
    identity_map = provenance.validate_identity_map(identity_map_value)
    market_index = provenance.validate_market_index_against_identity_map(market_index_value, identity_map)
    query_plan = provenance.validate_query_plan(query_plan_value, market_index_value=market_index)
    bundle = build_master_bundle_from_evidence(identity_map, market_index, query_plan)
    body = {
        "schema_version": "2.0.0",
        "kind": BUILD_REQUEST_KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "publisher_must_recompute": True,
        "identity_map": identity_map,
        "market_index": market_index,
        "query_plan": query_plan,
        "expected_candidate_source_sha256": bundle["candidate_source"]["source_sha256"],
        "expected_master_sha256": bundle["master"]["master_sha256"],
        "expected_selector_certificate_sha256": bundle["selector_certificate"]["certificate_sha256"],
    }
    value = {**body, "request_sha256": semantic_hash(body)}
    validate_publish_request(value)
    return value


def derive_publish_request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = dict(value)
    body = {k: x for k, x in request.items() if k != "request_sha256"}
    if request.get("schema_version") != "2.0.0" or request.get("kind") != BUILD_REQUEST_KIND or request.get("architecture_version") != ARCHITECTURE_VERSION:
        raise ValueError("invalid V4.2 MASTER build request")
    if request.get("request_sha256") != semantic_hash(body) or request.get("publisher_must_recompute") is not True:
        raise ValueError("MASTER build request hash or recomputation contract invalid")
    identity_map = provenance.validate_identity_map(request.get("identity_map") or {})
    market_index = provenance.validate_market_index_against_identity_map(request.get("market_index") or {}, identity_map)
    query_plan = provenance.validate_query_plan(request.get("query_plan") or {}, market_index_value=market_index)
    bundle = build_master_bundle_from_evidence(identity_map, market_index, query_plan)
    if request.get("expected_candidate_source_sha256") != bundle["candidate_source"]["source_sha256"]:
        raise ValueError("MASTER build request candidate source expectation mismatch")
    if request.get("expected_master_sha256") != bundle["master"]["master_sha256"]:
        raise ValueError("MASTER build request expected MASTER mismatch")
    if request.get("expected_selector_certificate_sha256") != bundle["selector_certificate"]["certificate_sha256"]:
        raise ValueError("MASTER build request expected certificate mismatch")
    return bundle


def validate_publish_request(value: Mapping[str, Any]) -> dict[str, Any]:
    derive_publish_request(value)
    return dict(value)


def master_pointer(bundle_value: Mapping[str, Any], *, identity_path: str, market_index_path: str,
                   query_plan_path: str, source_path: str, master_path: str, certificate_path: str,
                   build_request_sha256: str, published_at: str, producer_release_sha256: str) -> dict[str, Any]:
    bundle = validate_master_bundle(bundle_value)
    master = bundle["master"]
    body = {
        "schema_version": "2.0.0",
        "kind": POINTER_KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "identity_path": str(identity_path),
        "market_index_path": str(market_index_path),
        "query_plan_path": str(query_plan_path),
        "source_path": str(source_path),
        "master_path": str(master_path),
        "certificate_path": str(certificate_path),
        "source_sha256": bundle["candidate_source"]["source_sha256"],
        "master_sha256": master["master_sha256"],
        "master_content_sha256": master["master_content_sha256"],
        "selector_certificate_sha256": master["selector_certificate_sha256"],
        "identity_map_sha256": master["identity_map_sha256"],
        "market_index_sha256": master["market_index_sha256"],
        "query_plan_sha256": master["query_plan_sha256"],
        "build_request_sha256": _require_hash(build_request_sha256, "MASTER build request hash"),
        "market_session_id": master["market_session_id"],
        "selected_scope_count": MASTER_SIZE,
        "published_at": str(published_at),
        "producer_release_sha256": _require_hash(producer_release_sha256, "producer release hash"),
    }
    value = {**body, "pointer_sha256": semantic_hash(body)}
    return validate_master_pointer(value, bundle=bundle)


def validate_master_pointer(value: Mapping[str, Any], *, bundle: Mapping[str, Any] | None = None) -> dict[str, Any]:
    validate_contract("v42-master-pointer", value)
    pointer = dict(value)
    body = {k: x for k, x in pointer.items() if k != "pointer_sha256"}
    if pointer.get("pointer_sha256") != semantic_hash(body):
        raise ValueError("V4.2 MASTER pointer self hash mismatch")
    prefixes = {
        "identity_path": "data/v42/identity/maps/",
        "market_index_path": "data/v42/identity/market-indexes/",
        "query_plan_path": "data/v42/query-plans/",
        "source_path": "data/v42/master-core500/sources/",
        "master_path": "data/v42/master-core500/masters/",
        "certificate_path": "data/v42/master-core500/certificates/",
    }
    for field, prefix in prefixes.items():
        if not str(pointer.get(field) or "").startswith(prefix):
            raise ValueError(f"V4.2 MASTER pointer path invalid: {field}")
    if int(pointer.get("selected_scope_count") or -1) != MASTER_SIZE:
        raise ValueError("V4.2 MASTER pointer count invalid")
    for field in (
        "source_sha256", "master_sha256", "master_content_sha256", "selector_certificate_sha256",
        "identity_map_sha256", "market_index_sha256", "query_plan_sha256", "build_request_sha256",
        "producer_release_sha256",
    ):
        _require_hash(pointer.get(field), f"MASTER pointer {field}")
    exact_paths = {
        "identity_path": f"data/v42/identity/maps/{pointer['identity_map_sha256']}.json",
        "market_index_path": f"data/v42/identity/market-indexes/{pointer['market_index_sha256']}.json",
        "query_plan_path": f"data/v42/query-plans/{pointer['query_plan_sha256']}.json",
        "source_path": f"data/v42/master-core500/sources/{pointer['source_sha256']}.json",
        "master_path": f"data/v42/master-core500/masters/{pointer['master_sha256']}/master.json",
        "certificate_path": f"data/v42/master-core500/certificates/{pointer['selector_certificate_sha256']}.json",
    }
    for field, expected_path in exact_paths.items():
        if pointer.get(field) != expected_path:
            raise ValueError(f"V4.2 MASTER pointer path/hash cross-binding invalid: {field}")
    if bundle is not None:
        canonical = validate_master_bundle(bundle)
        master = canonical["master"]
        expected = {
            "source_sha256": canonical["candidate_source"]["source_sha256"],
            "master_sha256": master["master_sha256"],
            "master_content_sha256": master["master_content_sha256"],
            "selector_certificate_sha256": master["selector_certificate_sha256"],
            "identity_map_sha256": master["identity_map_sha256"],
            "market_index_sha256": master["market_index_sha256"],
            "query_plan_sha256": master["query_plan_sha256"],
            "market_session_id": master["market_session_id"],
        }
        for field, expected_value in expected.items():
            if pointer.get(field) != expected_value:
                raise ValueError(f"V4.2 MASTER pointer artifact mismatch: {field}")
    return pointer
