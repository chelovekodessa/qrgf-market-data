#!/usr/bin/env python3
"""Compute CANARY or PILOT gate from a clean GitHub checkout."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import v42_runtime as rt

if str(rt.SCRIPTS) not in sys.path:
    sys.path.insert(0, str(rt.SCRIPTS))

from common import ensure
import campaign, migration, registry

RELEASE_REL = "screening/config/v42-state-producer-release.json"


def _reuse_results(master: dict[str, Any], source_snapshot: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key in master["pilot_scope_keys"]:
        record = source_snapshot["records"].get(key) or {}
        if str(record.get("quality_status") or "") not in campaign.QUALITY_RESOLVED:
            continue
        entry_path = rt.REPO_ROOT / registry.pointer_path(key)
        entry = registry.validate_entry(rt.read(entry_path))
        passport_value = registry.validate_passport(rt.read(rt.REPO_ROOT / entry["passport_path"]), entry=entry)
        reused = registry.reuse(entry, passport_value=passport_value, market_session_id=master["market_session_id"])
        ensure(reused is not None, f"PILOT reusable Passport failed readback: {key}")
        output[key] = {
            "passport_hash": record["passport_hash"],
            "entry_sha256": record["entry_sha256"],
            "receipt_sha256": record["receipt_sha256"],
            "reused_without_deep_research": True,
            "policy_compatible": True,
            "overlay_compatible": True,
        }
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("runtime", "pilot"))
    parser.add_argument("--source-commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--verified-at", required=True)
    args = parser.parse_args()

    release_sha = rt.verify_release(RELEASE_REL, "master_core500_v42")
    loaded = rt.load_master_authority()
    if loaded is None:
        print(json.dumps({"status": "blocked", "reason": "MASTER_NOT_INITIALIZED"}, sort_keys=True))
        return 0
    bundle = loaded[1]
    master = bundle["master"]
    state_loaded = rt.load_campaign_state(bundle)
    if state_loaded is None:
        print(json.dumps({"status": "blocked", "reason": "CAMPAIGN_NOT_INITIALIZED"}, sort_keys=True))
        return 0
    _, state = state_loaded
    source_path = rt.REPO_ROOT / f"data/v42/campaigns/{master['master_sha256']}/snapshots/{state['registry_snapshot_sha256']}.json"
    source_snapshot = campaign.validate_durable_snapshot(rt.read(source_path), master=master)
    reconstructed_snapshot = campaign.durable_snapshot(master, migration.registry_inventory(rt.REPO_ROOT))

    gate_dir = rt.REPO_ROOT / f"data/v42/campaigns/{master['master_sha256']}/gates"
    if args.mode == "runtime":
        target = gate_dir / "runtime-reconstruction.json"
        if target.is_file():
            print(json.dumps({"status": "already_present", "path": target.relative_to(rt.REPO_ROOT).as_posix()}, sort_keys=True))
            return 0
        if state["phase"] != "CANARY" or int(state["canary_durable_count"]) != len(master["canary_scope_keys"]):
            print(json.dumps({"status": "blocked", "reason": "CANARY_NOT_DURABLE_COMPLETE", "count": state["canary_durable_count"]}, sort_keys=True))
            return 0
        gate = campaign.runtime_reconstruction_gate(
            master,
            source_snapshot,
            reconstructed_snapshot,
            source_commit_sha=args.source_commit_sha,
            workflow_run_id=args.workflow_run_id,
            reconstructed_at=args.verified_at,
            validator_release_sha256=release_sha,
        )
    else:
        target = gate_dir / "pilot-registry.json"
        if target.is_file():
            print(json.dumps({"status": "already_present", "path": target.relative_to(rt.REPO_ROOT).as_posix()}, sort_keys=True))
            return 0
        runtime_path = gate_dir / "runtime-reconstruction.json"
        if not runtime_path.is_file() or state["phase"] != "PILOT" or int(state["pilot_durable_count"]) != len(master["pilot_scope_keys"]):
            print(json.dumps({"status": "blocked", "reason": "PILOT_NOT_DURABLE_COMPLETE", "count": state["pilot_durable_count"]}, sort_keys=True))
            return 0
        gate = campaign.pilot_registry_gate(
            master,
            source_snapshot,
            reconstructed_snapshot,
            reuse_results=_reuse_results(master, source_snapshot),
            source_commit_sha=args.source_commit_sha,
            workflow_run_id=args.workflow_run_id,
            verified_at=args.verified_at,
            validator_release_sha256=release_sha,
        )

    rt.immutable(target, gate)
    print(json.dumps({"status": "computed", "path": target.relative_to(rt.REPO_ROOT).as_posix(), "gate_sha256": gate["gate_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
