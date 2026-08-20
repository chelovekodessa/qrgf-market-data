#!/usr/bin/env python3
"""V4 deterministic quality-first selection.

Structural Quality is the primary ordering dimension. Recovery can only reorder
candidates inside the same Quality tier; it can never compensate across tiers.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable, Mapping

from common import clamp, load_policy, number

_CLASS_SUFFIXES = (
    re.compile(r"\s*[-–—]\s*class\s+[a-z0-9-]+\s+(?:common\s+|capital\s+)?(?:stock|shares?)\s*$", re.I),
    re.compile(r"\s*[-–—]\s*class\s+[a-z0-9-]+\s+ordinary\s+shares?\s*$", re.I),
    re.compile(r"\s+class\s+[a-z0-9-]+\s+(?:common\s+|capital\s+)?(?:stock|shares?)\s*$", re.I),
)


def identity(row: Mapping[str, Any]) -> tuple[str, str]:
    ticker = str(row.get("ticker") or "").strip().upper()
    contract = str(row.get("contract_id") or "").strip()
    if not ticker or not contract:
        raise ValueError("candidate requires ticker and contract_id")
    return ticker, contract


def _clean_company(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for pattern in _CLASS_SUFFIXES:
        stripped = pattern.sub("", text).strip(" -–—")
        if stripped != text:
            return re.sub(r"[^A-Z0-9]+", " ", stripped.upper()).strip()
    return ""


def derive_issuer_id(row: Mapping[str, Any]) -> str:
    explicit = str(row.get("issuer_id") or "").strip()
    if explicit:
        return explicit if explicit.startswith(("ISSUER:", "CIK:", "NAMECLASS:", "SECURITY:")) else f"ISSUER:{explicit.upper()}"
    cik = str(row.get("issuer_cik") or row.get("cik") or "").strip().lstrip("0")
    if cik.isdigit():
        return f"CIK:{int(cik):010d}"
    company = _clean_company(row.get("company_name") or row.get("company"))
    if company:
        return f"NAMECLASS:{company}"
    ticker, contract = identity(row)
    return f"SECURITY:{ticker}:{contract}"


def security_overlay(row: Mapping[str, Any]) -> str:
    security_type = str(row.get("security_type") or "unknown").strip().lower()
    if security_type == "adr":
        return "adr"
    if security_type == "etf":
        return "etf"
    return "common_equity"


def issuer_research_key(row: Mapping[str, Any]) -> str:
    return f"{derive_issuer_id(row)}|{security_overlay(row)}"


def normalize_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["ticker"] = str(value.get("ticker") or "").strip().upper()
    value["issuer_id"] = derive_issuer_id(value)
    value["security_overlay"] = security_overlay(value)
    value["research_scope_key"] = f"{value['issuer_id']}|{value['security_overlay']}"
    setup = number(value.get("recovery_setup_score"))
    if setup is None:
        setup = number(value.get("l2_setup_score"))
    confidence = number(value.get("evidence_confidence_pct"))
    if confidence is None:
        confidence = number(value.get("l2_confidence_pct"))
    value["recovery_setup_score"] = round(clamp(setup), 4) if setup is not None else None
    value["evidence_confidence_pct"] = round(clamp(confidence), 4) if confidence is not None else None
    return value


def quality_tier(score: Any) -> str | None:
    numeric = number(score)
    if numeric is None:
        return None
    tiers = load_policy()["selection"]["quality_tiers"]
    for name in ("A", "B", "C", "D"):
        low, high = tiers[name]
        if float(low) <= numeric <= float(high):
            return name
    return None


def _tier_rank(tier: str | None) -> int:
    return {"A": 0, "B": 1, "C": 2, "D": 3}.get(tier or "", 99)


def _confidence(row: Mapping[str, Any], stage: str) -> float | None:
    values: list[float | None] = [number(row.get("evidence_confidence_pct"))]
    values.append(number(row.get("l3_coverage_pct")))
    if stage == "L4":
        values.append(number(row.get("l4_coverage_pct")))
    if any(value is None for value in values):
        return None
    return round(min(clamp(float(value)) for value in values if value is not None), 4)


def eligibility(stage: str, row: Mapping[str, Any]) -> tuple[bool, str]:
    policy = load_policy()
    if row.get("hard_vetoes"):
        return False, "hard_veto"
    if stage == "L3":
        if row.get("l3_status") not in {"pass", "conditional"}:
            return False, f"l3_status:{row.get('l3_status')}"
        if row.get("fundamental_eligible") is not True:
            return False, "fundamental_not_eligible"
        score, coverage = number(row.get("l3_score")), number(row.get("l3_coverage_pct"))
        if score is None or score < float(policy["l3"]["minimum_quality_score"]):
            return False, "below_l3_quality_floor"
        if coverage is None or coverage < float(policy["l3"]["minimum_coverage_pct"]):
            return False, "below_l3_coverage_floor"
        if number(row.get("recovery_setup_score")) is None:
            return False, "missing_recovery_setup"
    elif stage == "L4":
        if row.get("l4_status") not in {"pass", "conditional"}:
            return False, f"l4_status:{row.get('l4_status')}"
        l4_score, l4_cov = number(row.get("l4_score")), number(row.get("l4_coverage_pct"))
        l3_score = number(row.get("l3_score"))
        if l4_score is None or l4_score < float(policy["l4"]["minimum_score"]):
            return False, "below_l4_recovery_floor"
        if l4_cov is None or l4_cov < float(policy["l4"]["minimum_coverage_pct"]):
            return False, "below_l4_coverage_floor"
        if l3_score is None or l3_score < float(policy["l3"]["minimum_quality_score"]):
            return False, "lost_l3_quality_gate"
        if number(row.get("recovery_setup_score")) is None:
            return False, "missing_recovery_setup"
    else:
        raise ValueError(f"unsupported selection stage: {stage}")
    return True, "eligible"


def _within_tier_score(stage: str, row: Mapping[str, Any]) -> float | None:
    q = number(row.get("l3_score")); setup = number(row.get("recovery_setup_score")); conf = _confidence(row, stage)
    if q is None or setup is None or conf is None:
        return None
    if stage == "L3":
        return round(0.60 * clamp(setup) + 0.25 * clamp(q) + 0.15 * clamp(conf), 4)
    thesis = number(row.get("l4_score"))
    if thesis is None:
        return None
    return round(0.50 * clamp(thesis) + 0.20 * clamp(setup) + 0.20 * clamp(q) + 0.10 * clamp(conf), 4)


def progression(stage: str, row: Mapping[str, Any]) -> dict[str, Any]:
    prefix = stage.lower()
    tier = quality_tier(row.get("l3_score"))
    return {
        f"{prefix}_quality_tier": tier,
        f"{prefix}_within_tier_score": _within_tier_score(stage, row),
        f"{prefix}_selection_model_version": load_policy()["selection"]["model_version"],
        f"{prefix}_selection_confidence_pct": _confidence(row, stage),
    }


def _sort_key(stage: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    prefix = stage.lower()
    tier = row.get(f"{prefix}_quality_tier") or quality_tier(row.get("l3_score"))
    within = number(row.get(f"{prefix}_within_tier_score"))
    quality = number(row.get("l3_score"))
    setup = number(row.get("recovery_setup_score"))
    confidence = number(row.get(f"{prefix}_selection_confidence_pct"))
    thesis = number(row.get("l4_score")) if stage == "L4" else None
    liquidity = number(row.get("avg_dollar_volume"))
    ticker, contract = identity(row)
    # Tier is always first. No numerical score may compensate across tiers.
    if stage == "L4":
        return (_tier_rank(str(tier)), -(thesis if thesis is not None else -1), -(setup if setup is not None else -1), -(quality if quality is not None else -1), -(confidence if confidence is not None else -1), -(within if within is not None else -1), -(liquidity if liquidity is not None else -1), ticker, contract)
    return (_tier_rank(str(tier)), -(setup if setup is not None else -1), -(quality if quality is not None else -1), -(confidence if confidence is not None else -1), -(within if within is not None else -1), -(liquidity if liquidity is not None else -1), ticker, contract)


def representative_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    normalized = normalize_candidate(row)
    setup = number(normalized.get("recovery_setup_score")); liquidity = number(normalized.get("avg_dollar_volume"))
    ticker, contract = identity(normalized)
    return (-(setup if setup is not None else -1), -(liquidity if liquidity is not None else -1), ticker, contract)


def l3_research_units(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[tuple[str, str]]]]:
    normalized = [normalize_candidate(row) for row in rows]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in normalized:
        groups.setdefault(str(row["research_scope_key"]), []).append(row)
    representatives: list[dict[str, Any]] = []
    members: dict[str, list[tuple[str, str]]] = {}
    for key, group in sorted(groups.items()):
        ordered = sorted(group, key=representative_key)
        representatives.append(ordered[0])
        members[key] = sorted(identity(row) for row in group)
    representatives.sort(key=identity)
    return representatives, members


def select(stage: str, rows: Iterable[Mapping[str, Any]], ceiling: int) -> dict[str, Any]:
    prepared: list[dict[str, Any]] = []
    for raw in rows:
        row = normalize_candidate(raw)
        row.update(progression(stage, row))
        ok, reason = eligibility(stage, row)
        row[f"{stage.lower()}_selection_eligible"] = ok
        row[f"{stage.lower()}_selection_reason"] = reason
        prepared.append(row)
    eligible = sorted((r for r in prepared if r[f"{stage.lower()}_selection_eligible"]), key=lambda r: _sort_key(stage, r))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in eligible:
        issuer = str(row.get("issuer_id") or "")
        if load_policy()["selection"]["issuer_dedup"]["enabled"] and issuer in seen:
            row[f"{stage.lower()}_selection_reason"] = "duplicate_issuer_lower_rank"
            continue
        if len(selected) >= int(ceiling):
            row[f"{stage.lower()}_selection_reason"] = "below_global_cutoff"
            continue
        selected.append(row); seen.add(issuer)
        row[f"{stage.lower()}_selection_reason"] = "selected"
        row[f"{stage.lower()}_selected_rank"] = len(selected)
    for i, row in enumerate(eligible, 1):
        row[f"{stage.lower()}_global_rank"] = i
    prefix = stage.lower()
    selected_ids = {identity(r) for r in selected}
    excluded = [r for r in eligible if identity(r) not in selected_ids and r.get(f"{prefix}_selection_reason") != "duplicate_issuer_lower_rank"]
    sector_counts = Counter(str(r.get("sector") or "unknown") for r in selected)
    diagnostics = {
        "model_version": load_policy()["selection"]["model_version"],
        "quality_tier_primary": True,
        "candidate_count": len(prepared),
        "eligible_count": len(eligible),
        "selected_count": len(selected),
        "tier_counts": dict(Counter(str(r.get(f"{prefix}_quality_tier") or "unknown") for r in selected)),
        "sector_counts": dict(sorted(sector_counts.items())),
        "worst_selected": _diag(selected[-1], stage) if selected else None,
        "best_excluded": _diag(excluded[0], stage) if excluded else None,
    }
    by_id = {identity(r): r for r in prepared}
    for r in eligible: by_id[identity(r)] = r
    return {"rows": [by_id[identity(r)] for r in prepared], "selected": selected, "diagnostics": diagnostics}


def _diag(row: Mapping[str, Any] | None, stage: str) -> dict[str, Any] | None:
    if row is None: return None
    prefix = stage.lower(); ticker, contract = identity(row)
    return {
        "ticker": ticker, "contract_id": contract, "issuer_id": row.get("issuer_id"),
        "quality_tier": row.get(f"{prefix}_quality_tier"), "quality_score": row.get("l3_score"),
        "recovery_setup_score": row.get("recovery_setup_score"), "recovery_thesis_score": row.get("l4_score") if stage == "L4" else None,
        "within_tier_score": row.get(f"{prefix}_within_tier_score"), "reason": row.get(f"{prefix}_selection_reason"),
    }
