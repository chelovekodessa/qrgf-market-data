#!/usr/bin/env python3
"""Hash-bound V4.2 MASTER CORE500 campaign state machine.

Campaign progress distinguishes a completed research attempt from resolved
Structural Quality and from resolved competitive uncertainty.  CANARY and
PILOT gates are computed from before/after Registry snapshots produced by a
clean GitHub checkout; callers cannot supply pass booleans or loss counts.
"""
from __future__ import annotations

from typing import Any, Mapping

from common import ensure, load_policy, semantic_hash
import bootstrap

PHASES = ("CANARY", "PILOT", "CORE500", "COMPLETE")
ATTEMPT_TERMINAL = frozenset({"pass", "conditional", "rejected", "insufficient_data"})
QUALITY_RESOLVED = frozenset({"pass", "conditional", "rejected"})
COMPETITIVE_RESOLVED = frozenset({"pass", "conditional", "rejected"})
SNAPSHOT_KIND = "qrgf_v42_registry_durable_snapshot"
RUNTIME_GATE_KIND = "qrgf_v42_runtime_reconstruction_gate"
PILOT_GATE_KIND = "qrgf_v42_pilot_registry_gate"
STATE_KIND = "qrgf_v42_campaign_state"
POINTER_KIND = "qrgf_v42_campaign_pointer"
WAVE_KIND = "qrgf_v42_campaign_wave_plan"
ATTESTATION_CLASS = "github_actions_computed"


def _hash(value: Mapping[str, Any], field: str, label: str) -> dict[str, Any]:
    v = dict(value)
    body = {k: x for k, x in v.items() if k != field}
    ensure(v.get(field) == semantic_hash(body), f"{label} self hash mismatch")
    return v


def _require_hash(value: Any, label: str) -> str:
    text = str(value or "")
    ensure(len(text) == 64 and all(ch in "0123456789abcdef" for ch in text), f"{label} must be a lowercase SHA-256")
    return text


def _require_commit(value: Any) -> str:
    text = str(value or "")
    ensure(len(text) == 40 and all(ch in "0123456789abcdef" for ch in text), "gate source commit must be a lowercase Git commit SHA")
    return text


def _scope_keys(master: Mapping[str, Any]) -> list[str]:
    return [str(x["research_scope_key"]) for x in master["scopes"]]


def _durable_record(record: Mapping[str, Any], *, market_session_id: str) -> bool:
    if record.get("durable_readback_verified") is not True:
        return False
    if record.get("policy_compatible") is not True or record.get("overlay_compatible") is not True:
        return False
    if str(record.get("freshness_status") or "") != "fresh":
        return False
    if str(record.get("event_scan_through") or "") < str(market_session_id):
        return False
    status = str(record.get("quality_status") or "")
    if status not in ATTEMPT_TERMINAL:
        return False
    if status == "insufficient_data" and not str(record.get("next_review_date") or "").strip():
        return False
    return all(len(str(record.get(field) or "")) == 64 for field in ("receipt_sha256", "passport_hash", "entry_sha256"))


def _semantics(status: str) -> dict[str, str]:
    return {
        "attempt_resolution": "completed_blocked" if status == "insufficient_data" else "completed",
        "quality_resolution": "resolved" if status in QUALITY_RESOLVED else "unknown",
        "competitive_resolution": "resolved" if status in COMPETITIVE_RESOLVED else "unresolved",
    }


def durable_snapshot(master_value: Mapping[str, Any], durable_by_scope: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    master = bootstrap.validate_master(master_value)
    records: dict[str, dict[str, Any]] = {}
    for key in _scope_keys(master):
        raw = durable_by_scope.get(key)
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        if not _durable_record(item, market_session_id=master["market_session_id"]):
            continue
        status = str(item["quality_status"])
        records[key] = {
            "quality_status": status,
            **_semantics(status),
            "event_scan_through": str(item["event_scan_through"]),
            "freshness_status": "fresh",
            "receipt_sha256": str(item["receipt_sha256"]),
            "passport_hash": str(item["passport_hash"]),
            "entry_sha256": str(item["entry_sha256"]),
            "policy_compatible": True,
            "overlay_compatible": True,
            "durable_readback_verified": True,
            "next_review_date": str(item.get("next_review_date") or "") or None,
        }
    body = {
        "schema_version": "2.0.0",
        "kind": SNAPSHOT_KIND,
        "architecture_version": bootstrap.ARCHITECTURE_VERSION,
        "master_sha256": master["master_sha256"],
        "market_session_id": master["market_session_id"],
        "records": {key: records[key] for key in sorted(records)},
    }
    value = {**body, "snapshot_sha256": semantic_hash(body)}
    validate_durable_snapshot(value, master=master)
    return value


def validate_durable_snapshot(value: Mapping[str, Any], *, master: Mapping[str, Any]) -> dict[str, Any]:
    m = bootstrap.validate_master(master)
    v = _hash(value, "snapshot_sha256", "Registry durable snapshot")
    ensure(v.get("schema_version") == "2.0.0" and v.get("kind") == SNAPSHOT_KIND, "invalid V4.2 Registry durable snapshot")
    ensure(v.get("architecture_version") == bootstrap.ARCHITECTURE_VERSION, "Registry durable snapshot architecture mismatch")
    ensure(v.get("master_sha256") == m["master_sha256"] and v.get("market_session_id") == m["market_session_id"], "Registry durable snapshot MASTER mismatch")
    records = v.get("records")
    ensure(isinstance(records, Mapping), "Registry durable snapshot records missing")
    keys = set(_scope_keys(m))
    ensure(set(records).issubset(keys), "Registry durable snapshot contains an external scope")
    for key, item in records.items():
        ensure(isinstance(item, Mapping) and _durable_record(item, market_session_id=m["market_session_id"]), f"Registry durable record invalid: {key}")
        status = str(item.get("quality_status") or "")
        for field, expected in _semantics(status).items():
            ensure(item.get(field) == expected, f"Registry durable record semantic split invalid: {key}/{field}")
    return v


def _gate_common(*, master: Mapping[str, Any], source_snapshot: Mapping[str, Any], reconstructed_snapshot: Mapping[str, Any],
                 source_commit_sha: str, workflow_run_id: str, validator_release_sha256: str, verified_at: str) -> dict[str, Any]:
    ensure(str(workflow_run_id or ""), "gate workflow run id missing")
    return {
        "schema_version": "2.0.0",
        "architecture_version": bootstrap.ARCHITECTURE_VERSION,
        "attestation_class": ATTESTATION_CLASS,
        "clean_checkout": True,
        "local_state_used": False,
        "source_commit_sha": _require_commit(source_commit_sha),
        "workflow_run_id": str(workflow_run_id),
        "validator_release_sha256": _require_hash(validator_release_sha256, "gate validator release hash"),
        "verified_at": str(verified_at),
        "master_sha256": master["master_sha256"],
        "master_content_sha256": master["master_content_sha256"],
        "source_registry_snapshot_sha256": source_snapshot["snapshot_sha256"],
        "reconstructed_registry_snapshot_sha256": reconstructed_snapshot["snapshot_sha256"],
    }


def _same_records(left: Mapping[str, Any], right: Mapping[str, Any], keys: list[str]) -> bool:
    return all(left.get(key) == right.get(key) for key in keys)


def runtime_reconstruction_gate(master_value: Mapping[str, Any], source_snapshot_value: Mapping[str, Any],
                                reconstructed_snapshot_value: Mapping[str, Any], *, source_commit_sha: str,
                                workflow_run_id: str, reconstructed_at: str,
                                validator_release_sha256: str) -> dict[str, Any]:
    master = bootstrap.validate_master(master_value)
    source = validate_durable_snapshot(source_snapshot_value, master=master)
    reconstructed = validate_durable_snapshot(reconstructed_snapshot_value, master=master)
    canary = list(master["canary_scope_keys"])
    source_count = sum(key in source["records"] for key in canary)
    reconstructed_count = sum(key in reconstructed["records"] for key in canary)
    equal = _same_records(source["records"], reconstructed["records"], canary)
    passed = source_count == len(canary) and reconstructed_count == len(canary) and equal
    common = _gate_common(
        master=master,
        source_snapshot=source,
        reconstructed_snapshot=reconstructed,
        source_commit_sha=source_commit_sha,
        workflow_run_id=workflow_run_id,
        validator_release_sha256=validator_release_sha256,
        verified_at=reconstructed_at,
    )
    body = {
        **common,
        "kind": RUNTIME_GATE_KIND,
        "canary_scope_count": len(canary),
        "source_canary_durable_count": source_count,
        "reconstructed_canary_durable_count": reconstructed_count,
        "canary_records_identical": equal,
        "runtime_reconstruction_passed": passed,
    }
    value = {**body, "gate_sha256": semantic_hash(body)}
    return _validate_gate(value, kind=RUNTIME_GATE_KIND, master=master)


def _validate_reuse_results(value: Mapping[str, Any], *, snapshot: Mapping[str, Any], keys: list[str]) -> dict[str, dict[str, Any]]:
    ensure(set(value) == set(keys), "PILOT reuse result scope set mismatch")
    output: dict[str, dict[str, Any]] = {}
    for key in keys:
        raw = value[key]
        ensure(isinstance(raw, Mapping), f"PILOT reuse result invalid: {key}")
        before = snapshot["records"].get(key)
        ensure(isinstance(before, Mapping), f"PILOT source snapshot missing scope: {key}")
        item = {
            "passport_hash": str(raw.get("passport_hash") or ""),
            "entry_sha256": str(raw.get("entry_sha256") or ""),
            "receipt_sha256": str(raw.get("receipt_sha256") or ""),
            "reused_without_deep_research": raw.get("reused_without_deep_research") is True,
            "policy_compatible": raw.get("policy_compatible") is True,
            "overlay_compatible": raw.get("overlay_compatible") is True,
        }
        for field in ("passport_hash", "entry_sha256", "receipt_sha256"):
            _require_hash(item[field], f"PILOT reuse {field}")
            ensure(item[field] == before[field], f"PILOT reuse result changed durable identity: {key}/{field}")
        ensure(item["reused_without_deep_research"] and item["policy_compatible"] and item["overlay_compatible"], f"PILOT reuse verification failed: {key}")
        output[key] = item
    return output


def pilot_registry_gate(master_value: Mapping[str, Any], source_snapshot_value: Mapping[str, Any],
                        reconstructed_snapshot_value: Mapping[str, Any], *, reuse_results: Mapping[str, Any],
                        source_commit_sha: str, workflow_run_id: str, verified_at: str,
                        validator_release_sha256: str) -> dict[str, Any]:
    master = bootstrap.validate_master(master_value)
    source = validate_durable_snapshot(source_snapshot_value, master=master)
    reconstructed = validate_durable_snapshot(reconstructed_snapshot_value, master=master)
    pilot = list(master["pilot_scope_keys"])
    source_count = sum(key in source["records"] for key in pilot)
    reconstructed_count = sum(key in reconstructed["records"] for key in pilot)
    lost = sorted(key for key in pilot if key in source["records"] and source["records"].get(key) != reconstructed["records"].get(key))
    reusable_keys = [key for key in pilot if str((source["records"].get(key) or {}).get("quality_status") or "") in QUALITY_RESOLVED]
    blocked_keys = [key for key in pilot if str((source["records"].get(key) or {}).get("quality_status") or "") == "insufficient_data"]
    reuse = _validate_reuse_results(reuse_results, snapshot=source, keys=reusable_keys)
    all_reused = len(reuse) == len(reusable_keys)
    blocked_reconstructed = all(source["records"].get(key) == reconstructed["records"].get(key) for key in blocked_keys)
    passed = source_count == len(pilot) and reconstructed_count == len(pilot) and not lost and all_reused and blocked_reconstructed
    common = _gate_common(
        master=master,
        source_snapshot=source,
        reconstructed_snapshot=reconstructed,
        source_commit_sha=source_commit_sha,
        workflow_run_id=workflow_run_id,
        validator_release_sha256=validator_release_sha256,
        verified_at=verified_at,
    )
    body = {
        **common,
        "kind": PILOT_GATE_KIND,
        "pilot_scope_count": len(pilot),
        "source_pilot_durable_count": source_count,
        "reconstructed_pilot_durable_count": reconstructed_count,
        "registry_loss_count": len(lost),
        "lost_scope_keys": lost,
        "reuse_verified_count": len(reuse),
        "blocked_reconstruction_verified_count": len(blocked_keys) if blocked_reconstructed else 0,
        "blocked_scope_keys": blocked_keys,
        "reuse_results_sha256": semantic_hash(reuse),
        "pilot_gate_passed": passed,
    }
    value = {**body, "gate_sha256": semantic_hash(body)}
    return _validate_gate(value, kind=PILOT_GATE_KIND, master=master)


def _validate_gate(value: Mapping[str, Any] | None, *, kind: str, master: Mapping[str, Any]) -> dict[str, Any] | None:
    if value is None:
        return None
    v = _hash(value, "gate_sha256", "campaign gate")
    ensure(v.get("schema_version") == "2.0.0" and v.get("kind") == kind, "campaign gate kind invalid")
    ensure(v.get("architecture_version") == bootstrap.ARCHITECTURE_VERSION, "campaign gate architecture mismatch")
    ensure(v.get("attestation_class") == ATTESTATION_CLASS and v.get("clean_checkout") is True and v.get("local_state_used") is False, "campaign gate was not computed by a clean GitHub run")
    _require_commit(v.get("source_commit_sha"))
    ensure(str(v.get("workflow_run_id") or ""), "campaign gate workflow run id missing")
    _require_hash(v.get("validator_release_sha256"), "campaign gate validator release hash")
    _require_hash(v.get("source_registry_snapshot_sha256"), "campaign gate source Registry snapshot hash")
    _require_hash(v.get("reconstructed_registry_snapshot_sha256"), "campaign gate reconstructed Registry snapshot hash")
    ensure(v.get("master_sha256") == master["master_sha256"] and v.get("master_content_sha256") == master["master_content_sha256"], "campaign gate MASTER mismatch")
    if kind == RUNTIME_GATE_KIND:
        count = len(master["canary_scope_keys"])
        ensure(v.get("canary_records_identical") is True and v.get("runtime_reconstruction_passed") is True, "runtime reconstruction gate evidence invalid")
        ensure(int(v.get("canary_scope_count") or -1) == count, "runtime reconstruction gate scope count invalid")
        ensure(int(v.get("source_canary_durable_count") or -1) == count and int(v.get("reconstructed_canary_durable_count") or -1) == count, "runtime reconstruction gate durable count invalid")
    elif kind == PILOT_GATE_KIND:
        count = len(master["pilot_scope_keys"])
        ensure(int(v.get("registry_loss_count") if v.get("registry_loss_count") is not None else -1) == 0 and list(v.get("lost_scope_keys") or []) == [], "PILOT gate Registry loss detected")
        ensure(int(v.get("pilot_scope_count") or -1) == count, "PILOT gate scope count invalid")
        ensure(int(v.get("source_pilot_durable_count") or -1) == count and int(v.get("reconstructed_pilot_durable_count") or -1) == count, "PILOT gate durable count invalid")
        reuse_count = int(v.get("reuse_verified_count") if v.get("reuse_verified_count") is not None else -1)
        blocked_count = int(v.get("blocked_reconstruction_verified_count") if v.get("blocked_reconstruction_verified_count") is not None else -1)
        blocked_keys = list(v.get("blocked_scope_keys") or [])
        ensure(reuse_count >= 0 and blocked_count >= 0 and reuse_count + blocked_count == count, "PILOT gate reuse/blocker accounting invalid")
        ensure(blocked_count == len(blocked_keys) and v.get("pilot_gate_passed") is True, "PILOT gate blocked-state reconstruction invalid")
        _require_hash(v.get("reuse_results_sha256"), "PILOT gate reuse result hash")
    else:
        raise ValueError("unsupported campaign gate kind")
    return v


def _counts(master: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, int]:
    records = snapshot["records"]
    master_keys = _scope_keys(master)
    canary = list(master["canary_scope_keys"])
    pilot = list(master["pilot_scope_keys"])
    statuses = [str(item["quality_status"]) for item in records.values()]
    return {
        "master_durable_count": sum(key in records for key in master_keys),
        "terminal_attempt_count": len(statuses),
        "canary_durable_count": sum(key in records for key in canary),
        "pilot_durable_count": sum(key in records for key in pilot),
        "quality_resolved_count": sum(status in QUALITY_RESOLVED for status in statuses),
        "quality_unknown_count": sum(status not in QUALITY_RESOLVED for status in statuses),
        "competitive_resolved_count": sum(status in COMPETITIVE_RESOLVED for status in statuses),
        "competitive_unresolved_count": sum(status not in COMPETITIVE_RESOLVED for status in statuses),
        "durable_blocker_count": sum(status == "insufficient_data" for status in statuses),
    }


def _phase(master: Mapping[str, Any], snapshot: Mapping[str, Any], runtime_gate_value: Mapping[str, Any] | None,
           pilot_gate_value: Mapping[str, Any] | None) -> tuple[str, bool, bool]:
    counts = _counts(master, snapshot)
    canary_complete = counts["canary_durable_count"] == len(master["canary_scope_keys"])
    pilot_complete = counts["pilot_durable_count"] == len(master["pilot_scope_keys"])
    runtime_ok = bool(runtime_gate_value and runtime_gate_value.get("runtime_reconstruction_passed") is True and canary_complete)
    pilot_ok = bool(pilot_gate_value and pilot_gate_value.get("pilot_gate_passed") is True and pilot_complete)
    if not runtime_ok:
        return "CANARY", runtime_ok, pilot_ok
    if not pilot_ok:
        return "PILOT", runtime_ok, pilot_ok
    if counts["master_durable_count"] == bootstrap.MASTER_SIZE:
        return "COMPLETE", runtime_ok, pilot_ok
    return "CORE500", runtime_ok, pilot_ok


def build_state(bundle_value: Mapping[str, Any], durable_by_scope: Mapping[str, Mapping[str, Any]], *,
                runtime_gate_value: Mapping[str, Any] | None = None,
                pilot_gate_value: Mapping[str, Any] | None = None,
                previous_state: Mapping[str, Any] | None = None,
                generated_at: str = "") -> dict[str, Any]:
    bundle = bootstrap.validate_master_bundle(bundle_value)
    master = bundle["master"]
    snapshot = durable_snapshot(master, durable_by_scope)
    runtime_gate = _validate_gate(runtime_gate_value, kind=RUNTIME_GATE_KIND, master=master)
    pilot_gate = _validate_gate(pilot_gate_value, kind=PILOT_GATE_KIND, master=master)
    phase, runtime_ok, pilot_ok = _phase(master, snapshot, runtime_gate, pilot_gate)
    if previous_state is not None:
        previous = validate_state(previous_state, bundle=bundle)
        ensure(PHASES.index(phase) >= PHASES.index(previous["phase"]), "campaign backward transition is forbidden")
        ensure(PHASES.index(phase) <= PHASES.index(previous["phase"]) + 1, "campaign phase skip is forbidden")
    counts = _counts(master, snapshot)
    scopes = {str(x["research_scope_key"]): x for x in master["scopes"]}
    phase_keys = master["canary_scope_keys"] if phase == "CANARY" else master["pilot_scope_keys"] if phase == "PILOT" else _scope_keys(master)
    next_scopes = [
        {k: scopes[key].get(k) for k in ("rank", "ticker", "contract_id", "issuer_id", "security_overlay", "research_scope_key", "bootstrap_best_lane", "bootstrap_priority_score")}
        for key in phase_keys if key not in snapshot["records"]
    ]
    input_body = {
        "master_sha256": master["master_sha256"],
        "selector_certificate_sha256": master["selector_certificate_sha256"],
        "registry_snapshot_sha256": snapshot["snapshot_sha256"],
        "runtime_gate_sha256": runtime_gate.get("gate_sha256") if runtime_gate else None,
        "pilot_gate_sha256": pilot_gate.get("gate_sha256") if pilot_gate else None,
    }
    body = {
        "schema_version": "2.0.0",
        "kind": STATE_KIND,
        "architecture_version": bootstrap.ARCHITECTURE_VERSION,
        "state_machine_version": load_policy()["campaign"]["state_machine_version"],
        "master_sha256": master["master_sha256"],
        "master_content_sha256": master["master_content_sha256"],
        "selector_certificate_sha256": master["selector_certificate_sha256"],
        "market_session_id": master["market_session_id"],
        "campaign_input_sha256": semantic_hash(input_body),
        "registry_snapshot_sha256": snapshot["snapshot_sha256"],
        "phase": phase,
        "canary_scope_count": len(master["canary_scope_keys"]),
        "canary_durable_count": counts["canary_durable_count"],
        "pilot_scope_count": len(master["pilot_scope_keys"]),
        "pilot_durable_count": counts["pilot_durable_count"],
        "master_scope_count": bootstrap.MASTER_SIZE,
        **counts,
        "canary_durable_complete": counts["canary_durable_count"] == len(master["canary_scope_keys"]),
        "runtime_reconstruction_gate_passed": runtime_ok,
        "pilot_registry_loss_gate_passed": pilot_ok,
        "core500_attempts_complete": counts["master_durable_count"] == bootstrap.MASTER_SIZE,
        "quality_unknowns_do_not_become_resolved": counts["quality_unknown_count"] > 0,
        "daily_broad_allowed": phase == "COMPLETE",
        "next_scope_count": len(next_scopes),
        "next_scopes": next_scopes[:12],
        "generated_at": str(generated_at),
    }
    value = {**body, "state_sha256": semantic_hash(body)}
    return validate_state(value, bundle=bundle)


def validate_state(value: Mapping[str, Any], *, bundle: Mapping[str, Any]) -> dict[str, Any]:
    master = bootstrap.validate_master_bundle(bundle)["master"]
    v = _hash(value, "state_sha256", "campaign state")
    ensure(v.get("schema_version") == "2.0.0" and v.get("kind") == STATE_KIND, "invalid V4.2 campaign state")
    ensure(v.get("architecture_version") == bootstrap.ARCHITECTURE_VERSION, "campaign state architecture mismatch")
    ensure(v.get("state_machine_version") == load_policy()["campaign"]["state_machine_version"], "campaign state model mismatch")
    ensure(v.get("master_sha256") == master["master_sha256"] and v.get("master_content_sha256") == master["master_content_sha256"], "campaign state MASTER mismatch")
    ensure(v.get("selector_certificate_sha256") == master["selector_certificate_sha256"], "campaign state certificate mismatch")
    ensure(v.get("phase") in PHASES, "campaign state phase invalid")
    ensure(int(v.get("master_scope_count") or -1) == bootstrap.MASTER_SIZE, "campaign state MASTER count invalid")
    durable = int(v.get("master_durable_count") if v.get("master_durable_count") is not None else -1)
    ensure(0 <= durable <= bootstrap.MASTER_SIZE and int(v.get("terminal_attempt_count", -1)) == durable, "campaign state durable attempt count invalid")
    ensure(int(v.get("quality_resolved_count") or 0) + int(v.get("quality_unknown_count") or 0) == durable, "campaign quality resolution counts invalid")
    ensure(int(v.get("competitive_resolved_count") or 0) + int(v.get("competitive_unresolved_count") or 0) == durable, "campaign competitive resolution counts invalid")
    ensure(int(v.get("durable_blocker_count") or 0) == int(v.get("quality_unknown_count") or 0), "campaign blocker count invalid")
    if v.get("phase") != "COMPLETE":
        ensure(v.get("daily_broad_allowed") is False, "ordinary daily broad is forbidden before COMPLETE")
    else:
        ensure(durable == bootstrap.MASTER_SIZE and v.get("daily_broad_allowed") is True and v.get("core500_attempts_complete") is True, "COMPLETE state invalid")
    if v.get("phase") in {"CORE500", "COMPLETE"}:
        ensure(v.get("pilot_registry_loss_gate_passed") is True, "CORE500 or COMPLETE without PILOT gate")
    if v.get("phase") in {"PILOT", "CORE500", "COMPLETE"}:
        ensure(v.get("runtime_reconstruction_gate_passed") is True, "phase advanced without runtime reconstruction gate")
    return v


def state_pointer(state_value: Mapping[str, Any], *, state_path: str, published_at: str,
                  producer_release_sha256: str) -> dict[str, Any]:
    state = dict(state_value)
    expected_path = f"data/v42/campaigns/{state.get('master_sha256')}/state.json"
    ensure(str(state_path) == expected_path, "campaign state pointer path invalid")
    body = {
        "schema_version": "2.0.0",
        "kind": POINTER_KIND,
        "architecture_version": bootstrap.ARCHITECTURE_VERSION,
        "state_path": str(state_path),
        "state_sha256": str(state["state_sha256"]),
        "master_sha256": str(state["master_sha256"]),
        "phase": str(state["phase"]),
        "daily_broad_allowed": state.get("daily_broad_allowed") is True,
        "published_at": str(published_at),
        "producer_release_sha256": str(producer_release_sha256),
    }
    return validate_state_pointer({**body, "pointer_sha256": semantic_hash(body)}, state=state)


def validate_state_pointer(value: Mapping[str, Any], *, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    v = _hash(value, "pointer_sha256", "campaign pointer")
    ensure(v.get("schema_version") == "2.0.0" and v.get("kind") == POINTER_KIND, "invalid V4.2 campaign pointer")
    ensure(v.get("architecture_version") == bootstrap.ARCHITECTURE_VERSION, "campaign pointer architecture mismatch")
    master_sha = _require_hash(v.get("master_sha256"), "campaign pointer MASTER hash")
    ensure(str(v.get("state_path") or "") == f"data/v42/campaigns/{master_sha}/state.json", "campaign pointer state path invalid")
    ensure(v.get("phase") in PHASES and isinstance(v.get("daily_broad_allowed"), bool), "campaign pointer phase invalid")
    _require_hash(v.get("state_sha256"), "campaign pointer state hash")
    _require_hash(v.get("producer_release_sha256"), "campaign pointer producer release hash")
    if state is not None:
        s = dict(state)
        ensure(v.get("state_sha256") == s.get("state_sha256") and master_sha == s.get("master_sha256") and v.get("phase") == s.get("phase") and v.get("daily_broad_allowed") == s.get("daily_broad_allowed"), "campaign pointer state binding mismatch")
    return v


def plan_wave(state_value: Mapping[str, Any], *, bundle: Mapping[str, Any]) -> dict[str, Any]:
    state = validate_state(state_value, bundle=bundle)
    wave_size = int(load_policy()["bootstrap"]["wave_size"])
    plans = list(state.get("next_scopes") or [])[:wave_size]
    return {
        "schema_version": "2.0.0",
        "kind": WAVE_KIND,
        "architecture_version": bootstrap.ARCHITECTURE_VERSION,
        "master_sha256": state["master_sha256"],
        "state_sha256": state["state_sha256"],
        "phase": state["phase"],
        "wave_size": wave_size,
        "plans": plans,
        "daily_broad_allowed": state["daily_broad_allowed"],
        "remaining_after_wave_lower_bound": max(0, int(state["next_scope_count"]) - len(plans)),
    }
