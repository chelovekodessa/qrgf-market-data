#!/usr/bin/env python3
"""Normalize evidence and bind analytical facts to their sources."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from common import get_path, load_policy, number, parse_time, semantic_hash

QUALITY_VALUES = {"verified", "partial", "conflict", "stale", "unverified"}


def normalize(records: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(records, list):
        return [], ["evidence must be an array"]
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    allowed_source_types = set(load_policy()["evidence"]["source_priority"])
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            errors.append(f"evidence[{index}] must be an object")
            continue
        field = str(raw.get("field") or "").strip()
        source = raw.get("source") if isinstance(raw.get("source"), Mapping) else {}
        source_type = str(source.get("source_type") or "").strip()
        source_id = str(source.get("source_id") or "").strip()
        as_of = raw.get("as_of")
        retrieved_at = raw.get("retrieved_at")
        quality = str(raw.get("quality_status") or "verified").strip().lower()
        if not field or not source_type or not source_id:
            errors.append(f"evidence[{index}] lacks field or source identity")
            continue
        if source_type not in allowed_source_types:
            errors.append(f"evidence[{index}] has unknown source_type: {source_type}")
            continue
        try:
            parse_time(as_of)
            parse_time(retrieved_at)
        except ValueError as exc:
            errors.append(f"evidence[{index}]: {exc}")
            continue
        if as_of in (None, "") or retrieved_at in (None, ""):
            errors.append(f"evidence[{index}] lacks as_of or retrieved_at")
            continue
        if quality not in QUALITY_VALUES:
            errors.append(f"evidence[{index}] has unknown quality_status")
            continue
        row = dict(raw)
        row["field"] = field
        row["source"] = dict(source)
        row["quality_status"] = quality
        transport = str(source.get("transport") or source.get("transport_type") or "").strip().lower()
        provider = str(source.get("provider") or "").strip().lower()
        metricduck_claimed = transport == "metricduck_sec_filing" or provider == "metricduck" or "metricduck" in source_id.lower()
        if metricduck_claimed:
            required_lineage = ("accession_or_edgar_handle", "period", "filing_lineage")
            missing_lineage = [name for name in required_lineage if not source.get(name)]
            if missing_lineage:
                errors.append(f"evidence[{index}] MetricDuck lineage missing: {','.join(missing_lineage)}")
                continue
            if source_type != "official_filing":
                errors.append(f"evidence[{index}] MetricDuck source identity must remain official_filing")
                continue
            if transport != "metricduck_sec_filing" or provider != "metricduck":
                errors.append(f"evidence[{index}] MetricDuck provider and transport markers are required")
                continue
        if field.startswith("facts.") and number(row.get("value")) is not None:
            if not row.get("period"):
                errors.append(f"evidence[{index}] numerical fact lacks period")
                continue
            if not row.get("unit"):
                errors.append(f"evidence[{index}] numerical fact lacks unit")
                continue
            if field.rsplit(".", 1)[-1].endswith("_score"):
                rubric = row.get("rubric")
                criteria = rubric.get("criteria") if isinstance(rubric, Mapping) else None
                if not isinstance(rubric, Mapping) or not rubric.get("method_version") or not isinstance(criteria, (list, dict)) or len(criteria) < 2:
                    errors.append(f"evidence[{index}] qualitative score lacks a multi-criterion rubric")
                    continue
                values = list(criteria.values()) if isinstance(criteria, Mapping) else criteria
                criterion_scores = [number(item.get("score") if isinstance(item, Mapping) else item) for item in values]
                if any(score is None for score in criterion_scores):
                    errors.append(f"evidence[{index}] qualitative rubric has a non-numeric criterion")
                    continue
                rubric_score = sum(float(score) for score in criterion_scores if score is not None) / len(criterion_scores)
                if abs(rubric_score - float(row["value"])) > 0.51:
                    errors.append(f"evidence[{index}] qualitative score does not match rubric")
                    continue
        ident = str(raw.get("evidence_id") or semantic_hash({k: row.get(k) for k in ("field", "value", "source", "as_of")}))
        row["evidence_id"] = ident
        if ident in seen:
            errors.append(f"duplicate evidence_id: {ident}")
            continue
        seen.add(ident)
        normalized.append(row)
    return normalized, errors


def bind(payload: Mapping[str, Any], required_fields: Iterable[str]) -> dict[str, Any]:
    """Require an evidence record equal to every named payload field.

    Critical numerical conflicts at equal source priority fail closed. Input
    order never resolves a conflict and missing never becomes zero or false.
    """
    records, errors = normalize(payload.get("evidence"))
    policy = load_policy()["evidence"]
    priority = {name: index for index, name in enumerate(policy["source_priority"])}
    tolerance = float(policy["critical_numeric_conflict_tolerance_pct"])
    by_field: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cutoff = parse_time(payload.get("research_cutoff_at")) if payload.get("research_cutoff_at") else None
    for row in records:
        observed = parse_time(row.get("as_of"))
        retrieved = parse_time(row.get("retrieved_at"))
        if observed and retrieved and observed > retrieved:
            errors.append(f"evidence after retrieval time: {row['evidence_id']}")
        if cutoff and ((observed and observed > cutoff) or (retrieved and retrieved > cutoff)):
            errors.append(f"evidence exceeds research cutoff: {row['evidence_id']}")
        if row["field"].startswith("clearances.") and str(row.get("value") or "").lower() == "clear":
            scope = row.get("clearance_scope")
            if not isinstance(scope, Mapping) or not isinstance(scope.get("documents"), list) or not scope.get("documents") or not scope.get("period_start") or not scope.get("period_end"):
                errors.append(f"negative clearance lacks defined scope: {row['evidence_id']}")
            else:
                try:
                    period_start = parse_time(scope["period_start"])
                    period_end = parse_time(scope["period_end"])
                    if period_start is None or period_end is None or period_start > period_end:
                        errors.append(f"negative clearance has invalid scope period: {row['evidence_id']}")
                except ValueError:
                    errors.append(f"negative clearance has invalid scope period: {row['evidence_id']}")
        by_field[row["field"]].append(row)
    links: dict[str, list[str]] = {}
    missing: list[str] = []
    conflicts: list[str] = []
    for field in sorted(set(required_fields)):
        expected = get_path(payload, field)
        rows = [row for row in by_field.get(field, []) if row["quality_status"] == "verified"]
        if expected is None or not rows:
            missing.append(field)
            continue
        best_priority = min(priority.get(str(row["source"].get("source_type")), 999) for row in rows)
        peers = [row for row in rows if priority.get(str(row["source"].get("source_type")), 999) == best_priority]
        distinct = {semantic_hash(row.get("value")) for row in peers}
        if len(distinct) > 1:
            conflicts.append(field)
            continue
        matching = [row for row in peers if values_equal(expected, row.get("value"), tolerance)]
        if not matching:
            missing.append(field)
            continue
        links[field] = sorted(row["evidence_id"] for row in matching)
    errors.extend(f"missing or mismatched evidence: {name}" for name in missing)
    errors.extend(f"same-priority evidence conflict: {name}" for name in conflicts)
    return {
        "valid": not errors,
        "records": records,
        "links": links,
        "missing_fields": missing,
        "conflict_fields": conflicts,
        "errors": errors,
    }


def values_equal(left: Any, right: Any, tolerance_pct: float = 0.5) -> bool:
    left_num, right_num = number(left), number(right)
    if left_num is not None and right_num is not None:
        scale = max(abs(left_num), abs(right_num), 1.0)
        return abs(left_num - right_num) / scale * 100.0 <= tolerance_pct
    return left == right
