#!/usr/bin/env python3
"""V4.2 daily market session, transport cursor and competitive frontier.

A 250-row page is a transport unit, not a universe cutoff.  Every row must
receive a durable triage disposition before the cursor advances, while rows
that still require Structural Quality research remain in a separate durable
competitive frontier.  `insufficient_data` is never treated as a resolved
competitive result.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from common import ensure, load_policy, semantic_hash
import bootstrap, campaign, selection

PAGE_SIZE = 250
ORDERING_MODEL = "l2_setup_confidence_prior_growth_liquidity_identity-v2"
PAGE_KIND = "qrgf_v42_challenger_page"
MANIFEST_KIND = "qrgf_v42_market_session_manifest"
BLOCKED_KIND = "qrgf_v42_market_session_blocked"
TRIAGE_KIND = "qrgf_v42_challenger_triage"
FRONTIER_KIND = "qrgf_v42_competitive_frontier"

SAFE_EXCLUSION_REASONS = frozenset({
    "instrument_ineligible",
    "liquidity_below_floor",
    "history_insufficient",
    "confirmed_hard_veto",
    "fresh_registry_rejected",
    "mathematical_upper_bound_below_cutoff",
})
TRIAGE_DISPOSITIONS = frozenset({"safe_exclusion", "resolved_candidate", "deep_research_required", "deferred_unknown"})


def _self(value: Mapping[str, Any], field: str, label: str) -> dict[str, Any]:
    v = dict(value)
    body = {k: x for k, x in v.items() if k != field}
    ensure(v.get(field) == semantic_hash(body), f"{label} self hash mismatch")
    return v


def _require_hash(value: Any, label: str) -> str:
    text = str(value or "")
    ensure(len(text) == 64 and all(ch in "0123456789abcdef" for ch in text), f"{label} must be a lowercase SHA-256")
    return text


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any, default: int = -1) -> int:
    return int(value) if value is not None else default


def _scope(raw: Mapping[str, Any]) -> str:
    explicit = str(raw.get("research_scope_key") or "").strip()
    if explicit:
        return explicit
    issuer = str(raw.get("issuer_id") or "").strip()
    overlay = str(raw.get("security_overlay") or "common_equity").strip()
    if issuer:
        return f"{issuer}|{overlay}"
    ticker = str(raw.get("ticker") or "").upper().strip()
    contract = str(raw.get("contract_id") or "").strip()
    ensure(ticker or contract, "challenger has no deterministic identity")
    return f"security:{contract or ticker}"


def _key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    setup = _number(row.get("l2_setup_score"))
    confidence = _number(row.get("l2_confidence_pct"))
    growth = _number(row.get("setup_prior_growth"))
    liquidity = _number(row.get("avg_dollar_volume"))
    return (
        -(setup if setup is not None else -1),
        -(confidence if confidence is not None else -1),
        -(growth if growth is not None else -1),
        -(liquidity if liquidity is not None else -1),
        str(row.get("ticker") or "").upper(),
        str(row.get("contract_id") or ""),
        _scope(row),
    )


def _project(raw: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
    keep = (
        "ticker", "company", "contract_id", "issuer_id", "security_overlay", "security_type", "instrument_status", "exchange",
        "sector", "industry", "current_price", "reference_52w_high", "market_cap", "avg_dollar_volume", "return_1m_pct",
        "return_3m_pct", "return_6m_pct", "return_12m_pct", "drawdown_pct", "historical_volatility_pct", "trading_history_days",
        "momentum_history_status", "data_integrity_status", "as_of", "l2_status", "l2_setup_score", "l2_confidence_pct",
        "setup_prior_growth", "setup_pullback_geometry", "setup_liquidity", "setup_data_completeness",
    )
    value = {key: raw.get(key) for key in keep}
    value.update({"research_scope_key": _scope(raw), "transport_ordinal": ordinal})
    body = dict(value)
    value["challenger_row_sha256"] = semantic_hash(body)
    return value


def _page(session_id: str, master_sha256: str, page_index: int, start: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    body = {
        "schema_version": "2.0.0",
        "kind": PAGE_KIND,
        "architecture_version": bootstrap.ARCHITECTURE_VERSION,
        "market_session_id": session_id,
        "master_sha256": master_sha256,
        "ordering_model": ORDERING_MODEL,
        "page_index": page_index,
        "cursor_start": start,
        "cursor_end_exclusive": start + len(rows),
        "row_count": len(rows),
        "page_size": PAGE_SIZE,
        "transport_is_not_quality_whitelist": True,
        "exhaustive_full_market_top30_claim_authorized": False,
        "rows": rows,
    }
    return {**body, "page_sha256": semantic_hash(body)}


def validate_challenger_page(value: Mapping[str, Any]) -> dict[str, Any]:
    v = _self(value, "page_sha256", "challenger page")
    ensure(v.get("schema_version") == "2.0.0" and v.get("kind") == PAGE_KIND, "invalid V4.2 challenger page")
    ensure(v.get("architecture_version") == bootstrap.ARCHITECTURE_VERSION, "challenger page architecture mismatch")
    rows = v.get("rows")
    ensure(isinstance(rows, list) and 0 < len(rows) <= PAGE_SIZE, "challenger page count invalid")
    ensure(v.get("ordering_model") == ORDERING_MODEL and v.get("page_size") == PAGE_SIZE and v.get("transport_is_not_quality_whitelist") is True, "challenger page transport contract invalid")
    ensure(_integer(v.get("row_count")) == len(rows), "challenger page row count mismatch")
    ensure(_integer(v.get("cursor_end_exclusive")) - _integer(v.get("cursor_start")) == len(rows), "challenger page cursor mismatch")
    scopes: list[str] = []
    for raw in rows:
        ensure(isinstance(raw, Mapping), "challenger page row invalid")
        row = dict(raw)
        row_hash = row.pop("challenger_row_sha256", None)
        ensure(row_hash == semantic_hash(row), "challenger row hash mismatch")
        scopes.append(str(raw.get("research_scope_key") or ""))
    ensure(all(scopes) and len(set(scopes)) == len(scopes), "challenger page duplicate scopes")
    return v


def validate_manifest(value: Mapping[str, Any], *, pages: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    v = _self(value, "manifest_sha256", "market session manifest")
    ensure(v.get("schema_version") == "2.0.0" and v.get("kind") == MANIFEST_KIND, "invalid V4.2 market session manifest")
    ensure(v.get("architecture_version") == bootstrap.ARCHITECTURE_VERSION, "market session architecture mismatch")
    ensure(v.get("ordering_model") == ORDERING_MODEL and _integer(v.get("page_size")) == PAGE_SIZE, "market session ordering or page size mismatch")
    ensure(v.get("ordinary_daily_broad_allowed") is True, "ordinary market session published before campaign COMPLETE")
    entries = v.get("pages")
    ensure(isinstance(entries, list) and _integer(v.get("page_count")) == len(entries), "market session page count mismatch")
    expected_start = 0
    all_scopes: set[str] = set()
    for index, entry in enumerate(entries):
        ensure(_integer(entry.get("page_index")) == index and _integer(entry.get("cursor_start")) == expected_start, "market session cursor is not monotonic")
        ensure(index in pages, "market session page missing")
        page = validate_challenger_page(pages[index])
        ensure(page["page_sha256"] == entry.get("page_sha256") and page["market_session_id"] == v["market_session_id"], "market session page hash mismatch")
        ensure(page["master_sha256"] == v["master_sha256"], "market session MASTER mismatch")
        scopes = {str(row["research_scope_key"]) for row in page["rows"]}
        ensure(not all_scopes.intersection(scopes), "market session pages overlap")
        all_scopes.update(scopes)
        expected_start = int(page["cursor_end_exclusive"])
    ensure(expected_start == _integer(v.get("total_eligible_challengers")) and len(all_scopes) == expected_start, "market session pages omit challengers")
    return v


def build_market_session(rows: Iterable[Mapping[str, Any]], *, bundle_value: Mapping[str, Any],
                         state_value: Mapping[str, Any], source_snapshot_id: str,
                         source_market_session_id: str | None = None) -> dict[str, Any]:
    bundle = bootstrap.validate_master_bundle(bundle_value)
    state = campaign.validate_state(state_value, bundle=bundle)
    master = bundle["master"]
    source_market_session = str(source_market_session_id or master["market_session_id"])
    ensure(source_market_session, "source market session id missing")
    if state["daily_broad_allowed"] is not True:
        body = {
            "schema_version": "2.0.0",
            "kind": BLOCKED_KIND,
            "architecture_version": bootstrap.ARCHITECTURE_VERSION,
            "market_session_id": source_market_session,
            "master_sha256": master["master_sha256"],
            "campaign_state_sha256": state["state_sha256"],
            "campaign_phase": state["phase"],
            "ordinary_daily_broad_allowed": False,
            "reason": "MASTER_CORE500_NOT_COMPLETE",
        }
        return {**body, "diagnostic_sha256": semantic_hash(body)}
    core_scopes = {str(scope["research_scope_key"]) for scope in master["scopes"]}
    grouped: dict[str, Mapping[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping) or raw.get("instrument_status") != "eligible":
            continue
        if str(raw.get("l2_status") or "") not in {"pass", "conditional", "recheck"}:
            continue
        key = _scope(raw)
        if key in core_scopes:
            continue
        previous = grouped.get(key)
        if previous is None or _key(raw) < _key(previous):
            grouped[key] = raw
    ordered = sorted(grouped.values(), key=_key)
    material = [_project(raw, index) for index, raw in enumerate(ordered)]
    session_seed = {
        "market_session_id": source_market_session,
        "source_snapshot_id": str(source_snapshot_id),
        "master_sha256": master["master_sha256"],
        "challenger_scope_keys": [x["research_scope_key"] for x in material],
    }
    session_id = f"{source_market_session}-{semantic_hash(session_seed)[:16]}"
    pages: dict[int, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    for index, start in enumerate(range(0, len(material), PAGE_SIZE)):
        page = _page(session_id, master["master_sha256"], index, start, material[start:start + PAGE_SIZE])
        pages[index] = page
        entries.append({
            "page_index": index,
            "path": f"challengers/page-{index:04d}.json",
            "page_sha256": page["page_sha256"],
            "cursor_start": start,
            "cursor_end_exclusive": start + len(page["rows"]),
            "row_count": len(page["rows"]),
        })
    body = {
        "schema_version": "2.0.0",
        "kind": MANIFEST_KIND,
        "architecture_version": bootstrap.ARCHITECTURE_VERSION,
        "market_session_id": session_id,
        "source_market_session_id": source_market_session,
        "source_snapshot_id": str(source_snapshot_id),
        "master_sha256": master["master_sha256"],
        "campaign_state_sha256": state["state_sha256"],
        "ordinary_daily_broad_allowed": True,
        "ordering_model": ORDERING_MODEL,
        "page_size": PAGE_SIZE,
        "total_eligible_challengers": len(material),
        "page_count": len(entries),
        "pages": entries,
        "transport_is_not_quality_whitelist": True,
        "competitive_frontier_required": True,
        "exhaustive_full_market_top30_claim_authorized": False,
    }
    manifest = {**body, "manifest_sha256": semantic_hash(body)}
    validate_manifest(manifest, pages=pages)
    return {"manifest": manifest, "pages": pages}


def build_triage(row_value: Mapping[str, Any], *, disposition: str, reason: str,
                 evidence_sha256: str, triaged_at: str) -> dict[str, Any]:
    row = dict(row_value)
    scope = str(row.get("research_scope_key") or "")
    row_hash = _require_hash(row.get("challenger_row_sha256"), "challenger row hash")
    disp = str(disposition)
    reason_text = str(reason)
    ensure(disp in TRIAGE_DISPOSITIONS, "challenger triage disposition invalid")
    _require_hash(evidence_sha256, "challenger triage evidence hash")
    if disp == "safe_exclusion":
        ensure(reason_text in SAFE_EXCLUSION_REASONS, "safe exclusion lacks an approved proven reason")
    else:
        ensure(reason_text not in SAFE_EXCLUSION_REASONS or disp == "resolved_candidate", "challenger triage reason/disposition mismatch")
    body = {
        "schema_version": "1.0.0",
        "kind": TRIAGE_KIND,
        "architecture_version": bootstrap.ARCHITECTURE_VERSION,
        "research_scope_key": scope,
        "challenger_row_sha256": row_hash,
        "transport_ordinal": int(row.get("transport_ordinal") or 0),
        "disposition": disp,
        "reason": reason_text,
        "evidence_sha256": str(evidence_sha256),
        "triaged_at": str(triaged_at),
    }
    value = {**body, "triage_sha256": semantic_hash(body)}
    return validate_triage(value, row=row)


def validate_triage(value: Mapping[str, Any], *, row: Mapping[str, Any]) -> dict[str, Any]:
    v = _self(value, "triage_sha256", "challenger triage")
    ensure(v.get("schema_version") == "1.0.0" and v.get("kind") == TRIAGE_KIND, "invalid V4.2 challenger triage")
    ensure(v.get("architecture_version") == bootstrap.ARCHITECTURE_VERSION, "challenger triage architecture mismatch")
    ensure(v.get("research_scope_key") == row.get("research_scope_key") and v.get("challenger_row_sha256") == row.get("challenger_row_sha256"), "challenger triage row binding mismatch")
    ensure(int(v.get("transport_ordinal") or -1) == int(row.get("transport_ordinal") or -1), "challenger triage ordinal mismatch")
    disp = str(v.get("disposition") or "")
    reason = str(v.get("reason") or "")
    ensure(disp in TRIAGE_DISPOSITIONS, "challenger triage disposition invalid")
    _require_hash(v.get("evidence_sha256"), "challenger triage evidence hash")
    if disp == "safe_exclusion":
        ensure(reason in SAFE_EXCLUSION_REASONS, "safe exclusion lacks an approved proven reason")
    return v


def _all_rows(pages: Mapping[int, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for index in sorted(pages):
        page = validate_challenger_page(pages[index])
        for row in page["rows"]:
            key = str(row["research_scope_key"])
            ensure(key not in output, "duplicate challenger scope across pages")
            output[key] = dict(row)
    return output


def _registry_disposition(record: Mapping[str, Any], *, market_session: str) -> tuple[str, str] | None:
    if not campaign._durable_record(record, market_session_id=market_session):
        return None
    status = str(record.get("quality_status") or "")
    if status == "rejected":
        return "safe_exclusion", "fresh_registry_rejected"
    if status in {"pass", "conditional"}:
        return "resolved_candidate", f"fresh_registry_{status}"
    if status == "insufficient_data":
        return "deferred_unknown", "fresh_registry_insufficient_data"
    return None


def _effective_triage(manifest: Mapping[str, Any], pages: Mapping[int, Mapping[str, Any]],
                      durable_by_scope: Mapping[str, Mapping[str, Any]],
                      triage_by_scope: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    rows = _all_rows(pages)
    session = str(manifest["source_market_session_id"])
    output: dict[str, dict[str, Any]] = {}
    for key, row in rows.items():
        registry_record = durable_by_scope.get(key)
        if isinstance(registry_record, Mapping):
            derived = _registry_disposition(registry_record, market_session=session)
            if derived is not None:
                disposition, reason = derived
                evidence = semantic_hash({
                    "receipt_sha256": registry_record.get("receipt_sha256"),
                    "passport_hash": registry_record.get("passport_hash"),
                    "entry_sha256": registry_record.get("entry_sha256"),
                    "quality_status": registry_record.get("quality_status"),
                })
                output[key] = build_triage(row, disposition=disposition, reason=reason, evidence_sha256=evidence, triaged_at=str(registry_record.get("triaged_at") or registry_record.get("event_scan_through") or ""))
                continue
        raw = triage_by_scope.get(key)
        if isinstance(raw, Mapping):
            output[key] = validate_triage(raw, row=row)
    return output


def build_frontier(manifest_value: Mapping[str, Any], *, pages: Mapping[int, Mapping[str, Any]],
                   durable_by_scope: Mapping[str, Mapping[str, Any]],
                   triage_by_scope: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    manifest = validate_manifest(manifest_value, pages=pages)
    rows = _all_rows(pages)
    triage = _effective_triage(manifest, pages, durable_by_scope, triage_by_scope)
    frontier_rows: list[dict[str, Any]] = []
    resolved_count = 0
    safe_exclusion_count = 0
    for key, row in rows.items():
        item = triage.get(key)
        if item is None:
            frontier_rows.append({**row, "frontier_reason": "triage_missing", "triage_sha256": None})
            continue
        disposition = str(item["disposition"])
        if disposition == "safe_exclusion":
            safe_exclusion_count += 1
        elif disposition == "resolved_candidate":
            resolved_count += 1
        else:
            frontier_rows.append({**row, "frontier_reason": item["reason"], "triage_sha256": item["triage_sha256"]})
    frontier_rows.sort(key=lambda row: int(row.get("transport_ordinal") or 0))
    body = {
        "schema_version": "2.0.0",
        "kind": FRONTIER_KIND,
        "architecture_version": bootstrap.ARCHITECTURE_VERSION,
        "market_session_id": manifest["market_session_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "master_sha256": manifest["master_sha256"],
        "total_eligible_challengers": manifest["total_eligible_challengers"],
        "triaged_scope_count": len(triage),
        "safe_exclusion_count": safe_exclusion_count,
        "resolved_candidate_count": resolved_count,
        "competitive_unresolved_count": len(frontier_rows),
        "frontier_rows": frontier_rows,
    }
    value = {**body, "frontier_sha256": semantic_hash(body)}
    return validate_frontier(value, manifest=manifest, pages=pages)


def validate_frontier(value: Mapping[str, Any], *, manifest: Mapping[str, Any], pages: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    v = _self(value, "frontier_sha256", "competitive frontier")
    ensure(v.get("schema_version") == "2.0.0" and v.get("kind") == FRONTIER_KIND, "invalid V4.2 competitive frontier")
    ensure(v.get("architecture_version") == bootstrap.ARCHITECTURE_VERSION, "competitive frontier architecture mismatch")
    ensure(v.get("market_session_id") == manifest["market_session_id"] and v.get("manifest_sha256") == manifest["manifest_sha256"] and v.get("master_sha256") == manifest["master_sha256"], "competitive frontier session binding mismatch")
    rows = v.get("frontier_rows")
    ensure(isinstance(rows, list) and int(v.get("competitive_unresolved_count", -1)) == len(rows), "competitive frontier count mismatch")
    all_rows = _all_rows(pages)
    keys = [str(row.get("research_scope_key") or "") for row in rows]
    ensure(all(keys) and len(set(keys)) == len(keys) and set(keys).issubset(all_rows), "competitive frontier contains invalid scopes")
    total = int(v.get("triaged_scope_count") or 0) + sum(str(row.get("frontier_reason")) == "triage_missing" for row in rows)
    ensure(total >= len(rows), "competitive frontier accounting invalid")
    return v


def next_page(manifest_value: Mapping[str, Any], *, pages: Mapping[int, Mapping[str, Any]],
              durable_by_scope: Mapping[str, Mapping[str, Any]],
              triage_by_scope: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    manifest = validate_manifest(manifest_value, pages=pages)
    triage = _effective_triage(manifest, pages, durable_by_scope, triage_by_scope)
    for index in range(int(manifest["page_count"])):
        page = validate_challenger_page(pages[index])
        pending = [row for row in page["rows"] if str(row["research_scope_key"]) not in triage]
        if pending:
            entry = manifest["pages"][index]
            return {
                "market_session_id": manifest["market_session_id"],
                "universe_transport_complete": False,
                "next_page_index": index,
                "next_cursor": entry["cursor_start"],
                "page_sha256": entry["page_sha256"],
                "page_triaged_scope_count": len(page["rows"]) - len(pending),
                "page_pending_triage_count": len(pending),
                "next_research_scope_key": pending[0]["research_scope_key"],
            }
    frontier = build_frontier(manifest, pages=pages, durable_by_scope=durable_by_scope, triage_by_scope=triage_by_scope)
    return {
        "market_session_id": manifest["market_session_id"],
        "universe_transport_complete": True,
        "next_page_index": None,
        "next_cursor": None,
        "triaged_scope_count": len(triage),
        "total_eligible_challengers": manifest["total_eligible_challengers"],
        "competitive_unresolved_count": frontier["competitive_unresolved_count"],
        "frontier_sha256": frontier["frontier_sha256"],
    }


def next_deep_scopes(frontier_value: Mapping[str, Any], *, manifest: Mapping[str, Any],
                     pages: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
    frontier = validate_frontier(frontier_value, manifest=manifest, pages=pages)
    wave = int(load_policy()["challenger_lane"]["deep_research_wave_size"])
    return [dict(row) for row in frontier["frontier_rows"][:wave]]


def production_claim(manifest_value: Mapping[str, Any], *, pages: Mapping[int, Mapping[str, Any]],
                     durable_by_scope: Mapping[str, Mapping[str, Any]],
                     triage_by_scope: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    manifest = validate_manifest(manifest_value, pages=pages)
    progress = next_page(manifest, pages=pages, durable_by_scope=durable_by_scope, triage_by_scope=triage_by_scope)
    frontier = build_frontier(manifest, pages=pages, durable_by_scope=durable_by_scope, triage_by_scope=triage_by_scope)
    authorized = progress["universe_transport_complete"] is True and int(frontier["competitive_unresolved_count"]) == 0
    return {
        "market_session_id": manifest["market_session_id"],
        "master_sha256": manifest["master_sha256"],
        "total_eligible_challengers": manifest["total_eligible_challengers"],
        "triaged_scope_count": frontier["triaged_scope_count"],
        "competitive_unresolved_count": frontier["competitive_unresolved_count"],
        "universe_transport_complete": progress["universe_transport_complete"],
        "frontier_sha256": frontier["frontier_sha256"],
        "exhaustive_full_market_top30_claim_authorized": authorized,
        "selection_scope": "full_market_competitive_frontier_resolved" if authorized else "core_registry_plus_competitive_frontier",
    }


def exact_core_rows(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = value.get("rows") if isinstance(value, Mapping) else None
    ensure(isinstance(rows, list), "core market rows missing")
    out = []
    for raw in rows:
        if raw.get("quality_reusable") is True and str(raw.get("quality_status") or "") in {"pass", "conditional"}:
            row = dict(raw)
            row.update({
                "l3_status": row["quality_status"],
                "l3_score": row.get("quality_score"),
                "l3_coverage_pct": row.get("quality_coverage_pct"),
                "fundamental_eligible": row.get("quality_eligible") is True,
                "hard_vetoes": [],
            })
            out.append(selection.normalize_candidate(row))
    return out
