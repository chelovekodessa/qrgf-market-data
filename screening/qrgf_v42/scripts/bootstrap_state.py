#!/usr/bin/env python3
"""Durable multi-session MASTER CORE500 bootstrap checkpoints.

These checkpoints are coordination state, not selection evidence.  They let a
new chat resume from GitHub without trusting local/chat memory.  Evidence
artifacts referenced by a checkpoint remain independently hash-validated.
"""
from __future__ import annotations

from typing import Any, Mapping

from common import ensure, semantic_hash

ARCHITECTURE_VERSION = "4.2.0"
KIND = "qrgf_v42_master_bootstrap_checkpoint"
POINTER_KIND = "qrgf_v42_master_bootstrap_pointer"
PHASES = ["IDENTITY", "MARKET_INDEX", "CLASSIFICATION", "QUALITY_DISCOVERY", "BUILD_REQUEST_READY"]


def _hash(value: Any, label: str) -> str:
    text = str(value or "")
    ensure(len(text) == 64 and all(ch in "0123456789abcdef" for ch in text), f"{label} must be a lowercase SHA-256")
    return text


def _artifact(value: Any, label: str) -> dict[str, str] | None:
    if value in (None, {}):
        return None
    ensure(isinstance(value, Mapping), f"{label} artifact ref invalid")
    path = str(value.get("path") or "")
    digest = _hash(value.get("sha256"), f"{label} artifact hash")
    ensure(path.startswith("data/v42/master-core500/bootstrap/") or path.startswith("data/v42/identity/") or path.startswith("data/v42/query-plans/") or path.startswith("data/v42/master-core500/requests/"), f"{label} artifact path outside V4.2 bootstrap authority")
    return {"path": path, "sha256": digest}


def _normalize_progress(value: Any) -> dict[str, Any]:
    raw = dict(value or {}) if isinstance(value, Mapping) else {}
    completed = sorted({str(x) for x in raw.get("completed_query_sha256s") or [] if str(x)})
    for digest in completed:
        _hash(digest, "completed query hash")
    pending = list(raw.get("pending_query_specs") or [])
    for item in pending:
        ensure(isinstance(item, Mapping), "pending query spec invalid")
    receipts = [_artifact(item, "receipt") for item in raw.get("receipt_artifacts") or []]
    ensure(all(item is not None for item in receipts), "receipt artifact ref missing")
    cursor = raw.get("cursor")
    ensure(cursor is None or isinstance(cursor, (str, int, float)), "bootstrap cursor must be scalar or null")
    return {
        "completed_query_sha256s": completed,
        "pending_query_specs": pending,
        "receipt_artifacts": receipts,
        "cursor": cursor,
    }


def build(value: Mapping[str, Any], previous_value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    phase = str(value.get("phase") or "")
    ensure(phase in PHASES, "bootstrap checkpoint phase invalid")
    market_session_id = str(value.get("market_session_id") or "")
    ensure(market_session_id, "bootstrap checkpoint market session missing")
    source_manifest_sha256 = _hash(value.get("source_manifest_sha256"), "bootstrap source manifest hash")
    bootstrap_id = str(value.get("bootstrap_id") or semantic_hash({"market_session_id": market_session_id, "source_manifest_sha256": source_manifest_sha256})[:24])
    ensure(bootstrap_id, "bootstrap id missing")
    artifacts_raw = value.get("artifacts") if isinstance(value.get("artifacts"), Mapping) else {}
    artifacts = {
        "identity_map": _artifact(artifacts_raw.get("identity_map"), "identity_map"),
        "market_index": _artifact(artifacts_raw.get("market_index"), "market_index"),
        "classification_plan": _artifact(artifacts_raw.get("classification_plan"), "classification_plan"),
        "quality_plan": _artifact(artifacts_raw.get("quality_plan"), "quality_plan"),
        "build_request": _artifact(artifacts_raw.get("build_request"), "build_request"),
    }
    phase_index = PHASES.index(phase)
    if phase_index >= PHASES.index("IDENTITY"):
        ensure(artifacts["identity_map"] is not None, "IDENTITY phase requires durable identity map")
    if phase_index >= PHASES.index("MARKET_INDEX"):
        ensure(artifacts["market_index"] is not None, "MARKET_INDEX phase requires durable market index")
    if phase_index >= PHASES.index("QUALITY_DISCOVERY"):
        ensure(artifacts["classification_plan"] is not None, "QUALITY_DISCOVERY requires complete classification plan")
    if phase_index >= PHASES.index("BUILD_REQUEST_READY"):
        ensure(artifacts["quality_plan"] is not None and artifacts["build_request"] is not None, "BUILD_REQUEST_READY requires quality plan and build request")
    progress = _normalize_progress(value.get("progress"))
    parent = str(value.get("parent_checkpoint_sha256") or "") or None
    previous = validate(previous_value) if previous_value is not None else None
    if previous is None:
        ensure(parent is None, "first bootstrap checkpoint cannot name a parent")
    else:
        ensure(parent == previous["checkpoint_sha256"], "bootstrap checkpoint parent mismatch")
        ensure(previous["bootstrap_id"] == bootstrap_id and previous["market_session_id"] == market_session_id and previous["source_manifest_sha256"] == source_manifest_sha256, "bootstrap checkpoint authority changed across resume")
        previous_index = PHASES.index(previous["phase"])
        ensure(phase_index in {previous_index, previous_index + 1}, "bootstrap checkpoint phase skip/backward transition forbidden")
        for key, ref in previous["artifacts"].items():
            if ref is not None:
                ensure(artifacts.get(key) == ref, f"bootstrap durable artifact changed after publication: {key}")
    body = {
        "schema_version": "1.0.0",
        "kind": KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "bootstrap_id": bootstrap_id,
        "phase": phase,
        "market_session_id": market_session_id,
        "source_manifest_sha256": source_manifest_sha256,
        "parent_checkpoint_sha256": parent,
        "artifacts": artifacts,
        "progress": progress,
        "ordinary_daily_broad_allowed": False,
        "master_published": False,
        "created_at": str(value.get("created_at") or ""),
    }
    ensure(body["created_at"], "bootstrap checkpoint created_at missing")
    result = {**body, "checkpoint_sha256": semantic_hash(body)}
    validate(result)
    return result


def validate(value: Mapping[str, Any] | None) -> dict[str, Any]:
    ensure(isinstance(value, Mapping), "bootstrap checkpoint invalid")
    result = dict(value)
    digest = str(result.get("checkpoint_sha256") or "")
    body = {key: child for key, child in result.items() if key != "checkpoint_sha256"}
    ensure(result.get("schema_version") == "1.0.0" and result.get("kind") == KIND and result.get("architecture_version") == ARCHITECTURE_VERSION, "bootstrap checkpoint contract invalid")
    ensure(digest == semantic_hash(body), "bootstrap checkpoint hash mismatch")
    _hash(digest, "bootstrap checkpoint hash")
    ensure(str(result.get("phase") or "") in PHASES, "bootstrap checkpoint phase invalid")
    ensure(result.get("ordinary_daily_broad_allowed") is False and result.get("master_published") is False, "bootstrap checkpoint cannot authorize broad or claim MASTER publication")
    _hash(result.get("source_manifest_sha256"), "bootstrap source manifest hash")
    for key, ref in dict(result.get("artifacts") or {}).items():
        if ref is not None:
            _artifact(ref, key)
    _normalize_progress(result.get("progress"))
    return result


def pointer(checkpoint_value: Mapping[str, Any], *, checkpoint_path: str, published_at: str) -> dict[str, Any]:
    checkpoint = validate(checkpoint_value)
    ensure(checkpoint_path.startswith("data/v42/master-core500/bootstrap/checkpoints/") and checkpoint_path.endswith(".json"), "bootstrap checkpoint pointer path invalid")
    body = {
        "schema_version": "1.0.0",
        "kind": POINTER_KIND,
        "architecture_version": ARCHITECTURE_VERSION,
        "bootstrap_id": checkpoint["bootstrap_id"],
        "phase": checkpoint["phase"],
        "checkpoint_path": checkpoint_path,
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "market_session_id": checkpoint["market_session_id"],
        "ordinary_daily_broad_allowed": False,
        "published_at": str(published_at),
    }
    ensure(body["published_at"], "bootstrap pointer published_at missing")
    return {**body, "pointer_sha256": semantic_hash(body)}


def validate_pointer(pointer_value: Mapping[str, Any], checkpoint_value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = dict(pointer_value)
    body = {key: child for key, child in result.items() if key != "pointer_sha256"}
    ensure(result.get("schema_version") == "1.0.0" and result.get("kind") == POINTER_KIND and result.get("architecture_version") == ARCHITECTURE_VERSION, "bootstrap pointer contract invalid")
    ensure(result.get("pointer_sha256") == semantic_hash(body), "bootstrap pointer hash mismatch")
    ensure(result.get("ordinary_daily_broad_allowed") is False, "bootstrap pointer cannot authorize daily broad")
    _hash(result.get("checkpoint_sha256"), "bootstrap pointer checkpoint hash")
    if checkpoint_value is not None:
        checkpoint = validate(checkpoint_value)
        ensure(result.get("checkpoint_sha256") == checkpoint["checkpoint_sha256"] and result.get("bootstrap_id") == checkpoint["bootstrap_id"] and result.get("phase") == checkpoint["phase"] and result.get("market_session_id") == checkpoint["market_session_id"], "bootstrap pointer/checkpoint readback mismatch")
    return result
