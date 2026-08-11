#!/usr/bin/env python3
"""Run one ruleset over all L1 candidates and perform one global L2 ranking."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from classify_l2 import classify  # noqa: E402
from qrgf_common import atomic_write_json, parse_canonical_csv_row  # noqa: E402


def load_candidates(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            payload = payload.get("candidates", payload.get("rows", payload.get("data", payload)))
        if not isinstance(payload, list):
            raise ValueError("candidate JSON must contain a list")
        return [dict(row) for row in payload if isinstance(row, dict)]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [parse_canonical_csv_row(row) for row in csv.DictReader(handle)]



def adapt_l1_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Map canonical L1 export fields into the L2 contract without guessing units."""
    result = dict(row)
    aliases = {
        "current_price": ("current_price", "price"),
        "return_3m_pct": ("return_3m_pct", "return_3m"),
        "return_6m_pct": ("return_6m_pct", "return_6m"),
        "return_12m_pct": ("return_12m_pct", "return_12m"),
        "drawdown_pct": ("drawdown_pct", "drawdown_52w"),
        "historical_volatility_pct": ("historical_volatility_pct", "historical_volatility"),
        "momentum_history_status": ("momentum_history_status", "history_sufficiency"),
    }
    for target, sources in aliases.items():
        if result.get(target) not in (None, ""):
            continue
        for source in sources:
            if row.get(source) not in (None, ""):
                result[target] = row.get(source)
                break
    return result

def rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    # L2 is a high-recall research triage.  `conditional` means the candidate
    # carries flags for later research; it is not incomplete data (that is
    # `recheck`) and must not silently outrank Research Priority.
    status_rank = {"pass": 2, "conditional": 2, "recheck": 1, "rejected": 0}.get(str(row.get("l2_status")), -1)
    research = row.get("research_priority_score")
    coverage = row.get("research_priority_coverage_pct") or 0.0
    ticker = str(row.get("ticker") or "")
    return (
        status_rank,
        float(research) if research is not None else -1.0,
        float(coverage),
        tuple(-ord(char) for char in ticker),
    )


def run(
    candidates: list[dict[str, Any]],
    rules: dict[str, Any],
    rules_hash: str,
    keep: int = 120,
    *,
    ruleset_version: str | None = None,
    ruleset_hash: str | None = None,
) -> dict[str, Any]:
    effective_version = str(ruleset_version or rules.get("ruleset_version") or "")
    effective_hash = str(ruleset_hash or rules_hash)
    results = []
    for candidate in candidates:
        adapted = adapt_l1_candidate(candidate)
        result = classify(adapted, rules, effective_hash)
        result["ruleset_version"] = effective_version
        result["ruleset_hash"] = effective_hash
        result["l2_rules_hash"] = rules_hash
        # Preserve stable identity and source metadata so L2 updates the existing
        # candidate instead of creating a second ticker-only state record.
        results.append({**adapted, **result})
    ranked = sorted(results, key=rank_key, reverse=True)
    finalists = [row for row in ranked if row["l2_status"] in {"pass", "conditional", "recheck"} and row.get("research_priority_score") is not None][:keep]
    finalist_keys = {(str(row.get("ticker") or "").upper(), str(row.get("contract_id") or "")) for row in finalists}
    for row in ranked:
        row["selected_for_next_stage"] = (str(row.get("ticker") or "").upper(), str(row.get("contract_id") or "")) in finalist_keys
    return {
        "ruleset_version": effective_version,
        "ruleset_hash": effective_hash,
        "l2_rules_hash": rules_hash,
        "input_count": len(candidates),
        "processed_count": len(results),
        "status_counts": {
            status: sum(row["l2_status"] == status for row in results)
            for status in ("pass", "conditional", "recheck", "rejected")
        },
        "global_ranking": True,
        "finalist_ceiling": keep,
        "finalists": finalists,
        "all_results": ranked,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--rules", type=Path, default=Path(__file__).resolve().parent.parent / "assets" / "l2-rules.json")
    parser.add_argument("--keep", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ruleset-version")
    parser.add_argument("--ruleset-hash")
    args = parser.parse_args()
    if not 1 <= args.keep <= 120:
        parser.error("keep must be between 1 and 120")
    rules_bytes = args.rules.read_bytes()
    result = run(
        load_candidates(args.input),
        json.loads(rules_bytes),
        hashlib.sha256(rules_bytes).hexdigest(),
        args.keep,
        ruleset_version=args.ruleset_version,
        ruleset_hash=args.ruleset_hash,
    )
    atomic_write_json(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
