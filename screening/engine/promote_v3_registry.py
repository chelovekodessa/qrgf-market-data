#!/usr/bin/env python3
"""Single-writer promotion of validated QRGF v3 Passport/invalidations into Registry."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

SENSITIVE = {
    "account", "account_id", "account_number", "balances", "positions", "allocation",
    "available_funds", "buying_power", "cash_balance", "quote", "bid", "ask",
    "raw_connector_response", "licensed_payload",
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def reject_sensitive(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            dotted = f"{path}.{key}" if path else str(key)
            if str(key).lower() in SENSITIVE:
                raise ValueError(f"sensitive field in public Registry proposal: {dotted}")
            reject_sensitive(child, dotted)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_sensitive(child, f"{path}[{index}]")


def validate_update(value: Mapping[str, Any]) -> dict[str, Any]:
    p = dict(value)
    if p.get("schema_version") != "1.0.0" or p.get("kind") != "qrgf_passport_update_proposal":
        raise ValueError("invalid Passport update proposal")
    body = {k: v for k, v in p.items() if k != "proposal_sha256"}
    if p.get("proposal_sha256") != semantic_hash(body):
        raise ValueError("Passport proposal hash mismatch")
    passport = p.get("passport_payload") if isinstance(p.get("passport_payload"), Mapping) else {}
    if passport.get("schema_version") != "1.0.0" or passport.get("kind") != "qrgf_quality_passport":
        raise ValueError("invalid Passport payload")
    if p.get("passport_sha256") != semantic_hash(passport):
        raise ValueError("Passport content hash mismatch")
    summary = p.get("summary") if isinstance(p.get("summary"), Mapping) else {}
    issuer = str(p.get("issuer_id") or "")
    if not issuer or issuer != str(passport.get("issuer_id") or "") or issuer != str(summary.get("issuer_id") or ""):
        raise ValueError("Passport issuer mismatch")
    if p.get("quality_policy_version") != passport.get("quality_policy_version") or p.get("quality_policy_version") != summary.get("quality_policy_version"):
        raise ValueError("Passport policy mismatch")
    reject_sensitive(p)
    return p


def validate_invalidation(value: Mapping[str, Any]) -> dict[str, Any]:
    p = dict(value)
    if p.get("schema_version") != "1.0.0" or p.get("kind") != "qrgf_passport_invalidation_proposal":
        raise ValueError("invalid Passport invalidation proposal")
    body = {k: v for k, v in p.items() if k != "proposal_sha256"}
    if p.get("proposal_sha256") != semantic_hash(body):
        raise ValueError("invalidation proposal hash mismatch")
    issuer = str(p.get("issuer_id") or "")
    if not issuer or not p.get("reason"):
        raise ValueError("invalidation proposal missing issuer/reason")
    reject_sensitive(p)
    return p


def proposal_order(p: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(p.get("event_scan_through") or p.get("market_session_id") or ""),
        str((p.get("summary") or {}).get("as_of") or p.get("created_at") or ""),
        str(p.get("proposal_sha256") or ""),
    )


def current_registry(root: Path, quality_policy_version: str) -> dict[str, Any]:
    latest = root / "registry" / "latest.json"
    if not latest.is_file():
        body = {
            "schema_version": "1.0.0", "kind": "qrgf_quality_registry", "registry_id": "bootstrap-empty-v1",
            "created_at": "1970-01-01T00:00:00Z", "quality_policy_version": quality_policy_version, "entries": [],
        }
        return {**body, "registry_sha256": semantic_hash(body)}
    value = load(latest)
    expected = value.get("registry_sha256")
    body = {k: v for k, v in value.items() if k != "registry_sha256"}
    if expected != semantic_hash(body):
        raise ValueError("existing Registry self-hash mismatch")
    return value


def safe_segment(issuer: str) -> str:
    return hashlib.sha256(issuer.encode("utf-8")).hexdigest()[:20]


def publish_registry(root: Path, entries: list[dict[str, Any]], quality_policy_version: str, last_proposal: str) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    registry_id = f"registry-{last_proposal[:16]}"
    body = {
        "schema_version": "1.0.0", "kind": "qrgf_quality_registry", "registry_id": registry_id,
        "created_at": now, "quality_policy_version": quality_policy_version,
        "entries": sorted(entries, key=lambda row: str(row.get("issuer_id") or "")),
    }
    value = {**body, "registry_sha256": semantic_hash(body)}
    registry_root = root / "registry"
    snapshots = registry_root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f"{value['registry_sha256']}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (registry_root / "latest.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return value


def update_system_registry(root: Path, registry: Mapping[str, Any]) -> None:
    for path in (root / "latest.json", root / "latest" / "system-snapshot.json"):
        if not path.is_file():
            continue
        system = load(path)
        system["registry_sha256"] = registry["registry_sha256"]
        coverage = system.get("coverage") if isinstance(system.get("coverage"), dict) else {}
        coverage["registry_entry_count"] = len(registry.get("entries") or [])
        system["coverage"] = coverage
        system.pop("system_snapshot_sha256", None)
        system["system_snapshot_sha256"] = semantic_hash(system)
        path.write_text(json.dumps(system, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v3-root", type=Path, required=True)
    parser.add_argument("--proposals-dir", type=Path, required=True)
    parser.add_argument("--quality-policy-version", required=True)
    args = parser.parse_args()
    registry = current_registry(args.v3_root, args.quality_policy_version)
    by_issuer = {str(row.get("issuer_id") or ""): dict(row) for row in registry.get("entries") or []}
    proposals: list[dict[str, Any]] = []
    if args.proposals_dir.is_dir():
        for path in sorted(args.proposals_dir.glob("*.json")):
            raw = load(path)
            kind = raw.get("kind")
            proposals.append(validate_update(raw) if kind == "qrgf_passport_update_proposal" else validate_invalidation(raw))
    proposals.sort(key=proposal_order)
    applied = 0
    last_sha = "0" * 64
    for p in proposals:
        issuer = str(p["issuer_id"])
        existing = by_issuer.get(issuer)
        existing_key = (str((existing or {}).get("event_scan_through") or ""), str((existing or {}).get("quality_as_of") or ""), str((existing or {}).get("last_proposal_sha256") or ""))
        if proposal_order(p) <= existing_key:
            continue
        last_sha = str(p["proposal_sha256"])
        if p["kind"] == "qrgf_passport_update_proposal":
            passport = dict(p["passport_payload"])
            passport_hash = str(p["passport_sha256"])
            passport_dir = args.v3_root / "passports" / safe_segment(issuer)
            passport_dir.mkdir(parents=True, exist_ok=True)
            passport_path = passport_dir / f"{passport_hash}.json"
            if passport_path.exists() and semantic_hash(load(passport_path)) != passport_hash:
                raise ValueError("immutable Passport collision")
            if not passport_path.exists():
                passport_path.write_text(json.dumps(passport, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            summary = p["summary"]
            status = str(summary.get("quality_status") or "unknown")
            by_issuer[issuer] = {
                "issuer_id": issuer,
                "passport_hash": passport_hash,
                "passport_path": passport_path.as_posix(),
                "registry_status": "active" if summary.get("quality_eligible") is True else "excluded" if status == "rejected" else "watch",
                "freshness_status": "fresh" if p.get("event_scan_through") else "needs_refresh",
                "event_scan_through": p.get("event_scan_through"),
                "quality_policy_version": p.get("quality_policy_version"),
                "quality_status": status,
                "quality_score": summary.get("quality_score"),
                "quality_coverage_pct": summary.get("quality_coverage_pct"),
                "quality_eligible": summary.get("quality_eligible"),
                "economic_archetype": summary.get("economic_archetype"),
                "listing_overlay": summary.get("listing_overlay"),
                "quality_as_of": summary.get("as_of"),
                "last_proposal_sha256": p["proposal_sha256"],
            }
        else:
            if existing is None:
                by_issuer[issuer] = {
                    "issuer_id": issuer, "registry_status": "excluded", "freshness_status": "invalidated",
                    "quality_policy_version": args.quality_policy_version, "quality_status": "unknown",
                    "quality_score": None, "quality_coverage_pct": None, "quality_eligible": False,
                    "event_scan_through": p.get("market_session_id"), "invalidation_reason": p.get("reason"),
                    "last_proposal_sha256": p["proposal_sha256"],
                }
            else:
                existing = dict(existing)
                existing.update({"registry_status": "excluded", "freshness_status": "invalidated", "quality_eligible": False, "invalidation_reason": p.get("reason"), "event_scan_through": p.get("market_session_id") or existing.get("event_scan_through"), "last_proposal_sha256": p["proposal_sha256"]})
                by_issuer[issuer] = existing
        applied += 1
    if applied:
        registry = publish_registry(args.v3_root, list(by_issuer.values()), args.quality_policy_version, last_sha)
        update_system_registry(args.v3_root, registry)
    print(json.dumps({"applied": applied, "registry_sha256": registry["registry_sha256"], "entry_count": len(registry.get("entries") or [])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
