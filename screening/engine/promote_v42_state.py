#!/usr/bin/env python3
"""Single-writer QRGF V4.2 state publisher with publisher-side MASTER recomputation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import v42_runtime as rt

RUNTIME = rt.RUNTIME
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from common import semantic_hash, write_json
import bootstrap, campaign, migration, registry_store

RELEASE_REL = "screening/config/v42-state-producer-release.json"


def _master_requests() -> list[dict[str, Any]]:
    directory = rt.REPO_ROOT / "data/v42/master-core500/requests"
    return [rt.read(path) for path in sorted(directory.glob("*.json"))] if directory.is_dir() else []


def _publish_master(request: Mapping[str, Any], release_sha: str, published_at: str) -> dict[str, Any]:
    request_value = bootstrap.validate_publish_request(request)
    bundle = bootstrap.derive_publish_request(request_value)
    identity = request_value["identity_map"]
    market_index = request_value["market_index"]
    query_plan = request_value["query_plan"]
    source = bundle["candidate_source"]
    master = bundle["master"]
    certificate = bundle["selector_certificate"]

    identity_path = f"data/v42/identity/maps/{identity['identity_map_sha256']}.json"
    market_index_path = f"data/v42/identity/market-indexes/{market_index['market_index_sha256']}.json"
    query_plan_path = f"data/v42/query-plans/{query_plan['query_plan_sha256']}.json"
    source_path = f"data/v42/master-core500/sources/{source['source_sha256']}.json"
    master_path = f"data/v42/master-core500/masters/{master['master_sha256']}/master.json"
    certificate_path = f"data/v42/master-core500/certificates/{certificate['certificate_sha256']}.json"

    latest_path = rt.REPO_ROOT / "data/v42/master-core500/latest.json"
    if latest_path.is_file():
        loaded = rt.load_master_authority()
        if loaded is None or loaded[1]["master"]["master_sha256"] != master["master_sha256"]:
            raise ValueError("an authoritative V4.2 MASTER already exists; replacement is forbidden")
        current_pointer = loaded[0]
        if current_pointer.get("build_request_sha256") != request_value["request_sha256"]:
            raise ValueError("authoritative V4.2 MASTER has a conflicting build request")
        return {"status": "already_applied", "pointer": current_pointer, "bundle": loaded[1]}

    for rel, value in (
        (identity_path, identity),
        (market_index_path, market_index),
        (query_plan_path, query_plan),
        (source_path, source),
        (master_path, master),
        (certificate_path, certificate),
    ):
        rt.immutable(rt.REPO_ROOT / rel, value)

    pointer = bootstrap.master_pointer(
        bundle,
        identity_path=identity_path,
        market_index_path=market_index_path,
        query_plan_path=query_plan_path,
        source_path=source_path,
        master_path=master_path,
        certificate_path=certificate_path,
        build_request_sha256=request_value["request_sha256"],
        published_at=published_at,
        producer_release_sha256=release_sha,
    )
    write_json(latest_path, pointer)
    return {"status": "applied", "pointer": pointer, "bundle": bundle}


def _load_gate(master_sha: str, name: str) -> dict[str, Any] | None:
    path = rt.REPO_ROOT / f"data/v42/campaigns/{master_sha}/gates/{name}"
    return rt.read(path) if path.is_file() else None


def _publish_campaign(bundle: Mapping[str, Any], release_sha: str, published_at: str) -> dict[str, Any]:
    master = bundle["master"]
    inventory = migration.registry_inventory(rt.REPO_ROOT)
    snapshot = campaign.durable_snapshot(master, inventory)
    snapshot_path = rt.REPO_ROOT / f"data/v42/campaigns/{master['master_sha256']}/snapshots/{snapshot['snapshot_sha256']}.json"
    rt.immutable(snapshot_path, snapshot)

    state_path = rt.REPO_ROOT / f"data/v42/campaigns/{master['master_sha256']}/state.json"
    pointer_path = rt.REPO_ROOT / "data/v42/campaign/latest.json"
    previous_state = None
    previous_pointer = None
    if state_path.is_file() and pointer_path.is_file():
        previous_state = campaign.validate_state(rt.read(state_path), bundle=bundle)
        previous_pointer = campaign.validate_state_pointer(rt.read(pointer_path), state=previous_state)

    runtime_gate = _load_gate(master["master_sha256"], "runtime-reconstruction.json")
    pilot_gate = _load_gate(master["master_sha256"], "pilot-registry.json")
    stable_time = str(previous_state.get("generated_at") or published_at) if previous_state else published_at
    candidate = campaign.build_state(
        bundle,
        inventory,
        runtime_gate_value=runtime_gate,
        pilot_gate_value=pilot_gate,
        previous_state=previous_state,
        generated_at=stable_time,
    )
    if previous_state is not None and candidate["state_sha256"] != previous_state["state_sha256"]:
        candidate = campaign.build_state(
            bundle,
            inventory,
            runtime_gate_value=runtime_gate,
            pilot_gate_value=pilot_gate,
            previous_state=previous_state,
            generated_at=published_at,
        )
    if previous_state is not None and candidate["state_sha256"] == previous_state["state_sha256"]:
        return {"status": "unchanged", "state": previous_state, "pointer": previous_pointer, "snapshot": snapshot}

    write_json(state_path, candidate)
    pointer = campaign.state_pointer(
        candidate,
        state_path=state_path.relative_to(rt.REPO_ROOT).as_posix(),
        published_at=published_at,
        producer_release_sha256=release_sha,
    )
    write_json(pointer_path, pointer)
    return {"status": "updated", "state": candidate, "pointer": pointer, "snapshot": snapshot}


def _publish_migration(bundle: Mapping[str, Any] | None) -> dict[str, Any]:
    report = migration.build_migration_report(rt.REPO_ROOT, bundle_value=bundle)
    report_path = rt.REPO_ROOT / f"data/v42/migrations/reports/{report['report_sha256']}.json"
    rt.immutable(report_path, report)
    body = {
        "schema_version": "2.0.0",
        "kind": "qrgf_v42_migration_pointer",
        "report_path": report_path.relative_to(rt.REPO_ROOT).as_posix(),
        "report_sha256": report["report_sha256"],
    }
    pointer = {**body, "pointer_sha256": semantic_hash(body)}
    write_json(rt.REPO_ROOT / "data/v42/migrations/latest.json", pointer)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--published-at", required=True)
    args = parser.parse_args()

    release_sha = rt.verify_release(RELEASE_REL, "master_core500_v42")
    registry_result = registry_store.promote_directory(
        rt.REPO_ROOT,
        rt.REPO_ROOT / "data/v4/registry/proposals",
        producer_release_sha256=release_sha,
        published_at=args.published_at,
    )

    requests = _master_requests()
    master_result: dict[str, Any] | None = None
    if requests:
        unique = {str(item.get("request_sha256") or "") for item in requests}
        if len(unique) != 1:
            raise ValueError("multiple distinct unconsumed V4.2 MASTER build requests")
        master_result = _publish_master(requests[0], release_sha, args.published_at)

    loaded = rt.load_master_authority()
    bundle = loaded[1] if loaded is not None else None
    campaign_result = _publish_campaign(bundle, release_sha, args.published_at) if bundle is not None else None
    migration_report = _publish_migration(bundle)
    output = {
        "status": "ok",
        "release_sha256": release_sha,
        "master": None if master_result is None else {"status": master_result["status"], "master_sha256": master_result["pointer"]["master_sha256"]},
        "registry": {key: registry_result[key] for key in ("files", "items", "applied", "skipped")},
        "campaign": None if campaign_result is None else {
            "status": campaign_result["status"],
            "phase": campaign_result["state"]["phase"],
            "master_durable_count": campaign_result["state"]["master_durable_count"],
            "daily_broad_allowed": campaign_result["state"]["daily_broad_allowed"],
            "state_sha256": campaign_result["state"]["state_sha256"],
        },
        "migration_report_sha256": migration_report["report_sha256"],
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
