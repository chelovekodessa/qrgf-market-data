#!/usr/bin/env python3
"""Publish an immutable QRGF L0-L2 funnel snapshot.

The full market stays in GitHub. The snapshot stores full L1/L2 audit artifacts,
but the ChatGPT production transport only needs the manifest and bounded L2
finalist pages.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from qrgf_common import parse_canonical_csv_row


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def semantic_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [parse_canonical_csv_row(row) for row in csv.DictReader(handle)]
    for row in rows:
        if not str(row.get("ticker") or "").strip() or not str(row.get("contract_id") or "").strip():
            raise ValueError("L1 finalist row lacks ticker/contract_id")
    return rows


def identity(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("ticker") or "").strip().upper(), str(row.get("contract_id") or "").strip()


def identity_text(row: dict[str, Any]) -> str:
    key = identity(row)
    return f"{key[0]}:{key[1]}"


# Only fields needed to resume L3 and audit L2 selection cross the ChatGPT
# connector. Full L1/L2 payloads remain immutable audit artifacts in GitHub.
TRANSPORT_FIELDS = (
    "ticker", "company", "contract_id", "security_type", "instrument_status", "exchange", "sector",
    "price", "current_price", "market_cap", "avg_dollar_volume", "return_1m", "return_3m", "return_6m", "return_12m",
    "return_3m_pct", "return_6m_pct", "return_12m_pct", "drawdown_52w", "drawdown_pct",
    "historical_volatility", "historical_volatility_pct", "trading_history_days", "momentum_history_status",
    "as_of", "l1_score", "l1_status", "opportunity_coverage_pct", "l2_status", "entry_readiness",
    "preliminary_timing_status", "research_priority_score", "research_priority_coverage_pct",
    "l2_opportunity_score", "l2_risk_score", "risk_coverage_pct", "risk_flags", "opportunity_flags",
    "hard_vetoes", "checks_missing", "decision_rule_ids", "next_required_check",
    "ruleset_version", "ruleset_hash", "l2_rules_hash", "selected_for_next_stage",
)


def transport_row(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in TRANSPORT_FIELDS if field in row}


def _observed_fixed_holiday(year: int, month: int, day: int) -> dt.date:
    value = dt.date(year, month, day)
    if value.weekday() == 5:
        return value - dt.timedelta(days=1)
    if value.weekday() == 6:
        return value + dt.timedelta(days=1)
    return value


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=delta + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    if month == 12:
        cursor = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        cursor = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return cursor - dt.timedelta(days=(cursor.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> dt.date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def us_equity_market_holidays(year: int) -> set[dt.date]:
    holidays = {
        _observed_fixed_holiday(year, 1, 1),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - dt.timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_fixed_holiday(year, 6, 19),
        _observed_fixed_holiday(year, 7, 4),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_fixed_holiday(year, 12, 25),
    }
    holidays.add(_observed_fixed_holiday(year + 1, 1, 1))
    return holidays


def is_us_equity_market_session(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in us_equity_market_holidays(day.year)


def validate_l1_history_contract(l1: dict[str, Any], l1_snapshot: Path) -> tuple[str, str]:
    if str(l1.get("history_end_semantics") or "") != "max_observed_market_session":
        raise ValueError("L1 history_end semantics are not max_observed_market_session")
    requested_text = str(l1.get("requested_history_end") or "").strip()
    history_text = str(l1.get("history_end") or "").strip()
    if not requested_text or not history_text:
        raise ValueError("L1 requested_history_end/history_end are required")
    requested_end = dt.date.fromisoformat(requested_text)
    history_end = dt.date.fromisoformat(history_text)
    if not is_us_equity_market_session(history_end):
        raise ValueError(f"L1 history_end is not a U.S. equity market session: {history_end}")
    if history_end > requested_end:
        raise ValueError("L1 history_end exceeds requested_history_end")

    observed: list[dt.date] = []
    with l1_snapshot.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            text = str(row.get("as_of") or "").strip()[:10]
            if not text:
                continue
            day = dt.date.fromisoformat(text)
            if not is_us_equity_market_session(day):
                raise ValueError(f"L1 row as_of is not a U.S. equity market session: {day}")
            observed.append(day)
    if not observed:
        raise ValueError("L1 snapshot contains no observed market sessions")
    actual_start = min(observed)
    actual_end = max(observed)
    if actual_end != history_end:
        raise ValueError(f"L1 history_end does not match max observed as_of: manifest={history_end} observed={actual_end}")
    if str(l1.get("history_start_semantics") or "") == "min_observed_market_session":
        if str(l1.get("history_start") or "") != actual_start.isoformat():
            raise ValueError("L1 history_start does not match min observed as_of")
    return actual_start.isoformat(), actual_end.isoformat()


def status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    statuses = ("pass", "conditional", "recheck", "rejected")
    return {name: sum(str(row.get("l2_status") or "") == name for row in rows) for name in statuses}


def validate_lineage(l0: dict[str, Any], l1: dict[str, Any]) -> dict[str, Any]:
    lineage = dict(l1.get("l0_lineage") or {})
    if int(lineage.get("accepted_unique") or 0) != int(l0.get("accepted_unique") or -1):
        raise ValueError("L0/L1 accepted_unique lineage mismatch")
    if str(lineage.get("bundle_csv_sha256") or "") != str((l0.get("bundle") or {}).get("csv_sha256") or ""):
        raise ValueError("L0/L1 accepted universe hash mismatch")
    if str(lineage.get("source_raw_sha256") or "") != str(l0.get("raw_sha256") or ""):
        raise ValueError("L0/L1 raw source hash mismatch")
    return lineage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--l0-manifest", type=Path, required=True)
    parser.add_argument("--l1-manifest", type=Path, required=True)
    parser.add_argument("--l1-finalists", type=Path, required=True)
    parser.add_argument("--l1-snapshot", type=Path, required=True)
    parser.add_argument("--l1-summary", type=Path, required=True)
    parser.add_argument("--l2-result", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--source-commit-sha", required=True)
    parser.add_argument("--producer-file", type=Path, action="append", default=[])
    parser.add_argument("--producer-release", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.page_size <= 100:
        parser.error("page-size must be between 1 and 100")
    if len(str(args.source_commit_sha).strip()) < 7:
        parser.error("source-commit-sha is required")

    l0 = load_json(args.l0_manifest)
    l1 = load_json(args.l1_manifest)
    l1_summary = load_json(args.l1_summary)
    l2 = load_json(args.l2_result)
    if l0.get("complete") is not True or l1.get("complete") is not True:
        raise ValueError("L0/L1 source snapshots must both be complete")
    if l1_summary.get("broad_search_complete") is not True or l1_summary.get("global_ranking_complete") is not True:
        raise ValueError("L1 global ranking is incomplete")
    if int(l1_summary.get("merged_rows") or 0) != int(l1.get("rows") or 0):
        raise ValueError("L1 merged_rows does not match source snapshot")
    if int(l1_summary.get("unresolved_mature_core_rows") or 0) != 0:
        raise ValueError("L1 contains unresolved mature core rows")
    validate_l1_history_contract(l1, args.l1_snapshot)

    l1_rows = read_csv_rows(args.l1_finalists)
    if len(l1_rows) != int(l1_summary.get("ranked_l1") or -1):
        raise ValueError("L1 finalist count mismatch")
    if len(l1_rows) > 800:
        raise ValueError("L1 finalist ceiling exceeded")

    l2_all = [dict(row) for row in (l2.get("all_results") or []) if isinstance(row, dict)]
    l2_finalists = [dict(row) for row in (l2.get("finalists") or []) if isinstance(row, dict)]
    if l2.get("global_ranking") is not True:
        raise ValueError("L2 global ranking is incomplete")
    if int(l2.get("input_count") or -1) != len(l1_rows) or int(l2.get("processed_count") or -1) != len(l1_rows):
        raise ValueError("L2 did not process every L1 finalist")
    if len(l2_all) != len(l1_rows):
        raise ValueError("L2 all_results row count mismatch")
    if len(l2_finalists) > 120:
        raise ValueError("L2 finalist ceiling exceeded")
    if status_counts(l2_all) != {k: int(v) for k, v in (l2.get("status_counts") or {}).items()}:
        raise ValueError("L2 status counts mismatch")

    l1_keys = [identity(row) for row in l1_rows]
    l2_keys = [identity(row) for row in l2_all]
    if len(set(l1_keys)) != len(l1_keys) or len(set(l2_keys)) != len(l2_keys):
        raise ValueError("duplicate identity in L1/L2 audit")
    if set(l1_keys) != set(l2_keys):
        raise ValueError("L2 identity set does not equal the full L1 finalist set")
    finalist_keys = [identity(row) for row in l2_finalists]
    if len(set(finalist_keys)) != len(finalist_keys):
        raise ValueError("duplicate L2 finalist identity")
    if not set(finalist_keys).issubset(set(l2_keys)):
        raise ValueError("L2 finalist not present in full L2 result")
    for row in l2_finalists:
        if row.get("selected_for_next_stage") is not True:
            raise ValueError("L2 finalist is not selected_for_next_stage")
        if str(row.get("l2_status") or "") not in {"pass", "conditional", "recheck"}:
            raise ValueError("rejected L2 row leaked into finalists")

    lineage = validate_lineage(l0, l1)
    producer_hashes = {path.name: sha256_file(path) for path in args.producer_file}
    required_producers = {
        "bulk_prefilter.py", "batch_l2.py", "classify_l2.py", "qrgf_common.py",
        "l1-rules.json", "l2-rules.json", "publish_funnel_snapshot.py", "update-funnel.yml",
    }
    if set(producer_hashes) != required_producers:
        raise ValueError("producer-file set is incomplete or unexpected")
    producer_release = load_json(args.producer_release)
    if str(producer_release.get("schema_version") or "") != "1.0.0":
        raise ValueError("unsupported producer release schema")
    release_version = str(producer_release.get("release_version") or "").strip()
    if not release_version:
        raise ValueError("producer release_version is required")
    expected_hashes = producer_release.get("producer_hashes")
    if not isinstance(expected_hashes, dict) or expected_hashes != producer_hashes:
        raise ValueError("actual producer hashes do not match producer release manifest")
    history_contract = producer_release.get("history_contract") or {}
    if int(history_contract.get("observed_closes_for_12m_return") or 0) != 253:
        raise ValueError("producer release must require 253 observed closes for 12m return")
    producer_release_sha256 = sha256_file(args.producer_release)

    source = {
        "source_commit_sha": str(args.source_commit_sha).strip(),
        "l0": {
            "source_id": l0.get("source_id"),
            "source_url": l0.get("source_url"),
            "retrieved_at": lineage.get("retrieved_at"),
            "raw_rows": int(l0.get("raw_rows") or 0),
            "accepted_unique": int(lineage.get("accepted_unique") or 0),
            "quarantined_rows": int((l0.get("summary") or {}).get("quarantined_rows") or 0),
            "raw_sha256": lineage.get("source_raw_sha256"),
            "accepted_csv_sha256": lineage.get("bundle_csv_sha256"),
            "lineage_manifest_sha256": lineage.get("manifest_sha256"),
            "producer_hashes": l0.get("producer_hashes") or {},
        },
        "l1": {
            "source_id": l1.get("source_id"),
            "retrieved_at": l1.get("retrieved_at"),
            "requested_history_start": l1.get("requested_history_start"),
            "requested_history_end": l1.get("requested_history_end"),
            "history_start": l1.get("history_start"),
            "history_end": l1.get("history_end"),
            "history_start_semantics": l1.get("history_start_semantics"),
            "history_end_semantics": l1.get("history_end_semantics"),
            "rows": int(l1.get("rows") or 0),
            "csv_sha256": (l1.get("bundle") or {}).get("csv_sha256"),
            "manifest_sha256": sha256_file(args.l1_manifest),
            "producer_hashes": l1.get("producer_hashes") or {},
        },
    }
    l1_selected_sha = semantic_sha256(l1_rows)
    l2_all_sha = semantic_sha256(l2_all)
    transport_finalists = [transport_row(row) for row in l2_finalists]
    l2_selected_sha = semantic_sha256(transport_finalists)
    content_source = {key: value for key, value in source.items() if key != "source_commit_sha"}
    content_seed = {
        "source": content_source,
        "producer_hashes": producer_hashes,
        "producer_release_version": release_version,
        "producer_release_sha256": producer_release_sha256,
        "l1_summary_sha256": semantic_sha256(l1_summary),
        "l1_finalists_semantic_sha256": l1_selected_sha,
        "l2_all_results_semantic_sha256": l2_all_sha,
        "l2_finalists_semantic_sha256": l2_selected_sha,
        "l2_rules_hash": l2.get("l2_rules_hash"),
    }
    content_sha = semantic_sha256(content_seed)
    snapshot_id = content_sha[:24]
    snapshot_dir = args.output_root / "snapshots" / snapshot_id
    manifest_path = snapshot_dir / "manifest.json"

    if manifest_path.exists():
        existing = load_json(manifest_path)
        if str(existing.get("snapshot_content_sha256") or "") != content_sha:
            raise ValueError("immutable snapshot id collision or attempted mutation")
        latest = {
            "schema_version": "2.0.0",
            "kind": "qrgf_funnel_pointer",
            "snapshot_id": snapshot_id,
            "manifest_path": str(existing.get("manifest_path")),
            "created_at": existing.get("created_at"),
        }
        atomic_json(args.output_root / "latest.json", latest)
        print(json.dumps({"snapshot_id": snapshot_id, "reused": True, "l2_finalists": len(l2_finalists)}, indent=2))
        return 0

    pages_dir = snapshot_dir / "l2-finalists"
    pages_dir.mkdir(parents=True, exist_ok=False)
    pages: list[dict[str, Any]] = []
    for start in range(0, len(transport_finalists), args.page_size):
        chunk = transport_finalists[start:start + args.page_size]
        name = f"page-{start // args.page_size + 1:04d}.jsonl"
        path = pages_dir / name
        atomic_text(path, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in chunk))
        pages.append({
            "name": name,
            "rows": len(chunk),
            "semantic_sha256": semantic_sha256(chunk),
            "first_identity": identity_text(chunk[0]),
            "last_identity": identity_text(chunk[-1]),
        })

    shutil.copy2(args.l1_finalists, snapshot_dir / "l1-finalists.csv")
    shutil.copy2(args.l2_result, snapshot_dir / "l2-results.json")
    audit = {
        "l1_summary": l1_summary,
        "l2_summary": {
            "input_count": int(l2.get("input_count") or 0),
            "processed_count": int(l2.get("processed_count") or 0),
            "status_counts": l2.get("status_counts") or {},
            "finalist_ceiling": int(l2.get("finalist_ceiling") or 0),
            "selected_l2": len(l2_finalists),
        },
    }
    atomic_json(snapshot_dir / "audit-summary.json", audit)

    created_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema_version": "2.0.0",
        "kind": "qrgf_funnel_snapshot",
        "complete": True,
        "snapshot_id": snapshot_id,
        "snapshot_content_sha256": content_sha,
        "manifest_path": f"data/funnel/snapshots/{snapshot_id}/manifest.json",
        "created_at": created_at,
        "source": source,
        "l1": {
            "broad_search_complete": True,
            "global_ranking_complete": True,
            "merged_rows": int(l1_summary.get("merged_rows") or 0),
            "assessed_rows": int(l1_summary.get("assessed_rows") or 0),
            "actual_rankable_rows": int(l1_summary.get("actual_rankable_rows") or 0),
            "unresolved_core_rows": int(l1_summary.get("unresolved_core_rows") or 0),
            "unresolved_mature_core_rows": int(l1_summary.get("unresolved_mature_core_rows") or 0),
            "ranked_l1": len(l1_rows),
            "finalists_semantic_sha256": l1_selected_sha,
            "summary_semantic_sha256": semantic_sha256(l1_summary),
            "audit_file": "l1-finalists.csv",
            "audit_file_sha256": sha256_file(snapshot_dir / "l1-finalists.csv"),
        },
        "l2": {
            "global_ranking_complete": True,
            "input_count": int(l2.get("input_count") or 0),
            "processed_count": int(l2.get("processed_count") or 0),
            "status_counts": l2.get("status_counts") or {},
            "finalist_ceiling": int(l2.get("finalist_ceiling") or 0),
            "selected_l2": len(l2_finalists),
            "all_results_semantic_sha256": l2_all_sha,
            "selected_semantic_sha256": l2_selected_sha,
            "transport_projection": "l3_resume_minimal_v1",
            "l2_rules_hash": l2.get("l2_rules_hash"),
            "audit_file": "l2-results.json",
            "audit_file_sha256": sha256_file(snapshot_dir / "l2-results.json"),
            "page_size": args.page_size,
            "page_count": len(pages),
            "pages": pages,
        },
        "audit_summary": {
            "path": "audit-summary.json",
            "sha256": sha256_file(snapshot_dir / "audit-summary.json"),
        },
        "producer_hashes": producer_hashes,
        "producer_release": {
            "schema_version": "1.0.0",
            "release_version": release_version,
            "manifest_path": "screening/config/producer-release.json",
            "manifest_sha256": producer_release_sha256,
            "history_contract": history_contract,
        },
    }
    atomic_json(manifest_path, manifest)
    latest = {
        "schema_version": "2.0.0",
        "kind": "qrgf_funnel_pointer",
        "snapshot_id": snapshot_id,
        "manifest_path": manifest["manifest_path"],
        "created_at": created_at,
    }
    args.output_root.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output_root / "latest.json", latest)
    print(json.dumps({"snapshot_id": snapshot_id, "l1_finalists": len(l1_rows), "l2_finalists": len(l2_finalists), "pages": len(pages)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
