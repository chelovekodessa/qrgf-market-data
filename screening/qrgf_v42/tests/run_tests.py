#!/usr/bin/env python3
"""QRGF V4.2 release tests: provenance, deterministic MASTER and durable recovery."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import batch, bootstrap, bootstrap_state, campaign, decision, eligibility, evidence, factpack, integrity, market_view, migration, passport, policy, provenance, registry, registry_store, research, selection
from common import semantic_hash, write_json

NOW = dt.datetime(2026, 8, 18, 9, 0, tzinfo=dt.timezone.utc)
NOW_TEXT = NOW.isoformat().replace("+00:00", "Z")
EARLIER = (NOW - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
LATER = (NOW + dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z")
RESULTS: list[dict[str, Any]] = []


def record(name: str, condition: bool, detail: Any = None) -> None:
    RESULTS.append({"name": name, "pass": bool(condition), "detail": None if condition else detail})


def rejects(fn: Callable[[], Any], contains: str | None = None) -> bool:
    try:
        fn()
    except Exception as exc:
        return contains is None or contains.lower() in str(exc).lower()
    return False


def ev(field: str, value: Any, source_type: str = "official_filing", *, ident: str | None = None) -> dict[str, Any]:
    source = {"source_id": f"fixture-{source_type}", "source_type": source_type}
    if source_type == "licensed_market_data":
        source["provider"] = "MetricDuck"
    if source_type == "official_filing":
        source.update({
            "accession_or_edgar_handle": "0000000000-26-000001",
            "filing_lineage": "fixture-10q",
            "period": "2026-Q2",
        })
    row: dict[str, Any] = {
        "evidence_id": ident or f"e-{semantic_hash([field, value, source_type])[:20]}",
        "field": field,
        "value": value,
        "source": source,
        "as_of": EARLIER,
        "retrieved_at": EARLIER,
        "quality_status": "verified",
    }
    if field.startswith("facts.") and isinstance(value, (int, float)) and not isinstance(value, bool):
        row["period"] = "2026-Q2"
        row["unit"] = "reported_unit"
        if field.endswith("_score"):
            row["rubric"] = {
                "method_version": "fixture-rubric-v2",
                "criteria": [{"criterion": "durability", "score": value}, {"criterion": "consistency", "score": value}],
                "rationale": f"Fixture rationale for {field}",
                "contrary_evidence": [],
                "uncertainty": "low",
            }
    if field.startswith("clearances.") and value == "clear":
        row["clearance_scope"] = {
            "documents": ["fixture-filing"],
            "period_start": "2025-08-01",
            "period_end": "2026-08-18",
        }
    return row


def l3_payload(lane: str, ticker: str = "GROW", *, security_type: str = "common_equity", triggered: str | None = None) -> dict[str, Any]:
    common = {"competitive_position_score": 88, "moat_quality_score": 84, "management_quality_score": 82}
    if lane == "recognized_growth":
        facts = {**common, "business_reality_score": 95, "revenue_growth_pct": 35, "revenue_cagr_3y_pct": 30, "operating_margin_pct": -5, "operating_margin_change_pp_yoy": 8, "fcf_margin_pct": -2, "fcf_margin_change_pp_yoy": 6, "cash": 2e9, "debt": 0.2e9, "net_debt_to_ebitda": 0.1, "cash_runway_months": 36, "dilution_pct_yoy": 3, "guidance_trend": "raised", "sales_efficiency_score": 78}
        clearances = ["bankruptcy_status", "going_concern_status", "accounting_status", "dilution_financing_status", "balance_sheet_viability_status", "binary_business_risk_status"]
        sector = "Technology"
    elif lane == "established_quality":
        facts = {**common, "net_debt_to_ebitda": 1.2, "cash": 1e9, "debt": 0.5e9, "operating_margin_pct": 18, "net_income_positive": True, "fcf_margin_pct": 16, "fcf_positive": True, "revenue_growth_pct": 10, "revenue_cagr_3y_pct": 9, "earnings_growth_pct": 12, "operating_margin_change_pp_yoy": 1.5, "guidance_trend": "maintained", "dilution_pct_yoy": 0, "capital_allocation_score": 85}
        clearances = ["bankruptcy_status", "going_concern_status", "accounting_status", "dilution_financing_status", "balance_sheet_viability_status", "binary_business_risk_status"]
        sector = "Industrials"
    elif lane == "cyclical":
        facts = {**common, "net_debt_to_ebitda": 1.5, "cash": 1e9, "debt": 0.8e9, "normalized_cycle_quality_score": 86, "normalized_fcf_quality_score": 82, "fcf_margin_pct": 12, "dilution_pct_yoy": 1, "capital_allocation_score": 84}
        clearances = ["bankruptcy_status", "going_concern_status", "accounting_status", "dilution_financing_status", "balance_sheet_viability_status", "binary_business_risk_status"]
        sector = "Materials"
    elif lane == "bank":
        facts = {**common, "franchise_quality_score": 85, "capital_quality_score": 88, "asset_quality_score": 86, "bank_profitability_score": 82, "funding_stability_score": 84}
        clearances = ["bankruptcy_status", "going_concern_status", "accounting_status", "capital_viability_status", "funding_viability_status", "binary_business_risk_status"]
        sector = "Banks"
    elif lane == "etf":
        ticker = "SPY"
        security_type = "etf"
        facts = {"holdings_quality_score": 88, "sector_fundamental_quality_score": 82, "breadth_quality_score": 80, "fund_structure_quality_score": 90, "largest_holding_weight_pct": 8, "top10_weight_pct": 42, "fund_aum": 5e9}
        clearances = ["fund_viability_status", "delisting_liquidation_status", "leverage_structure_status", "underlying_binary_concentration_status"]
        sector = "Broad US market"
    else:
        raise ValueError(lane)
    clearance_values = {name: ("triggered" if name == triggered else "clear") for name in clearances}
    if security_type == "adr":
        clearance_values.update({"issuer_reporting_status": "clear", "listing_status": "clear"})
    evidence_rows = [ev(f"facts.{key}", value) for key, value in facts.items()]
    evidence_rows.extend(ev(f"clearances.{key}", value) for key, value in clearance_values.items())
    return {
        "ticker": ticker,
        "contract_id": f"US:{ticker}",
        "security_type": security_type,
        "instrument_status": "eligible",
        "sector": sector,
        "facts": facts,
        "clearances": clearance_values,
        "evidence": evidence_rows,
        "research_cutoff_at": NOW_TEXT,
        "collection_status": "ready",
    }


def l4_payload(l3: Mapping[str, Any]) -> dict[str, Any]:
    events = {
        "cause_classification": "temporary_supported",
        "next_review_date": "2026-08-24",
        "last_earnings_date": "2026-08-01T12:00:00Z",
        "guidance_status": "maintained",
        "next_earnings_date": "2026-11-01T12:00:00Z",
        "revenue_surprise_pct": 3,
        "eps_surprise_pct": 4,
        "margin_change_bps": 100,
        "guidance_midpoint_change_pct": 2,
        "estimate_revision_90d_pct": 3,
        "catalyst_confirmed": True,
    }
    actions = [{"bank": f"Bank{i}", "analyst": f"A{i}", "rating": "Buy", "new_target": 120 + i, "currency": "USD", "date": "2026-08-05T12:00:00Z", "source": "fixture-news"} for i in range(5)]
    evidence_rows = [ev(f"events.{key}", value, "investor_relations") for key, value in events.items()]
    evidence_rows.append(ev("analyst_actions", actions, "reputable_news"))
    return {"ticker": l3["ticker"], "contract_id": l3["contract_id"], "security_type": l3.get("security_type", "common_equity"), "events": events, "analyst_actions": actions, "l3": dict(l3), "evidence": evidence_rows, "analysis_time": NOW_TEXT, "research_cutoff_at": NOW_TEXT}


def execution_payload(ticker: str = "GROW") -> dict[str, Any]:
    fresh = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "provider": "IBKR",
        "ticker": ticker,
        "requested_status": "open_now",
        "contract": {"ticker": ticker, "contract_id": f"US:{ticker}", "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD", "security_type": "common_equity", "identity_status": "verified_ibkr", "us_listing_verified": True},
        "quote": {"last": 100.0, "bid": 99.9, "ask": 100.1, "quote_timestamp": fresh, "bid_ask_timestamp": fresh, "volume": 1e6, "avg_90d_usd_volume": 50e6, "historical_vol": 0.35},
        "history": {"daily_period": "ONE_YEAR", "daily_step": "ONE_DAY", "weekly_period": "FIVE_YEARS", "weekly_step": "ONE_WEEK", "daily_bars": 253, "weekly_bars": 160},
        "account": {"summary": {"ok": True}, "balances": {"ok": True}, "positions": {"ok": True}, "allocation": {"allocation_type": "ALL"}, "fetched_at": fresh},
        "explicit_order_request": False,
    }


def bootstrap_facts(boost: float = 0.0) -> dict[str, Any]:
    return {"roic": 0.18 + boost, "operating_margin_pct": 0.22 + boost, "fcf_margin_pct": 0.20 + boost, "revenue_cagr_3y_pct": 0.15 + boost, "net_debt_to_ebitda": 0.5, "cash": 20e9, "debt": 5e9, "dilution_pct_yoy": 0.01}


def metricduck_fixture_row(member: Mapping[str, Any], *, cap: float | None = None, sector_code: str = "TECH") -> dict[str, Any]:
    connector_cap = float(cap if cap is not None else member.get("connector_market_cap") or member.get("market_cap"))
    return {
        "ticker": member["ticker"],
        "contract_id": member.get("contract_id"),
        "company": member["company"],
        "sector_code": sector_code,
        "connector_market_cap": connector_cap,
        "connector_metrics": {
            "roic@ttm": 0.18,
            "oper_margin@ttm": 0.22,
            "roe@ttm": 0.20,
            "roa@ttm": 0.10,
            "revenues@ttm": 100e9,
            "revenues@ttm.cagr3": 0.15,
            "fcf@ttm": 20e9,
        },
    }


def response_handle(spec: Mapping[str, Any], label: str = "fixture") -> str:
    normalized = provenance.normalize_query_spec(spec)
    return f"connector-attested://MetricDuck.screen_companies/{normalized['query_sha256']}/{label}"


def provenance_fixture(count: int = 520, session: str = "2026-08-18") -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    # Model the actual production L1/V3 contract: membership/history exists but
    # sector, industry, market_cap and CIK are absent from Radar rows.
    rows: list[dict[str, Any]] = []
    connector_rows: list[dict[str, Any]] = []
    identity_entries: list[dict[str, Any]] = []
    for index in range(count):
        ticker = f"Q{index:03d}"
        cik = f"CIK:{index + 1000:010d}"
        if index == 0:
            ticker, cik = "GOOG", "CIK:0001652044"
        elif index == 1:
            ticker, cik = "GOOGL", "CIK:0001652044"
        contract = f"US:{ticker}"
        connector_cap = float((count - index + 500) * 1_000_000_000)
        row = {
            "ticker": ticker,
            "contract_id": contract,
            "company": f"Company {ticker}",
            "security_type": "common_equity",
            "instrument_status": "eligible",
            "exchange": "NASDAQ",
            "avg_dollar_volume": 1_000_000_000,
        }
        rows.append(row)
        connector_rows.append({**row, "connector_market_cap": connector_cap, "connector_sector_code": ("TRANSPORT" if index == 2 else "OTHER" if index == 3 else "TECH")})
        identity_entries.append({
            "ticker": ticker,
            "contract_id": contract,
            "security_type": "common_equity",
            "issuer_id": cik,
            "resolution_status": "official",
            "share_class_group": cik,
            "security_class": "common",
            "source_record_sha256": semantic_hash({"ticker": ticker, "cik": cik}),
        })
    identity_map = provenance.build_identity_map(
        source_kind=provenance.IDENTITY_SOURCE_KIND,
        source_snapshot_sha256="1" * 64,
        entries=identity_entries,
    )
    market_index = provenance.build_market_index(
        rows,
        market_session_id=session,
        source_snapshot_id="fixture-radar",
        source_manifest_sha256="2" * 64,
        identity_map_value=identity_map,
    )

    sorted_rows = sorted(connector_rows, key=lambda item: float(item["connector_market_cap"]))
    boundaries: list[tuple[float, float | None]] = []
    start = 0.0
    for offset in range(50, len(sorted_rows), 50):
        high = float(sorted_rows[offset]["connector_market_cap"])
        boundaries.append((start, high))
        start = high
    boundaries.append((start, None))

    leaves: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []

    def add_partitioned_scope(purpose: str, lane: str | None, sector_code: str | None, members: list[dict[str, Any]]) -> None:
        for part, (low, high) in enumerate(boundaries):
            spec = {
                "purpose": purpose,
                "lane": lane,
                "sector_code": sector_code,
                "market_cap_min": low,
                "market_cap_max": high,
                "limit": 50,
            }
            partition = [row for row in members if float(row["connector_market_cap"]) >= low and (high is None or float(row["connector_market_cap"]) < high)]
            result_rows = [metricduck_fixture_row(member, cap=member["connector_market_cap"], sector_code=str(member.get("connector_sector_code") or "TECH")) for member in partition]
            receipt = provenance.build_query_receipt(
                spec,
                result_rows=result_rows,
                matched_count=len(result_rows),
                retrieved_at=NOW_TEXT,
                response_handle=response_handle(spec, f"{purpose}/{lane or 'all'}/{sector_code or 'all'}/{part}"),
            )
            leaves.append(spec)
            receipts.append(receipt)

    # Complete sectorless classification bridge and two global quality lanes.
    add_partitioned_scope(provenance.QUERY_PURPOSE_CLASSIFICATION, None, None, connector_rows)
    add_partitioned_scope(provenance.QUERY_PURPOSE_QUALITY, "established_quality", None, connector_rows)
    add_partitioned_scope(provenance.QUERY_PURPOSE_QUALITY, "recognized_growth", None, connector_rows)

    # Required sector-native lanes are present and complete even when no fixture
    # companies match those sectors.
    for lane, sector_code in (("bank", "FIN"), ("cyclical", "ENERGY"), ("cyclical", "MAT")):
        spec = {
            "purpose": provenance.QUERY_PURPOSE_QUALITY,
            "lane": lane,
            "sector_code": sector_code,
            "market_cap_min": 0.0,
            "market_cap_max": None,
            "limit": 50,
        }
        receipt = provenance.build_query_receipt(
            spec,
            result_rows=[],
            matched_count=0,
            retrieved_at=NOW_TEXT,
            response_handle=response_handle(spec, f"quality/{lane}/{sector_code}"),
        )
        leaves.append(spec)
        receipts.append(receipt)

    unique_expectations: list[str] = []
    for row in market_index["rows"]:
        key = row.get("research_scope_key")
        if key and key not in unique_expectations:
            unique_expectations.append(str(key))
        if len(unique_expectations) == 5:
            break
    query_plan = provenance.build_query_plan(
        market_index,
        leaves=leaves,
        receipts=receipts,
        regression_expectations=unique_expectations,
    )
    source = bootstrap.build_candidate_source(identity_map, market_index, query_plan)
    bundle = bootstrap.build_master_bundle_from_evidence(identity_map, market_index, query_plan)
    return identity_map, market_index, query_plan, bundle


def durable_record(status: str = "pass", session: str = "2026-08-18", token: str = "a") -> dict[str, Any]:
    return {"quality_status": status, "event_scan_through": session, "freshness_status": "fresh", "durable_readback_verified": True, "policy_compatible": True, "overlay_compatible": True, "receipt_sha256": token * 64, "passport_hash": token * 64, "entry_sha256": token * 64, "next_review_date": "2026-08-25" if status == "insufficient_data" else None}


def reuse_results(snapshot: Mapping[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: {"passport_hash": snapshot["records"][key]["passport_hash"], "entry_sha256": snapshot["records"][key]["entry_sha256"], "receipt_sha256": snapshot["records"][key]["receipt_sha256"], "reused_without_deep_research": True, "policy_compatible": True, "overlay_compatible": True} for key in keys}


def _rehash_bundle_with_scope_mutation(bundle: Mapping[str, Any], mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    changed = copy.deepcopy(bundle)
    master = changed["master"]
    mutate(master)
    certificate = changed["selector_certificate"]
    content = {key: value for key, value in master.items() if key not in {"master_content_sha256", "selector_certificate_sha256", "master_sha256"}}
    master_content = semantic_hash(content)
    certificate["master_content_sha256"] = master_content
    certificate["cohort_scope_keys_sha256"] = semantic_hash([item["research_scope_key"] for item in master["scopes"]])
    cert_body = {key: value for key, value in certificate.items() if key != "certificate_sha256"}
    certificate["certificate_sha256"] = semantic_hash(cert_body)
    master["master_content_sha256"] = master_content
    master["selector_certificate_sha256"] = certificate["certificate_sha256"]
    master_body = {key: value for key, value in master.items() if key != "master_sha256"}
    master["master_sha256"] = semantic_hash(master_body)
    return changed


def main() -> int:
    # Policy and connector boundaries.
    record("V4.2 policy validates", policy.validate()["architecture_version"] == "4.2.0")
    policy_value = json.loads((ROOT / "config/policy.json").read_text())
    record("V4.2 deployment manifests are mandatory and pinned", policy_value["architecture"]["deployment_release_manifest_required"] is True and policy_value["architecture"]["remote_release_must_match_pinned_hash"] is True)
    record("Insufficient data requires a durable next-review date", policy_value["quality_registry"]["insufficient_data_requires_next_review_date"] is True)
    record("Pilot reconstructs blocked states without treating them as reused quality", policy_value["campaign"]["pilot_blocked_state_reconstructed_not_quality_reused"] is True)
    record("Deployment overlay hash validation is mandatory", policy_value["validation_framework"]["deployment_overlay_hash_check_required"] is True)
    connectors = json.loads((ROOT / "config/connectors.json").read_text())
    record("Registry and MASTER share the V4.2 single-writer release", connectors["quality_registry_v4"]["producer_release_path"] == connectors["master_core500_v42"]["producer_release_path"])
    record("V4.1 production connectors are absent", "master_core500_v41" not in connectors and "market_view_v41" not in connectors)
    record("V4.2 MASTER publisher must recompute", connectors["master_core500_v42"]["publisher_recomputes_master_from_evidence"] is True)
    record("MetricDuck screen trust is connector-attested, not falsely signed", connectors["primary_evidence"]["metricduck"]["cross_company_screen_trust_class"] == "connector_attested" and connectors["primary_evidence"]["metricduck"]["external_cryptographic_signature_available"] is False)
    bank_spec = provenance.normalize_query_spec({"purpose":provenance.QUERY_PURPOSE_QUALITY,"lane":"bank","sector_code":"FIN","market_cap_min":0,"market_cap_max":None,"limit":50})
    bank_args = provenance.connector_query_args(bank_spec)
    record("Bank discovery uses the native MetricDuck classification tag", bank_args.get("required_tags") == ["financial_services_traditional"] and bank_args.get("sectors") == ["FIN"] and bank_args["filters"][0] == {"metric_id":"roa","operator":"gte","value":0.005,"period_type":"ttm"})
    classification_spec = provenance.normalize_query_spec({"purpose":provenance.QUERY_PURPOSE_CLASSIFICATION,"lane":None,"sector_code":None,"market_cap_min":0,"market_cap_max":None,"limit":50})
    classification_args = provenance.connector_query_args(classification_spec)
    record("Classification discovery is sectorless and independent of Radar", "sectors" not in classification_args and classification_args["filters"] == [{"metric_id":"market_cap","operator":"gte","value":0.0,"period_type":"ttm"}])
    record("MetricDuck request-sector enum remains limited to connector query inputs", rejects(lambda: provenance._normal_query_sector_code("TRANSPORT"), "unsupported query sector"))
    record("MetricDuck returned taxonomy is open and preserves observed provider codes", provenance._normal_result_sector_code("TRANSPORT") == "TRANSPORT" and provenance._normal_result_sector_code("OTHER") == "OTHER")
    taxonomy_receipt = provenance.build_query_receipt(
        classification_spec,
        result_rows=[
            metricduck_fixture_row({"ticker":"TXA","company":"Transport Example"}, cap=2_000_000_000, sector_code="TRANSPORT"),
            metricduck_fixture_row({"ticker":"OTH","company":"Other Example"}, cap=3_000_000_000, sector_code="OTHER"),
        ],
        matched_count=2, retrieved_at=NOW_TEXT, response_handle=response_handle(classification_spec, "classification/open-taxonomy"),
    )
    record("Sectorless classification receipt accepts provider-only sector codes", provenance.validate_query_receipt(taxonomy_receipt)["returned_count"] == 2)
    record("Sector-filtered query still rejects a mismatching returned sector", rejects(lambda: provenance.build_query_receipt(
        bank_spec, result_rows=[metricduck_fixture_row({"ticker":"BADSEC","company":"Bad Sector"}, cap=2_000_000_000, sector_code="OTHER")],
        matched_count=1, retrieved_at=NOW_TEXT, response_handle=response_handle(bank_spec, "bank/mismatch")
    ), "differs from connector query sector"))
    rounded_boundary_spec = provenance.normalize_query_spec({"purpose":provenance.QUERY_PURPOSE_CLASSIFICATION,"lane":None,"sector_code":None,"market_cap_min":0,"market_cap_max":297_310_000_000,"limit":50})
    rounded_boundary_row = metricduck_fixture_row({"ticker":"NFLX","company":"NETFLIX INC"}, cap=297_310_000_000, sector_code="CONS_DISC")
    rounded_boundary_receipt = provenance.build_query_receipt(
        rounded_boundary_spec, result_rows=[rounded_boundary_row], matched_count=1, retrieved_at=NOW_TEXT,
        response_handle=response_handle(rounded_boundary_spec, "rounded-boundary")
    )
    record("Rounded screen projection at strict market-cap boundary is accepted from connector response membership", provenance.validate_query_receipt(rounded_boundary_receipt)["returned_count"] == 1)
    record("Strict market-cap boundary is preserved in connector query args without epsilon", provenance.connector_query_args(rounded_boundary_spec)["filters"][-1] == {"metric_id":"market_cap","operator":"lt","value":297_310_000_000.0,"period_type":"ttm"})
    bad_handle = response_handle(rounded_boundary_spec, "rounded-boundary").replace(rounded_boundary_spec["query_sha256"], "f" * 64)
    record("MetricDuck receipt rejects a response handle bound to a different query", rejects(lambda: provenance.build_query_receipt(rounded_boundary_spec, result_rows=[rounded_boundary_row], matched_count=1, retrieved_at=NOW_TEXT, response_handle=bad_handle), "query binding mismatch"))
    record("MetricDuck connector contract version remains aligned with immutable V4.2 bootstrap receipts", json.loads((ROOT / "config/policy.json").read_text())["bootstrap"]["metricduck_query_plan"]["connector_contract_version"] == connectors["primary_evidence"]["metricduck"]["cross_company_screen_contract_version"] == "2026-08-20")
    record("Producer release hashes are nonzero", connectors["master_core500_v42"]["expected_producer_release_sha256"] != "0" * 64 and connectors["market_view_v42"]["expected_producer_release_sha256"] != "0" * 64)

    # Structural scoring parity with the frozen analytical model.
    golden = json.loads((ROOT / "tests/golden/l3-v430-baseline.json").read_text())
    for lane in ("established_quality", "recognized_growth", "cyclical", "bank", "etf"):
        result = research.evaluate_l3(l3_payload(lane, ticker=f"T{lane[:2].upper()}"))
        expected = golden[lane]
        record(f"V4.2 preserves frozen L3 {lane} score/status", result["l3_score"] == expected["l3_score"] and result["l3_status"] == expected["l3_status"] and result["l3_coverage_pct"] == expected["l3_coverage_pct"], {"result": result, "golden": expected})
    first = passport.evaluate(l3_payload("recognized_growth", "FAST"))
    changed = l3_payload("recognized_growth", "FAST2")
    changed["facts"].update({"guidance_trend": "withdrawn", "revenue_growth_pct": -30, "operating_margin_change_pp_yoy": -15, "fcf_margin_change_pp_yoy": -12})
    for item in changed["evidence"]:
        if item["field"] == "facts.guidance_trend":
            item["value"] = "withdrawn"
        elif item["field"] == "facts.revenue_growth_pct":
            item["value"] = -30
        elif item["field"] == "facts.operating_margin_change_pp_yoy":
            item["value"] = -15
        elif item["field"] == "facts.fcf_margin_change_pp_yoy":
            item["value"] = -12
    second = passport.evaluate(changed)
    record("Structural Quality remains independent of fast momentum", first["quality_score"] == second["quality_score"])
    record("Going-concern hard veto is preserved", passport.evaluate(l3_payload("established_quality", "BAD", triggered="going_concern_status"))["quality_status"] == "rejected")
    record("Subjective assessments are explicitly model judgments", first["assessment_is_model_judgment"] is True)
    record("Subjective score rationale survives evaluation", bool(first["assessment_rationale"]) and first["auditability_class"] == "v42_rich")
    record("Assessment uncertainty is durable", first["assessment_uncertainty"] == "low")
    missing_rationale = l3_payload("established_quality", "NORAT")
    next(item for item in missing_rationale["evidence"] if item["field"] == "facts.moat_quality_score")["rubric"].pop("rationale")
    missing_result = passport.evaluate(missing_rationale)
    record("Missing subjective rationale fails evidence closed", missing_result["quality_status"] == "recheck" and missing_result["auditability_class"] == "v42_incomplete", missing_result)
    exhausted_without_review = l3_payload("established_quality", "NODATE")
    exhausted_without_review["collection_status"] = "exhausted"
    exhausted_without_review["evidence"] = []
    no_review_result = passport.evaluate(exhausted_without_review)
    record("Exhausted evidence without next review date stays nonterminal", no_review_result["quality_status"] == "recheck" and no_review_result["quality_status_reason"] == "next_review_date_missing_for_insufficient_data", no_review_result)
    exhausted_with_review = copy.deepcopy(exhausted_without_review)
    exhausted_with_review["ticker"] = "HASDATE"
    exhausted_with_review["contract_id"] = "US:HASDATE"
    exhausted_with_review["next_review_date"] = "2026-08-25"
    blocked_result = passport.evaluate(exhausted_with_review)
    record("Exhausted evidence becomes insufficient_data only with next review date", blocked_result["quality_status"] == "insufficient_data" and blocked_result["next_review_date"] == "2026-08-25", blocked_result)

    # Provenance fixture and deterministic MASTER.
    identity_map, market_index, query_plan, bundle = provenance_fixture()
    source = bundle["candidate_source"]
    master = bundle["master"]
    record("Official identity map is self-hashed", provenance.validate_identity_map(identity_map)["identity_map_sha256"] == identity_map["identity_map_sha256"])
    record("Market index is bound to the identity map", market_index["identity_map_sha256"] == identity_map["identity_map_sha256"])
    record("Market index requires publisher recomputation", market_index["publisher_recompute_required"] is True)
    record("Full Radar without sector remains valid UNKNOWN", market_index["known_market_sector_count"] == 0 and all(row["sector"] is None and row["market_sector_status"] == "unknown" for row in market_index["rows"]))
    record("Full Radar without market cap remains valid UNKNOWN", market_index["known_market_cap_count"] == 0 and all(row["market_cap"] is None and row["market_cap_status"] == "unknown" for row in market_index["rows"]))
    record("Radar CIK is not required because official identity is built separately", all("cik" not in row and "issuer_cik" not in row for row in market_index["rows"]) and all(row["issuer_id"].startswith("CIK:") for row in market_index["rows"] if row["master_eligible"]))
    record("All MASTER-eligible operating companies use official CIK identities", all(row["issuer_id"].startswith("CIK:") for row in market_index["rows"] if row["master_eligible"]))
    record("MetricDuck plan covers classification plus every native quality scope", query_plan["partition_coverage_complete"] is True and query_plan["classification_catalog_complete"] is True and query_plan["radar_classification_required"] is False and query_plan["required_lanes"] == sorted(json.loads((ROOT / "config/policy.json").read_text())["bootstrap"]["metricduck_query_plan"]["screen_lanes"]))
    record("Every MetricDuck leaf is unsaturated", all(receipt["complete"] is True and receipt["requires_split"] is False for receipt in query_plan["receipts"]))
    record("MetricDuck receipts preserve connector response provenance", all(receipt["response_handle"] for receipt in query_plan["receipts"]))
    sample_native_row = next(row for receipt in query_plan["receipts"] for row in receipt["rows"] if row["ticker"] == "GOOG")
    record("Native MetricDuck values derive only semantically exact canonical facts", abs(sample_native_row["facts"]["fcf_margin_pct"] - 0.20) < 1e-12 and abs(sample_native_row["facts"]["revenue_cagr_3y_pct"] - 0.15) < 1e-12)
    record("Candidate source is derived from market plus query plan", source["market_index_sha256"] == market_index["market_index_sha256"] and source["query_plan_sha256"] == query_plan["query_plan_sha256"])
    record("Candidate source contains at least 500 market-bound rows", source["quality_candidate_union_size"] >= 500 and all(row["market_membership_bound"] is True for row in source["candidates"]))
    record("Operating candidates use connector-attested classification, never Radar classification", source["radar_classification_used"] is False and all(row["classification_bound"] is True and provenance._normal_result_sector_code(row["sector_code"]) == row["sector_code"] for row in source["candidates"] if row["security_type"] != "etf"))
    provider_codes = {str(row.get("sector_code") or "") for row in source["candidates"] if row["security_type"] != "etf"}
    record("Provider-returned TRANSPORT and OTHER survive classification without remapping", {"TRANSPORT", "OTHER"}.issubset(provider_codes), sorted(provider_codes))
    record("MetricDuck transport limit 50 is not a market cutoff", source["quality_candidate_union_size"] > 50 and all(int(leaf["limit"]) <= 50 for leaf in query_plan["leaves"]) and len([leaf for leaf in query_plan["leaves"] if leaf["purpose"] == provenance.QUERY_PURPOSE_CLASSIFICATION]) > 1)
    record("Unsupported structural facts stay UNKNOWN", all("net_debt_to_ebitda" not in row["facts"] and "cet1_ratio_pct" not in row["facts"] for row in source["candidates"] if row["security_type"] != "etf"))
    record("MASTER CORE500 is exactly 500", len(master["scopes"]) == 500 and master["selected_scope_count"] == 500)
    record("MASTER is canonical deterministic derivation", bootstrap.validate_master_bundle(bundle)["master"]["master_sha256"] == master["master_sha256"])
    build_request = bootstrap.build_publish_request(identity_map, market_index, query_plan)
    record("MASTER publisher recomputation request is self-contained", bootstrap.derive_publish_request(build_request)["master"]["master_sha256"] == master["master_sha256"])
    master_pointer = bootstrap.master_pointer(
        bundle,
        identity_path=f"data/v42/identity/maps/{identity_map['identity_map_sha256']}.json",
        market_index_path=f"data/v42/identity/market-indexes/{market_index['market_index_sha256']}.json",
        query_plan_path=f"data/v42/query-plans/{query_plan['query_plan_sha256']}.json",
        source_path=f"data/v42/master-core500/sources/{source['source_sha256']}.json",
        master_path=f"data/v42/master-core500/masters/{master['master_sha256']}/master.json",
        certificate_path=f"data/v42/master-core500/certificates/{bundle['selector_certificate']['certificate_sha256']}.json",
        build_request_sha256=build_request["request_sha256"], published_at=NOW_TEXT, producer_release_sha256="a" * 64,
    )
    record("MASTER pointer binds every evidence artifact", bootstrap.validate_master_pointer(master_pointer, bundle=bundle)["query_plan_sha256"] == query_plan["query_plan_sha256"])
    # Durable bootstrap coordination survives chat/session boundaries and never
    # authorizes daily broad before the real MASTER exists.
    identity_checkpoint = bootstrap_state.build({
        "phase": "IDENTITY",
        "market_session_id": market_index["market_session_id"],
        "source_manifest_sha256": market_index["source_manifest_sha256"],
        "created_at": NOW_TEXT,
        "artifacts": {"identity_map": {"path": f"data/v42/master-core500/bootstrap/artifacts/identity-{identity_map['identity_map_sha256']}.json", "sha256": identity_map["identity_map_sha256"]}},
        "progress": {},
    })
    market_checkpoint = bootstrap_state.build({
        "bootstrap_id": identity_checkpoint["bootstrap_id"],
        "phase": "MARKET_INDEX",
        "market_session_id": market_index["market_session_id"],
        "source_manifest_sha256": market_index["source_manifest_sha256"],
        "parent_checkpoint_sha256": identity_checkpoint["checkpoint_sha256"],
        "created_at": LATER,
        "artifacts": {
            "identity_map": identity_checkpoint["artifacts"]["identity_map"],
            "market_index": {"path": f"data/v42/master-core500/bootstrap/artifacts/market-{market_index['market_index_sha256']}.json", "sha256": market_index["market_index_sha256"]},
        },
        "progress": {},
    }, identity_checkpoint)
    pointer_checkpoint = bootstrap_state.pointer(market_checkpoint, checkpoint_path=f"data/v42/master-core500/bootstrap/checkpoints/{market_checkpoint['checkpoint_sha256']}.json", published_at=LATER)
    record("Bootstrap checkpoint is GitHub-resumable and readback-bound", bootstrap_state.validate_pointer(pointer_checkpoint, market_checkpoint)["checkpoint_sha256"] == market_checkpoint["checkpoint_sha256"] and market_checkpoint["ordinary_daily_broad_allowed"] is False)
    record("Bootstrap checkpoint forbids phase skip", rejects(lambda: bootstrap_state.build({
        "bootstrap_id": identity_checkpoint["bootstrap_id"], "phase": "CLASSIFICATION", "market_session_id": market_index["market_session_id"],
        "source_manifest_sha256": market_index["source_manifest_sha256"], "parent_checkpoint_sha256": identity_checkpoint["checkpoint_sha256"], "created_at": LATER,
        "artifacts": {"identity_map": identity_checkpoint["artifacts"]["identity_map"], "market_index": market_checkpoint["artifacts"]["market_index"]}, "progress": {}
    }, identity_checkpoint), "phase skip"))
    tampered_pointer = copy.deepcopy(master_pointer)
    tampered_pointer["identity_path"] = "data/v42/identity/maps/" + "f" * 64 + ".json"
    tampered_pointer["pointer_sha256"] = semantic_hash({key: value for key, value in tampered_pointer.items() if key != "pointer_sha256"})
    record("MASTER pointer rejects a path/hash cross-binding", rejects(lambda: bootstrap.validate_master_pointer(tampered_pointer, bundle=bundle), "artifact mismatch") or rejects(lambda: bootstrap.validate_master_pointer(tampered_pointer, bundle=bundle), "path"))
    alpha = next(item for item in master["scopes"] if item["issuer_id"] == "CIK:0001652044")
    record("GOOG and GOOGL share one issuer scope", set(alpha["member_tickers"]) == {"GOOG", "GOOGL"})
    record("Canary and pilot are natural prefix subsets", master["canary_scope_keys"] == [item["research_scope_key"] for item in master["scopes"][:15]] and master["pilot_scope_keys"] == [item["research_scope_key"] for item in master["scopes"][:50]])
    record("Regression expectations are not empty", len(bundle["selector_certificate"]["regression_expectations"]) >= 5)
    record("Regression expectations were naturally selected", bundle["selector_certificate"]["regression_expectations_satisfied"] is True)

    # Negative provenance tests for the real V4.2.1 production failure.
    quality_leaves = [item for item in query_plan["leaves"] if item["purpose"] == provenance.QUERY_PURPOSE_QUALITY and item["lane"] == "established_quality"]
    fake_spec = copy.deepcopy(max(quality_leaves, key=lambda item: float(item["market_cap_min"])))
    target_sha = str(fake_spec["query_sha256"])
    original_receipt = next(item for item in query_plan["receipts"] if item["query_sha256"] == target_sha)
    fake_member = {
        "ticker": "FAKE000",
        "contract_id": "US:FAKE000",
        "company": "Fake",
        "connector_market_cap": float(fake_spec["market_cap_min"] + 1_000_000),
    }
    fake_rows = list(original_receipt["rows"]) + [metricduck_fixture_row(fake_member, cap=fake_member["connector_market_cap"])]
    fake_receipt = provenance.build_query_receipt(
        fake_spec,
        result_rows=fake_rows,
        matched_count=len(fake_rows),
        retrieved_at=NOW_TEXT,
        response_handle=response_handle(fake_spec, "fake"),
    )
    fake_plan = copy.deepcopy(query_plan)
    fake_plan["receipts"] = [fake_receipt if item["query_sha256"] == target_sha else item for item in fake_plan["receipts"]]
    fake_plan["query_plan_sha256"] = semantic_hash({key: value for key, value in fake_plan.items() if key != "query_plan_sha256"})
    fake_source = bootstrap.build_candidate_source(identity_map, market_index, fake_plan)
    record("Connector-only ticker never enters market-bound MASTER candidates", all(row["ticker"] != "FAKE000" for row in fake_source["candidates"]) and any(row.get("ticker") == "FAKE000" and row.get("reason") == "connector_result_not_in_pinned_market" for row in fake_source["excluded_query_rows"]))

    # A complete quality receipt may not create a candidate if the independent
    # connector classification catalog lacks that market binding.
    classification_receipt = next(item for item in query_plan["receipts"] if item["query_spec"]["purpose"] == provenance.QUERY_PURPOSE_CLASSIFICATION and item["rows"])
    missing_ticker = classification_receipt["rows"][0]["ticker"]
    reduced_rows = classification_receipt["rows"][1:]
    reduced_classification = provenance.build_query_receipt(
        classification_receipt["query_spec"],
        result_rows=reduced_rows,
        matched_count=len(reduced_rows),
        retrieved_at=NOW_TEXT,
        response_handle=response_handle(classification_receipt["query_spec"], "classification-missing-one"),
    )
    missing_binding_plan = copy.deepcopy(query_plan)
    missing_binding_plan["receipts"] = [reduced_classification if item["query_sha256"] == classification_receipt["query_sha256"] else item for item in missing_binding_plan["receipts"]]
    missing_binding_plan["query_plan_sha256"] = semantic_hash({key: value for key, value in missing_binding_plan.items() if key != "query_plan_sha256"})
    record("MASTER candidate is refused when classification evidence binding is missing", rejects(lambda: bootstrap.build_candidate_source(identity_map, market_index, missing_binding_plan), "lacks MetricDuck classification binding"), {"ticker": missing_ticker})

    guessed_sector_spec = copy.deepcopy(fake_spec)
    guessed_sector_spec["sector"] = "Technology"
    record("Guessed or Radar sector routing is explicitly rejected", rejects(lambda: provenance.normalize_query_spec(guessed_sector_spec), "Radar/human sector routing is forbidden"))

    saturated = provenance.build_query_receipt(query_plan["leaves"][0], result_rows=[], matched_count=1, retrieved_at=NOW_TEXT, response_handle=response_handle(query_plan["leaves"][0], "saturated"))
    saturated_plan = copy.deepcopy(query_plan)
    saturated_plan["receipts"] = [saturated if item["query_sha256"] == saturated["query_sha256"] else item for item in saturated_plan["receipts"]]
    saturated_plan["query_plan_sha256"] = semantic_hash({key: value for key, value in saturated_plan.items() if key != "query_plan_sha256"})
    record("Saturated MetricDuck leaf is rejected", rejects(lambda: provenance.validate_query_plan(saturated_plan, market_index_value=market_index), "incomplete"))

    omitted = copy.deepcopy(query_plan)
    removed_leaf = omitted["leaves"].pop(0)
    omitted["receipts"] = [item for item in omitted["receipts"] if item["query_sha256"] != removed_leaf["query_sha256"]]
    omitted["leaf_count"] -= 1
    omitted["receipt_count"] -= 1
    omitted["query_plan_sha256"] = semantic_hash({key: value for key, value in omitted.items() if key != "query_plan_sha256"})
    record("Omitted MetricDuck partition is rejected", rejects(lambda: provenance.validate_query_plan(omitted, market_index_value=market_index), "gap"))

    wrong_filter = copy.deepcopy(query_plan["leaves"][0])
    wrong_filter["connector_filters"] = [{"metric_id":"roic","operator":"gte","value":0.01,"period_type":"ttm"}]
    record("Unapproved native MetricDuck filter is rejected", rejects(lambda: provenance.normalize_query_spec(wrong_filter), "native filters"))
    legacy_filter = copy.deepcopy(query_plan["leaves"][0])
    legacy_filter["filters"] = {"fcf_margin_pct": {"operator": "gte", "value": 0.05}}
    record("Legacy internal MetricDuck field filter is rejected", rejects(lambda: provenance.normalize_query_spec(legacy_filter), "legacy V4.2"))
    price_filter = copy.deepcopy(query_plan["leaves"][0])
    price_filter["filters"] = {"current_price": {"operator": "gte", "value": 3}}
    record("Price-based legacy query filter is rejected", rejects(lambda: provenance.normalize_query_spec(price_filter)))

    tampered_source = copy.deepcopy(source)
    tampered_source["candidates"][0]["facts"]["roic"] = 9.9
    row = tampered_source["candidates"][0]
    row["candidate_row_sha256"] = semantic_hash({key: value for key, value in row.items() if key != "candidate_row_sha256"})
    tampered_source["source_sha256"] = semantic_hash({key: value for key, value in tampered_source.items() if key != "source_sha256"})
    record("Self-consistent tampered candidate source fails evidence recomputation", rejects(lambda: bootstrap.validate_candidate_source_against_evidence(tampered_source, identity_map_value=identity_map, market_index_value=market_index, query_plan_value=query_plan), "canonical derivation"))

    ghost_bundle = _rehash_bundle_with_scope_mutation(bundle, lambda value: value["scopes"][0].update({"ticker": "GHOST", "contract_id": "US:GHOST", "issuer_id": "CIK:9999999999", "research_scope_key": "CIK:9999999999|common_equity", "member_tickers": ["GHOST"], "member_contract_ids": ["US:GHOST"]}))
    ghost_bundle["master"]["canary_scope_keys"][0] = "CIK:9999999999|common_equity"
    ghost_bundle["master"]["pilot_scope_keys"][0] = "CIK:9999999999|common_equity"
    # Rehash again after subset changes.
    ghost_bundle = _rehash_bundle_with_scope_mutation(ghost_bundle, lambda value: None)
    record("GHOST scope absent from source is rejected", rejects(lambda: bootstrap.validate_master_bundle(ghost_bundle), "canonical deterministic derivation"))

    def swap_identity(value: dict[str, Any]) -> None:
        fields = ("ticker", "contract_id", "issuer_id", "research_scope_key", "member_tickers", "member_contract_ids", "member_market_membership_sha256s")
        first_values = {field: copy.deepcopy(value["scopes"][0].get(field)) for field in fields}
        second_values = {field: copy.deepcopy(value["scopes"][1].get(field)) for field in fields}
        for field in fields:
            value["scopes"][0][field] = second_values[field]
            value["scopes"][1][field] = first_values[field]
        value["canary_scope_keys"] = [item["research_scope_key"] for item in value["scopes"][:15]]
        value["pilot_scope_keys"] = [item["research_scope_key"] for item in value["scopes"][:50]]
    wrong_order_bundle = _rehash_bundle_with_scope_mutation(bundle, swap_identity)
    record("Self-consistent wrong MASTER order is rejected", rejects(lambda: bootstrap.validate_master_bundle(wrong_order_bundle), "canonical deterministic derivation"))

    tampered_master = copy.deepcopy(master)
    tampered_master["scopes"][0]["ticker"] = "HACK"
    record("Simple MASTER tampering fails self hash", rejects(lambda: bootstrap.validate_master(tampered_master), "self hash"))
    contaminated_source = copy.deepcopy(source)
    contaminated_source["candidates"][0]["facts"]["current_price"] = 100
    contaminated_source["candidates"][0]["candidate_row_sha256"] = semantic_hash({key: value for key, value in contaminated_source["candidates"][0].items() if key != "candidate_row_sha256"})
    contaminated_source["source_sha256"] = semantic_hash({key: value for key, value in contaminated_source.items() if key != "source_sha256"})
    record("Nested current-price contamination fails", rejects(lambda: bootstrap.validate_candidate_source(contaminated_source), "recovery/current-price"))

    # Quality-tier-first selection remains unchanged.
    high = {"ticker": "HIGH", "contract_id": "US:HIGH", "issuer_id": "CIK:0000000001", "security_type": "common_equity", "l3_status": "pass", "fundamental_eligible": True, "l3_score": 90, "l3_coverage_pct": 90, "recovery_setup_score": 40, "evidence_confidence_pct": 90, "avg_dollar_volume": 10e6, "hard_vetoes": []}
    low = {"ticker": "LOW", "contract_id": "US:LOW", "issuer_id": "CIK:0000000002", "security_type": "common_equity", "l3_status": "pass", "fundamental_eligible": True, "l3_score": 66, "l3_coverage_pct": 90, "recovery_setup_score": 100, "evidence_confidence_pct": 100, "avg_dollar_volume": 10e6, "hard_vetoes": []}
    record("Recovery cannot compensate across Quality tiers", selection.select("L3", [low, high], 1)["selected"][0]["ticker"] == "HIGH")
    same_a = {**high, "ticker": "A1", "contract_id": "US:A1", "issuer_id": "CIK:0000000011", "l3_score": 90, "recovery_setup_score": 55}
    same_b = {**high, "ticker": "A2", "contract_id": "US:A2", "issuer_id": "CIK:0000000012", "l3_score": 86, "recovery_setup_score": 90}
    record("Recovery may decide within one Quality tier", selection.select("L3", [same_a, same_b], 1)["selected"][0]["ticker"] == "A2")
    record("L4 remains Quality-tier first", selection.select("L4", [{**same_b, "l4_status": "pass", "l4_score": 98, "l4_coverage_pct": 90}, {**low, "l4_status": "pass", "l4_score": 100, "l4_coverage_pct": 90}], 1)["selected"][0]["ticker"] == "A2")

    # Fact Pack and Registry end to end.
    source_payload = l3_payload("established_quality", "REG")
    source_payload["structure"] = {"leveraged": False, "inverse": False}
    fact_pack = factpack.build(source_payload)
    record("Structural Fact Pack is hash-bound", factpack.validate(fact_pack)["fact_pack_sha256"] == fact_pack["fact_pack_sha256"])
    l3_from_pack = factpack.to_l3_input(fact_pack)
    record("Fact Pack preserves eligibility metadata", l3_from_pack.get("instrument_status") == "eligible" and l3_from_pack.get("structure") == source_payload["structure"])
    from contracts import validate as validate_contract
    record("Fact Pack satisfies canonical L3 input", not rejects(lambda: validate_contract("l3-input", l3_from_pack)))
    bad_pack = dict(source_payload)
    bad_pack["quote"] = {"last": 100}
    record("Fact Pack rejects quote payload", rejects(lambda: factpack.build(bad_pack), "fast/recovery"))
    missing_status = dict(source_payload)
    missing_status.pop("instrument_status", None)
    record("Fact Pack fails closed without instrument status", rejects(lambda: factpack.build(missing_status), "instrument_status"))
    result = passport.evaluate(l3_from_pack)
    record("New Passport is rich-auditability", result["auditability_class"] == "v42_rich" and result["evidence_structure_validated"] is True)
    record("Eligible instrument remains eligible after Fact Pack", "instrument_ineligible" not in result["hard_vetoes"])
    scope = f"{result['issuer_id']}|common_equity"
    proposal = registry.proposal(result, research_scope_key=scope, event_scan_through="2026-08-18")
    record("Registry accepts event watermark", registry.validate_proposal(proposal)["event_scan_through"] == "2026-08-18")
    second_result = passport.evaluate(factpack.to_l3_input(factpack.build(l3_payload("established_quality", "RG2"))))
    second_scope = f"{second_result['issuer_id']}|common_equity"
    second_proposal = registry.proposal(second_result, research_scope_key=second_scope, event_scan_through="2026-08-18")
    batch_proposal = registry.batch_proposal([proposal, second_proposal])
    record("Registry batch supports one wave", len(registry.validate_batch(batch_proposal)["items"]) == 2)
    mutated_batch = copy.deepcopy(batch_proposal)
    mutated_batch["items"][0]["passport_payload"]["structural_facts"]["cash"] = 123
    record("Registry batch rejects post-hash mutation", rejects(lambda: registry.validate_batch(mutated_batch), "batch self hash mismatch"))
    record("Registry batch refuses more than four", rejects(lambda: registry.batch_proposal([proposal, second_proposal, proposal, second_proposal, proposal]), "1..wave_size"))

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        stored = registry_store.apply_passport_proposal(root, proposal, producer_release_sha256="a" * 64, published_at=NOW_TEXT)
        entry = stored["entry"]
        passport_value = json.loads((root / entry["passport_path"]).read_text())
        receipt = stored["receipt"]
        readback = registry.bootstrap_durable_complete(receipt=receipt, entry=entry, passport_value=passport_value)
        record("Registry completion requires pointer Passport and receipt", readback["durable_reviewed"] and readback["quality_resolved"])
        record("Fresh Passport reuses Structural Quality", registry.reuse(entry, passport_value=passport_value, market_session_id="2026-08-18") is not None)
        record("Stale Passport is not reused", registry.reuse(entry, passport_value=passport_value, market_session_id="2026-08-19") is None)
        tampered = copy.deepcopy(passport_value)
        tampered["summary"]["quality_score"] = 1
        record("Tampered Passport fails", rejects(lambda: registry.bootstrap_durable_complete(receipt=receipt, entry=entry, passport_value=tampered), "content hash"))
        replayed = registry_store.apply_passport_proposal(root, proposal, producer_release_sha256="b" * 64, published_at=LATER)
        record("Identical Registry proposal preserves original receipt", replayed["receipt"] == receipt)
        blocked_scope = f"{blocked_result['issuer_id']}|common_equity"
        blocked_proposal = registry.proposal(blocked_result, research_scope_key=blocked_scope, event_scan_through="2026-08-18")
        blocked_stored = registry_store.apply_passport_proposal(root, blocked_proposal, producer_release_sha256="a" * 64, published_at=NOW_TEXT)
        blocked_entry = blocked_stored["entry"]
        blocked_passport = json.loads((root / blocked_entry["passport_path"]).read_text())
        blocked_readback = registry.bootstrap_durable_complete(receipt=blocked_stored["receipt"], entry=blocked_entry, passport_value=blocked_passport)
        record("Registry persists insufficient_data review date", blocked_entry["next_review_date"] == "2026-08-25" and blocked_readback["next_review_date"] == "2026-08-25")
        record("Insufficient_data Passport is durable but never reusable", blocked_readback["durable_reviewed"] is True and blocked_readback["quality_resolved"] is False and registry.reuse(blocked_entry, passport_value=blocked_passport, market_session_id="2026-08-18") is None)

    # Campaign phases and computed gates.
    empty_state = campaign.build_state(bundle, {})
    record("Campaign starts CANARY", empty_state["phase"] == "CANARY" and empty_state["daily_broad_allowed"] is False)
    canary_map = {key: durable_record(token="b") for key in master["canary_scope_keys"]}
    canary_state = campaign.build_state(bundle, canary_map, previous_state=empty_state)
    record("Fifteen records without reconstruction gate remain CANARY", canary_state["phase"] == "CANARY")
    missing_review_map = dict(canary_map)
    missing_key = master["canary_scope_keys"][0]
    missing_review_map[missing_key] = durable_record(status="insufficient_data", token="9")
    missing_review_map[missing_key]["next_review_date"] = None
    record("Campaign refuses insufficient_data without next review date", campaign.build_state(bundle, missing_review_map)["canary_durable_count"] == 14)
    canary_snapshot = campaign.durable_snapshot(master, canary_map)
    runtime_gate = campaign.runtime_reconstruction_gate(master, canary_snapshot, canary_snapshot, source_commit_sha="a" * 40, workflow_run_id="fixture-run-1", reconstructed_at=NOW_TEXT, validator_release_sha256="c" * 64)
    pilot_start = campaign.build_state(bundle, canary_map, runtime_gate_value=runtime_gate, previous_state=canary_state)
    record("Computed clean reconstruction gate enters PILOT", pilot_start["phase"] == "PILOT")
    record("Runtime gate records clean checkout", runtime_gate["clean_checkout"] is True and runtime_gate["local_state_used"] is False)
    bad_reconstructed = copy.deepcopy(canary_snapshot)
    bad_reconstructed["records"].pop(master["canary_scope_keys"][0])
    bad_reconstructed["snapshot_sha256"] = semantic_hash({key: value for key, value in bad_reconstructed.items() if key != "snapshot_sha256"})
    record("Runtime gate detects reconstruction loss", rejects(lambda: campaign.runtime_reconstruction_gate(master, canary_snapshot, bad_reconstructed, source_commit_sha="a" * 40, workflow_run_id="fixture-run-2", reconstructed_at=NOW_TEXT, validator_release_sha256="c" * 64), "evidence invalid"))
    record("Old manual runtime pass boolean is refused", rejects(lambda: campaign.runtime_reconstruction_gate(master, canary_snapshot, canary_snapshot, source_commit_sha="a" * 40, workflow_run_id="x", reconstructed_at=NOW_TEXT, validator_release_sha256="c" * 64, runtime_reconstruction_passed=True)))

    pilot_map = {key: durable_record(token="d") for key in master["pilot_scope_keys"]}
    pilot_snapshot = campaign.durable_snapshot(master, pilot_map)
    pilot_gate = campaign.pilot_registry_gate(master, pilot_snapshot, pilot_snapshot, reuse_results=reuse_results(pilot_snapshot, list(master["pilot_scope_keys"])), source_commit_sha="a" * 40, workflow_run_id="fixture-run-3", verified_at=NOW_TEXT, validator_release_sha256="c" * 64)
    core_start = campaign.build_state(bundle, pilot_map, runtime_gate_value=runtime_gate, pilot_gate_value=pilot_gate, previous_state=pilot_start)
    record("Computed zero-loss reuse gate enters CORE500", core_start["phase"] == "CORE500")
    record("PILOT loss count is computed as zero", pilot_gate["registry_loss_count"] == 0 and pilot_gate["reuse_verified_count"] == 50)
    pilot_blocked_map = dict(pilot_map)
    pilot_blocked_key = master["pilot_scope_keys"][-1]
    pilot_blocked_map[pilot_blocked_key] = durable_record(status="insufficient_data", token="8")
    pilot_blocked_snapshot = campaign.durable_snapshot(master, pilot_blocked_map)
    reusable_pilot_keys = [key for key in master["pilot_scope_keys"] if key != pilot_blocked_key]
    pilot_blocked_gate = campaign.pilot_registry_gate(master, pilot_blocked_snapshot, pilot_blocked_snapshot, reuse_results=reuse_results(pilot_blocked_snapshot, reusable_pilot_keys), source_commit_sha="a" * 40, workflow_run_id="fixture-run-blocked", verified_at=NOW_TEXT, validator_release_sha256="c" * 64)
    record("PILOT reconstructs insufficient_data without reusing it as quality", pilot_blocked_gate["pilot_gate_passed"] is True and pilot_blocked_gate["reuse_verified_count"] == 49 and pilot_blocked_gate["blocked_reconstruction_verified_count"] == 1 and pilot_blocked_gate["blocked_scope_keys"] == [pilot_blocked_key])
    record("Old manual PILOT loss inputs are refused", rejects(lambda: campaign.pilot_registry_gate(master, pilot_snapshot, pilot_snapshot, reuse_results=reuse_results(pilot_snapshot, list(master["pilot_scope_keys"])), source_commit_sha="a" * 40, workflow_run_id="x", verified_at=NOW_TEXT, validator_release_sha256="c" * 64, registry_loss_count=0, reuse_check_passed=True)))
    lost_snapshot = copy.deepcopy(pilot_snapshot)
    lost_snapshot["records"].pop(master["pilot_scope_keys"][0])
    lost_snapshot["snapshot_sha256"] = semantic_hash({key: value for key, value in lost_snapshot.items() if key != "snapshot_sha256"})
    record("PILOT gate detects Registry loss", rejects(lambda: campaign.pilot_registry_gate(master, pilot_snapshot, lost_snapshot, reuse_results=reuse_results(pilot_snapshot, list(master["pilot_scope_keys"])), source_commit_sha="a" * 40, workflow_run_id="fixture-run-4", verified_at=NOW_TEXT, validator_release_sha256="c" * 64), "loss"))

    map_499 = {item["research_scope_key"]: durable_record(token="e") for item in master["scopes"][:499]}
    state_499 = campaign.build_state(bundle, map_499, runtime_gate_value=runtime_gate, pilot_gate_value=pilot_gate, previous_state=core_start)
    record("499 attempts remain CORE500", state_499["phase"] == "CORE500" and state_499["daily_broad_allowed"] is False)
    complete_map = {item["research_scope_key"]: durable_record(token="f") for item in master["scopes"]}
    complete_map[master["scopes"][-1]["research_scope_key"]] = durable_record(status="insufficient_data", token="1")
    complete_state = campaign.build_state(bundle, complete_map, runtime_gate_value=runtime_gate, pilot_gate_value=pilot_gate, previous_state=state_499)
    record("Five hundred terminal attempts enter COMPLETE", complete_state["phase"] == "COMPLETE" and complete_state["daily_broad_allowed"] is True)
    record("Insufficient data stays quality unknown", complete_state["quality_unknown_count"] == 1 and complete_state["quality_resolved_count"] == 499)
    record("Insufficient data stays competitively unresolved", complete_state["competitive_unresolved_count"] == 1 and complete_state["durable_blocker_count"] == 1)
    pointer = campaign.state_pointer(complete_state, state_path=f"data/v42/campaigns/{master['master_sha256']}/state.json", published_at=NOW_TEXT, producer_release_sha256="a" * 64)
    record("V4.2 campaign pointer is hash-bound", campaign.validate_state_pointer(pointer, state=complete_state)["state_sha256"] == complete_state["state_sha256"])
    bad_pointer = copy.deepcopy(pointer)
    bad_pointer["state_path"] = "data/v42/campaigns/other/state.json"
    bad_pointer["pointer_sha256"] = semantic_hash({key: value for key, value in bad_pointer.items() if key != "pointer_sha256"})
    record("Campaign pointer cannot cross-bind another MASTER", rejects(lambda: campaign.validate_state_pointer(bad_pointer, state=complete_state), "path"))
    record("Backward campaign transition is forbidden", rejects(lambda: campaign.build_state(bundle, {}, previous_state=core_start), "backward"))
    record("Campaign plans four deep scopes", len(campaign.plan_wave(pilot_start, bundle=bundle)["plans"]) == 4)
    repeat = campaign.build_state(bundle, pilot_map, runtime_gate_value=runtime_gate, pilot_gate_value=pilot_gate)
    record("Fresh runtime reconstructs deterministic state", repeat["state_sha256"] == campaign.build_state(bundle, pilot_map, runtime_gate_value=runtime_gate, pilot_gate_value=pilot_gate)["state_sha256"])
    stale = campaign.build_state(bundle, {master["scopes"][0]["research_scope_key"]: durable_record(session="2026-08-17")})
    record("Stale Registry record is not credited", stale["master_durable_count"] == 0)
    outside = campaign.build_state(bundle, {"OUTSIDE|common_equity": durable_record()})
    record("Registry knowledge outside MASTER is ignored, not deleted", outside["master_durable_count"] == 0)

    # Migration preservation.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        registry_store.apply_passport_proposal(root, proposal, producer_release_sha256="a" * 64, published_at=NOW_TEXT)
        legacy_body = {"schema_version": "1.0.0", "kind": "qrgf_v4_core500_cohort", "architecture_version": "4.0.0", "market_session_id": "2026-08-18", "selection_model_version": "legacy", "requested_size": 15, "selected_scope_count": 15, "core500_is_research_bootstrap_not_whitelist": True, "current_recovery_used": False, "scopes": [{"research_scope_key": scope} for _ in range(15)]}
        legacy = {**legacy_body, "cohort_sha256": semantic_hash(legacy_body)}
        write_json(root / "data/v4/bootstrap/cohorts/legacy/cohort.json", legacy)
        write_json(root / "data/v4/bootstrap/latest.json", {"cohort_path": "data/v4/bootstrap/cohorts/legacy/cohort.json"})
        report = migration.build_migration_report(root)
        record("Legacy Core15 remains historical only", report["legacy_bootstrap"]["selected_scope_count"] == 15 and report["legacy_is_not_authoritative_master"] is True)
        record("V4.1 production state is marked read-only", report["v41_production_state_is_historical_read_only"] is True)
        record("Registry knowledge is preserved", report["registry_scope_count"] == 1 and report["registry_knowledge_outside_master_preserved"] is True)
        record("Migration target is V4.2", report["target_architecture_version"] == "4.2.0")

    # Batch ceiling.
    payloads = [l3_payload("established_quality", f"W{i}") for i in range(4)]
    record("Deep L3 wave remains capped at four", len(batch.evaluate("L3", payloads)) == 4)
    record("Fifth deep item is refused", rejects(lambda: batch.evaluate("L3", payloads + [l3_payload("established_quality", "W5")]), "exceeds wave"))

    # Challenger transport and frontier.
    blocked = market_view.build_market_session([], bundle_value=bundle, state_value=state_499, source_snapshot_id="radar-fixture")
    record("Market producer blocks before COMPLETE", blocked["kind"] == market_view.BLOCKED_KIND and blocked["ordinary_daily_broad_allowed"] is False)
    challengers = [{"ticker": f"X{i:04d}", "contract_id": f"US:X{i:04d}", "issuer_id": f"ISSUER:{i:04d}", "instrument_status": "eligible", "l2_status": "pass", "l2_setup_score": 100 - (i % 100), "l2_confidence_pct": 80, "setup_prior_growth": 30, "avg_dollar_volume": 10_000_000} for i in range(706)]
    session = market_view.build_market_session(challengers, bundle_value=bundle, state_value=complete_state, source_snapshot_id="radar-fixture")
    manifest, pages = session["manifest"], session["pages"]
    record("Challenger page is at most 250", all(page["row_count"] <= 250 for page in pages.values()))
    record("706 challengers create three pages", manifest["total_eligible_challengers"] == 706 and manifest["page_count"] == 3)
    record("Page union has no duplicates", len({row["research_scope_key"] for page in pages.values() for row in page["rows"]}) == 706)
    repeated_session = market_view.build_market_session(challengers, bundle_value=bundle, state_value=complete_state, source_snapshot_id="radar-fixture")
    record("Pinned market ordering is stable", repeated_session["manifest"]["manifest_sha256"] == manifest["manifest_sha256"])

    empty_progress = market_view.next_page(manifest, pages=pages, durable_by_scope={}, triage_by_scope={})
    record("No triage starts at page zero", empty_progress["next_page_index"] == 0)
    first_page_triage: dict[str, Any] = {}
    for row in pages[0]["rows"]:
        first_page_triage[row["research_scope_key"]] = market_view.build_triage(row, disposition="deep_research_required", reason="quality_unknown", evidence_sha256="a" * 64, triaged_at=NOW_TEXT)
    after_first = market_view.next_page(manifest, pages=pages, durable_by_scope={}, triage_by_scope=first_page_triage)
    record("Transport advances after durable triage even with unresolved frontier", after_first["next_page_index"] == 1 and after_first["next_cursor"] == 250)
    frontier_first = market_view.build_frontier(manifest, pages=pages, durable_by_scope={}, triage_by_scope=first_page_triage)
    record("Deep-research rows remain in competitive frontier", frontier_first["competitive_unresolved_count"] == 706)
    record("Next deep wave is capped at four", len(market_view.next_deep_scopes(frontier_first, manifest=manifest, pages=pages)) == 4)

    partial_triage = {key: value for index, (key, value) in enumerate(first_page_triage.items()) if index < 100}
    partial_progress = market_view.next_page(manifest, pages=pages, durable_by_scope={}, triage_by_scope=partial_triage)
    record("Partially triaged page cannot be skipped", partial_progress["next_page_index"] == 0 and partial_progress["page_triaged_scope_count"] == 100)
    record("Unsafe arbitrary exclusion reason is rejected", rejects(lambda: market_view.build_triage(pages[0]["rows"][0], disposition="safe_exclusion", reason="looks_bad", evidence_sha256="a" * 64, triaged_at=NOW_TEXT), "approved proven reason"))

    all_safe: dict[str, Any] = {}
    for page in pages.values():
        for row in page["rows"]:
            all_safe[row["research_scope_key"]] = market_view.build_triage(row, disposition="safe_exclusion", reason="mathematical_upper_bound_below_cutoff", evidence_sha256="b" * 64, triaged_at=NOW_TEXT)
    safe_progress = market_view.next_page(manifest, pages=pages, durable_by_scope={}, triage_by_scope=all_safe)
    safe_claim = market_view.production_claim(manifest, pages=pages, durable_by_scope={}, triage_by_scope=all_safe)
    record("All triaged pages complete transport", safe_progress["universe_transport_complete"] is True)
    record("Exhaustive claim requires empty frontier", safe_claim["exhaustive_full_market_top30_claim_authorized"] is True and safe_claim["competitive_unresolved_count"] == 0)

    all_insufficient = {row["research_scope_key"]: durable_record(status="insufficient_data", token="2") for page in pages.values() for row in page["rows"]}
    insufficient_progress = market_view.next_page(manifest, pages=pages, durable_by_scope=all_insufficient, triage_by_scope={})
    insufficient_claim = market_view.production_claim(manifest, pages=pages, durable_by_scope=all_insufficient, triage_by_scope={})
    record("Insufficient-data challengers can complete transport", insufficient_progress["universe_transport_complete"] is True)
    record("Insufficient-data challengers do not close competition", insufficient_claim["exhaustive_full_market_top30_claim_authorized"] is False and insufficient_claim["competitive_unresolved_count"] == 706)
    record("Page size remains transport, not whitelist", manifest["transport_is_not_quality_whitelist"] is True and manifest["total_eligible_challengers"] > manifest["page_size"])

    pass_records = {row["research_scope_key"]: durable_record(status="pass", token="3") for page in pages.values() for row in page["rows"]}
    pass_claim = market_view.production_claim(manifest, pages=pages, durable_by_scope=pass_records, triage_by_scope={})
    record("Resolved pass challengers close competitive uncertainty", pass_claim["exhaustive_full_market_top30_claim_authorized"] is True)

    # Execution and package hygiene.
    execution = decision.validate_execution(execution_payload())
    record("IBKR execution contract remains intact", execution["valid"], execution)
    forbidden = ["v31state.py", "v3state.py", "frontier31.py", "remote_frontier.py", "legacy.py", "funnel.py", "quality_bounds.py"]
    record("Package contains no legacy production engines", not any((ROOT / "scripts" / name).exists() for name in forbidden))
    protected_files = {item["path"] for item in integrity.build()["files"]}
    record("V4.2 provenance module is integrity-protected", "scripts/provenance.py" in protected_files)

    passed = sum(item["pass"] for item in RESULTS)
    failed = len(RESULTS) - passed
    report = {"total": len(RESULTS), "passed": passed, "failed": failed, "results": RESULTS}
    output_path = os.environ.get("QRGF_TEST_RESULTS")
    if output_path:
        write_json(Path(output_path), report)
    print(f"TOTAL {len(RESULTS)} PASS {passed} FAIL {failed}")
    for item in RESULTS:
        print(("PASS" if item["pass"] else "FAIL") + " | " + item["name"])
        if not item["pass"]:
            print(json.dumps(item["detail"], ensure_ascii=False, default=str)[:5000])
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
