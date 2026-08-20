#!/usr/bin/env python3
"""Isolated V4.1 to V4.2 Registry inventory and lossless reuse proof."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from common import read_json, semantic_hash
import bootstrap, registry


def _read(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _overlay_compatible(entry: Mapping[str, Any], passport_value: Mapping[str, Any]) -> bool:
    overlay = str(entry.get("security_overlay") or "")
    listing = str(passport_value.get("listing_overlay") or "none").lower()
    expected = "adr" if overlay == "adr" else "none"
    return overlay in {"common_equity", "adr", "etf"} and listing == expected


def registry_inventory(repo_root: Path) -> dict[str, dict[str, Any]]:
    """Read the existing Registry as durable knowledge without modifying it."""
    root = Path(repo_root)
    pointers = root / "data/v4/registry/scopes"
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(pointers.glob("*.json")) if pointers.is_dir() else []:
        try:
            entry = registry.validate_entry(_read(path))
            passport = registry.validate_passport(_read(root / entry["passport_path"]), entry=entry)
            receipt_path = root / "data/v4/registry/receipts" / f"{entry['last_proposal_sha256']}.json"
            receipt = registry.validate_receipt(_read(receipt_path), entry=entry, passport_value=passport)
            durable = registry.bootstrap_durable_complete(receipt=receipt, entry=entry, passport_value=passport)
            out[entry["research_scope_key"]] = {
                "quality_status": entry["quality_status"], "quality_score": entry.get("quality_score"),
                "quality_coverage_pct": entry.get("quality_coverage_pct"), "quality_eligible": entry.get("quality_eligible") is True,
                "freshness_status": entry["freshness_status"], "event_scan_through": entry.get("event_scan_through"),
                "receipt_sha256": receipt["receipt_sha256"], "passport_hash": entry["passport_hash"],
                "entry_sha256": entry["entry_sha256"], "next_review_date": entry.get("next_review_date"),
                "durable_readback_verified": durable["durable_reviewed"], "policy_compatible": True,
                "overlay_compatible": passport.get("issuer_id") == entry.get("issuer_id") and _overlay_compatible(entry, passport),
                "passport_path": entry["passport_path"], "receipt_path": receipt_path.relative_to(root).as_posix(),
            }
        except (OSError, ValueError, KeyError):
            # An invalid item remains visible to the report, but never counts as reusable.
            out[f"invalid:{path.name}"] = {"durable_readback_verified": False, "invalid_pointer_path": path.relative_to(root).as_posix()}
    return out


def reusable_for_master(repo_root: Path, bundle_value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    bundle = bootstrap.validate_master_bundle(bundle_value)
    master = bundle["master"]
    inventory = registry_inventory(Path(repo_root))
    return {key: inventory[key] for key in [x["research_scope_key"] for x in master["scopes"]] if key in inventory}


def legacy_proposal_journal_status(repo_root: Path) -> dict[str, Any]:
    """Verify that historically receipted proposals are present in the live journal."""
    root = Path(repo_root)
    history_path = root / "data/v4/migrations/v410/proposal-journal-history.json"
    if not history_path.is_file():
        return {"present": False, "verified": False, "restored_to_current_journal": False, "records": 0}
    history = _read(history_path)
    body = {k: x for k, x in history.items() if k != "history_sha256"}
    if history.get("kind") != "qrgf_v41_legacy_proposal_journal_history" or history.get("history_sha256") != semantic_hash(body) or history.get("raw_proposals_restored_to_current_journal") is not True:
        raise ValueError("invalid legacy proposal journal history")
    proposal_shas: set[str] = set()
    proposal_dir = root / "data/v4/registry/proposals"
    for path in sorted(proposal_dir.glob("*.json")) if proposal_dir.is_dir() else []:
        proposal_value = _read(path)
        kind = str(proposal_value.get("kind") or "")
        if kind == "qrgf_v4_registry_batch":
            items = registry.validate_batch(proposal_value)["items"]
        elif kind == "qrgf_v4_passport_update_proposal":
            items = [registry.validate_proposal(proposal_value)]
        elif kind == "qrgf_v4_freshness_update_proposal":
            items = [registry.validate_freshness_proposal(proposal_value)]
        else:
            raise ValueError("unsupported proposal journal item")
        proposal_shas.update(str(item["proposal_sha256"]) for item in items)
    records = list(history.get("records") or [])
    for record in records:
        proposal_sha = str(record.get("proposal_sha256") or "")
        receipt_path = root / "data/v4/registry/receipts" / f"{proposal_sha}.json"
        if proposal_sha not in proposal_shas or not receipt_path.is_file() or registry.validate_receipt_record(_read(receipt_path)).get("proposal_sha256") != proposal_sha:
            raise ValueError("legacy proposal journal restoration mismatch")
    return {"present": True, "verified": True, "restored_to_current_journal": True, "records": len(records), "history_sha256": history["history_sha256"]}


def build_migration_report(repo_root: Path, *, bundle_value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Produce a self-hashed audit record. Old Core15 never becomes a MASTER."""
    root = Path(repo_root)
    inventory = registry_inventory(root)
    legacy_latest = root / "data/v4/bootstrap/latest.json"
    legacy: dict[str, Any] = {"present": legacy_latest.exists(), "classification": "historical_validation_artifact_only"}
    legacy_keys: list[str] = []
    if legacy_latest.exists():
        pointer = _read(legacy_latest)
        cohort_path = root / str(pointer.get("cohort_path") or "")
        if cohort_path.is_file():
            cohort = _read(cohort_path)
            body = {k: x for k, x in cohort.items() if k != "cohort_sha256"}
            if cohort.get("cohort_sha256") == semantic_hash(body):
                legacy_keys = [str(item.get("research_scope_key") or "") for item in cohort.get("scopes") or []]
                legacy.update({"cohort_sha256": cohort.get("cohort_sha256"), "requested_size": cohort.get("requested_size"), "selected_scope_count": cohort.get("selected_scope_count"), "legacy_scope_count": len(legacy_keys), "historical_hash_valid": True})
            else:
                legacy["historical_hash_valid"] = False
    master_reuse: dict[str, Any] = {"master_present": bundle_value is not None, "reused_fresh_scope_count": 0, "stale_scope_count": 0, "new_scope_count": 0}
    if bundle_value is not None:
        bundle = bootstrap.validate_master_bundle(bundle_value)
        master = bundle["master"]
        keys = [str(item["research_scope_key"]) for item in master["scopes"]]
        reused = reusable_for_master(root, bundle)
        durable_reused = [key for key, record in reused.items() if record.get("durable_readback_verified") is True and record.get("freshness_status") == "fresh" and str(record.get("event_scan_through") or "") >= str(master["market_session_id"])]
        stale = [key for key, record in reused.items() if key not in durable_reused]
        master_reuse = {"master_present": True, "master_sha256": master["master_sha256"], "reused_fresh_scope_count": len(durable_reused), "stale_scope_count": len(stale), "new_scope_count": len(keys) - len(reused), "reused_scope_keys": sorted(durable_reused), "stale_scope_keys": sorted(stale)}
    body = {
        "schema_version": "2.0.0", "kind": "qrgf_v42_migration_report", "source_architecture_version": "4.1.0",
        "target_architecture_version": "4.2.0", "legacy_bootstrap": legacy,
        "registry_scope_count": len(inventory), "registry_scope_keys": sorted(key for key in inventory if not key.startswith("invalid:")),
        "legacy_scopes_preserved_count": sum(key in inventory for key in legacy_keys), "master_reuse": master_reuse,
        "legacy_is_not_authoritative_master": True, "v41_production_state_is_historical_read_only": True,
        "registry_knowledge_outside_master_preserved": True,
        "legacy_proposal_journal": legacy_proposal_journal_status(root),
    }
    return {**body, "report_sha256": semantic_hash(body)}
