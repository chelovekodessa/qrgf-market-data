#!/usr/bin/env python3
"""Shared fail-closed runtime for the deployable QRGF V4.2 GitHub overlay."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME = REPO_ROOT / "screening/qrgf_v42"
SCRIPTS = RUNTIME / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from common import semantic_hash, write_json  # noqa: E402
import bootstrap, provenance  # noqa: E402


def read(path: Path | str) -> dict[str, Any]:
    target = Path(path)
    value = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{target} must contain an object")
    return value


def file_hash(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def immutable(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        if semantic_hash(read(path)) != semantic_hash(value):
            raise ValueError(f"immutable collision: {path.relative_to(REPO_ROOT)}")
        return
    write_json(path, dict(value))


def _normalized_connectors(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): ("BOUND_BY_RELEASE_MANIFEST" if str(key) == "expected_producer_release_sha256" else _normalized_connectors(child))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_normalized_connectors(child) for child in value]
    return value


def verify_release(manifest_rel: str, connector_key: str) -> str:
    manifest_path = REPO_ROOT / manifest_rel
    manifest = read(manifest_path)
    if manifest.get("schema_version") != "2.0.0" or manifest.get("architecture_version") != "4.2.0" or manifest.get("release_version") != "4.2.3":
        raise ValueError("invalid QRGF V4.2 producer release manifest")
    connectors = read(RUNTIME / "config/connectors.json")
    actual_release_sha = file_hash(manifest_path)
    expected_release_sha = str((connectors.get(connector_key) or {}).get("expected_producer_release_sha256") or "")
    if actual_release_sha != expected_release_sha:
        raise ValueError(f"V4.2 {connector_key} release hash mismatch")
    normalized_sha = semantic_hash(_normalized_connectors(connectors))
    if manifest.get("connectors_contract_sha256") != normalized_sha:
        raise ValueError("V4.2 connectors contract mismatch")
    if manifest.get("policy_file_sha256") != file_hash(RUNTIME / "config/policy.json"):
        raise ValueError("V4.2 policy file mismatch")
    files = manifest.get("files")
    if not isinstance(files, Mapping) or not files:
        raise ValueError("V4.2 release file set missing")
    for rel, expected in sorted(files.items()):
        path = REPO_ROOT / str(rel)
        if not path.is_file() or file_hash(path) != str(expected):
            raise ValueError(f"V4.2 producer file mismatch: {rel}")
    return actual_release_sha


def load_master_authority() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    latest = REPO_ROOT / "data/v42/master-core500/latest.json"
    if not latest.is_file():
        return None
    pointer = bootstrap.validate_master_pointer(read(latest))
    identity = provenance.validate_identity_map(read(REPO_ROOT / pointer["identity_path"]))
    market_index = provenance.validate_market_index_against_identity_map(read(REPO_ROOT / pointer["market_index_path"]), identity)
    query_plan = provenance.validate_query_plan(read(REPO_ROOT / pointer["query_plan_path"]), market_index_value=market_index)
    bundle = {
        "schema_version": "2.0.0",
        "kind": "qrgf_v42_master_core500_bundle",
        "candidate_source": read(REPO_ROOT / pointer["source_path"]),
        "master": read(REPO_ROOT / pointer["master_path"]),
        "selector_certificate": read(REPO_ROOT / pointer["certificate_path"]),
    }
    bundle = bootstrap.validate_master_bundle(bundle)
    bootstrap.validate_candidate_source_against_evidence(
        bundle["candidate_source"], identity_map_value=identity, market_index_value=market_index, query_plan_value=query_plan
    )
    bootstrap.validate_master_pointer(pointer, bundle=bundle)
    return pointer, bundle, identity, market_index, query_plan


def load_campaign_state(bundle: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    import campaign

    pointer_path = REPO_ROOT / "data/v42/campaign/latest.json"
    if not pointer_path.is_file():
        return None
    pointer = read(pointer_path)
    state = campaign.validate_state(read(REPO_ROOT / pointer["state_path"]), bundle=bundle)
    campaign.validate_state_pointer(pointer, state=state)
    return pointer, state
