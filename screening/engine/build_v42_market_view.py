#!/usr/bin/env python3
"""Publish V4.2 market pages, durable triage progress and competitive frontier."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import v42_runtime as rt

if str(rt.SCRIPTS) not in sys.path:
    sys.path.insert(0, str(rt.SCRIPTS))

from common import load_connectors, semantic_hash, write_json
import market_view, migration

RELEASE_REL = "screening/config/v42-market-producer-release.json"


def _load_radar() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    system = rt.read(rt.REPO_ROOT / "data/v3/latest.json")
    if system.get("complete") is not True:
        raise ValueError("V3 Radar system snapshot is incomplete")
    manifest_path = rt.REPO_ROOT / str(system.get("radar_manifest_path") or "")
    manifest = rt.read(manifest_path)
    if manifest.get("complete") is not True:
        raise ValueError("V3 Radar manifest is incomplete")
    if system.get("radar_manifest_sha256") != manifest.get("manifest_semantic_sha256"):
        raise ValueError("V3 Radar pointer/manifest mismatch")
    rows: list[dict[str, Any]] = []
    for page in manifest.get("pages") or []:
        path = manifest_path.parent / str(page.get("name") or "")
        if not path.is_file() or rt.file_hash(path) != str(page.get("sha256") or ""):
            raise ValueError(f"V3 Radar page hash mismatch: {page.get('name')}")
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows.extend(dict(item) for item in csv.DictReader(handle))
    if len(rows) != int(manifest.get("rows") or -1):
        raise ValueError("V3 Radar row count mismatch")
    return system, manifest, rows


def _blocked(reason: str, release_sha: str, *, master_sha: str | None = None, phase: str | None = None) -> dict[str, Any]:
    body = {
        "schema_version": "2.0.0",
        "kind": "qrgf_v42_market_pointer",
        "architecture_version": "4.2.0",
        "ordinary_daily_broad_allowed": False,
        "reason": reason,
        "master_sha256": master_sha,
        "campaign_phase": phase,
        "producer_release_sha256": release_sha,
    }
    pointer = {**body, "pointer_sha256": semantic_hash(body)}
    write_json(rt.REPO_ROOT / "data/v42/market/latest.json", pointer)
    return pointer


def _enrich_core_scopes(rows: list[dict[str, Any]], master: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_ticker: dict[str, Mapping[str, Any]] = {}
    for scope in master["scopes"]:
        for ticker in scope.get("member_tickers") or [scope.get("ticker")]:
            if ticker:
                by_ticker[str(ticker).upper()] = scope
    output: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        scope = by_ticker.get(str(item.get("ticker") or "").upper())
        if scope is not None:
            item.update({
                "issuer_id": scope["issuer_id"],
                "security_overlay": scope["security_overlay"],
                "research_scope_key": scope["research_scope_key"],
            })
        output.append(item)
    return output


def _public_market_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    keep = (
        "ticker", "company", "contract_id", "security_type", "instrument_status", "exchange", "sector", "industry",
        "current_price", "reference_52w_high", "market_cap", "avg_dollar_volume", "return_1m_pct", "return_3m_pct",
        "return_6m_pct", "return_12m_pct", "drawdown_pct", "historical_volatility_pct", "trading_history_days",
        "momentum_history_status", "data_integrity_status", "as_of", "l2_status", "l2_setup_score", "l2_confidence_pct",
        "setup_prior_growth", "setup_pullback_geometry", "setup_liquidity", "setup_data_completeness",
    )
    return {key: raw.get(key) for key in keep}


def _core_view(rows: list[dict[str, Any]], master: Mapping[str, Any], inventory: Mapping[str, Mapping[str, Any]], market_session: str) -> dict[str, Any]:
    by_ticker = {str(row.get("ticker") or "").upper(): row for row in rows}
    output: list[dict[str, Any]] = []
    for scope in master["scopes"]:
        raw = None
        for ticker in scope.get("member_tickers") or [scope.get("ticker")]:
            if str(ticker or "").upper() in by_ticker:
                raw = by_ticker[str(ticker).upper()]
                break
        item = _public_market_row(raw or {"ticker": scope.get("ticker"), "contract_id": scope.get("contract_id")})
        record = dict(inventory.get(scope["research_scope_key"]) or {})
        reusable = (
            record.get("durable_readback_verified") is True
            and record.get("freshness_status") == "fresh"
            and str(record.get("event_scan_through") or "") >= market_session
            and str(record.get("quality_status") or "") in {"pass", "conditional"}
        )
        item.update({
            "issuer_id": scope["issuer_id"],
            "security_overlay": scope["security_overlay"],
            "research_scope_key": scope["research_scope_key"],
            "master_rank": scope["rank"],
            "bootstrap_priority_score": scope.get("bootstrap_priority_score"),
            "registry_status": record.get("freshness_status") or "missing",
            "quality_reusable": reusable,
            "quality_status": record.get("quality_status"),
            "quality_score": record.get("quality_score"),
            "quality_coverage_pct": record.get("quality_coverage_pct"),
            "quality_eligible": record.get("quality_eligible") is True,
            "event_scan_through": record.get("event_scan_through"),
            "passport_hash": record.get("passport_hash"),
            "next_review_date": record.get("next_review_date"),
        })
        output.append(item)
    body = {
        "schema_version": "2.0.0",
        "kind": "qrgf_v42_core_market_view",
        "architecture_version": "4.2.0",
        "source_market_session_id": market_session,
        "master_sha256": master["master_sha256"],
        "rows": output,
    }
    return {**body, "view_sha256": semantic_hash(body)}


def _triage_records(session_id: str, pages: Mapping[int, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    connectors = load_connectors()["market_view_v42"]
    row_by_scope = {str(row["research_scope_key"]): row for page in pages.values() for row in page["rows"]}
    triage_dir = rt.REPO_ROOT / f"{connectors['triage_prefix']}/{session_id}"
    proposals_dir = rt.REPO_ROOT / f"{connectors['triage_proposal_prefix']}/{session_id}"
    output: dict[str, dict[str, Any]] = {}
    for path in sorted(triage_dir.glob("*.json")) if triage_dir.is_dir() else []:
        value = rt.read(path)
        key = str(value.get("research_scope_key") or "")
        if key not in row_by_scope:
            raise ValueError(f"stored triage references unknown challenger: {key}")
        output[key] = market_view.validate_triage(value, row=row_by_scope[key])
    for path in sorted(proposals_dir.glob("*.json")) if proposals_dir.is_dir() else []:
        value = rt.read(path)
        key = str(value.get("research_scope_key") or "")
        if key not in row_by_scope:
            raise ValueError(f"triage proposal references unknown challenger: {key}")
        triage = market_view.validate_triage(value, row=row_by_scope[key])
        previous = output.get(key)
        if previous is not None and previous["triage_sha256"] != triage["triage_sha256"]:
            raise ValueError(f"conflicting triage proposal for one challenger: {key}")
        target = triage_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
        rt.immutable(target, triage)
        output[key] = triage
    return output


def main() -> int:
    release_sha = rt.verify_release(RELEASE_REL, "market_view_v42")
    authority = rt.load_master_authority()
    if authority is None:
        pointer = _blocked("MASTER_CORE500_NOT_INITIALIZED", release_sha)
        print(json.dumps({"status": "blocked", "reason": pointer["reason"]}, sort_keys=True))
        return 0
    bundle = authority[1]
    master = bundle["master"]
    state_loaded = rt.load_campaign_state(bundle)
    if state_loaded is None or state_loaded[1].get("daily_broad_allowed") is not True:
        phase = state_loaded[1]["phase"] if state_loaded else None
        pointer = _blocked("MASTER_CORE500_NOT_COMPLETE", release_sha, master_sha=master["master_sha256"], phase=phase)
        print(json.dumps({"status": "blocked", "reason": pointer["reason"], "phase": phase}, sort_keys=True))
        return 0
    state = state_loaded[1]
    system, radar_manifest, raw_rows = _load_radar()
    market_session = str(system["market_session_id"])
    rows = _enrich_core_scopes(raw_rows, master)
    session = market_view.build_market_session(
        rows,
        bundle_value=bundle,
        state_value=state,
        source_snapshot_id=str(system.get("snapshot_id") or ""),
        source_market_session_id=market_session,
    )
    manifest = session["manifest"]
    pages = session["pages"]
    session_id = manifest["market_session_id"]
    session_root = rt.REPO_ROOT / f"data/v42/market/sessions/{session_id}"
    for index, page in pages.items():
        rt.immutable(session_root / f"challengers/page-{index:04d}.json", page)
    rt.immutable(session_root / "manifest.json", manifest)

    inventory = migration.registry_inventory(rt.REPO_ROOT)
    core = _core_view(raw_rows, master, inventory, market_session)
    rt.immutable(session_root / "core.json", core)
    triage = _triage_records(session_id, pages)
    frontier = market_view.build_frontier(manifest, pages=pages, durable_by_scope=inventory, triage_by_scope=triage)
    frontier_path = rt.REPO_ROOT / f"data/v42/market/frontiers/{session_id}/{frontier['frontier_sha256']}.json"
    rt.immutable(frontier_path, frontier)
    progress = market_view.next_page(manifest, pages=pages, durable_by_scope=inventory, triage_by_scope=triage)
    progress_body = {
        "schema_version": "2.0.0",
        "kind": "qrgf_v42_market_progress",
        "architecture_version": "4.2.0",
        "market_session_id": session_id,
        "master_sha256": master["master_sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
        "frontier_sha256": frontier["frontier_sha256"],
        **progress,
    }
    progress_value = {**progress_body, "progress_sha256": semantic_hash(progress_body)}
    progress_path = rt.REPO_ROOT / f"data/v42/market/progress/{session_id}.json"
    write_json(progress_path, progress_value)

    pointer_body = {
        "schema_version": "2.0.0",
        "kind": "qrgf_v42_market_pointer",
        "architecture_version": "4.2.0",
        "ordinary_daily_broad_allowed": True,
        "market_session_id": session_id,
        "source_market_session_id": market_session,
        "source_snapshot_id": system.get("snapshot_id"),
        "master_sha256": master["master_sha256"],
        "campaign_state_sha256": state["state_sha256"],
        "manifest_path": (session_root / "manifest.json").relative_to(rt.REPO_ROOT).as_posix(),
        "manifest_sha256": manifest["manifest_sha256"],
        "core_path": (session_root / "core.json").relative_to(rt.REPO_ROOT).as_posix(),
        "core_view_sha256": core["view_sha256"],
        "frontier_path": frontier_path.relative_to(rt.REPO_ROOT).as_posix(),
        "frontier_sha256": frontier["frontier_sha256"],
        "progress_path": progress_path.relative_to(rt.REPO_ROOT).as_posix(),
        "progress_sha256": progress_value["progress_sha256"],
        "next_page_index": progress.get("next_page_index"),
        "next_cursor": progress.get("next_cursor"),
        "universe_transport_complete": progress.get("universe_transport_complete") is True,
        "competitive_unresolved_count": frontier["competitive_unresolved_count"],
        "total_eligible_challengers": manifest["total_eligible_challengers"],
        "page_size": manifest["page_size"],
        "page_count": manifest["page_count"],
        "producer_release_sha256": release_sha,
    }
    pointer = {**pointer_body, "pointer_sha256": semantic_hash(pointer_body)}
    write_json(rt.REPO_ROOT / "data/v42/market/latest.json", pointer)
    print(json.dumps({
        "status": "published",
        "market_session_id": session_id,
        "core_rows": len(core["rows"]),
        "challengers": manifest["total_eligible_challengers"],
        "page_count": manifest["page_count"],
        "next_page_index": progress.get("next_page_index"),
        "competitive_unresolved_count": frontier["competitive_unresolved_count"],
        "pointer_sha256": pointer["pointer_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
