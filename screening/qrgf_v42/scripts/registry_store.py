#!/usr/bin/env python3
"""Deterministic V4 Registry single-writer reference implementation.

The proposal directory is an append-only journal. Receipts are idempotency keys:
replaying an already receipted proposal is a verified no-op, even when the new
run has a different published_at or producer release. Scope pointers move only
forward by logical version; proposal hashes are never used as recency order.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from common import ensure, load_connectors, semantic_hash, write_json
import registry


def _read(path: Path) -> dict[str, Any]:
    value=json.loads(path.read_text(encoding="utf-8-sig"))
    ensure(isinstance(value,dict),f"{path} must contain an object")
    return value


def _write_immutable(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        ensure(semantic_hash(_read(path))==semantic_hash(value),f"immutable collision at {path}")
        return
    write_json(path,dict(value))


def _receipt_path(root: Path, proposal_sha256: str) -> Path:
    prefix=load_connectors()["quality_registry_v4"]["receipt_prefix"].rstrip("/")
    return root/f"{prefix}/{proposal_sha256}.json"


def _passport_file(root: Path, scope: str, passport_hash: str) -> Path:
    return root/registry.passport_path(scope,passport_hash)


def _proposal_version(p: Mapping[str, Any]) -> tuple[str,str]:
    if p.get("kind") == "qrgf_v4_passport_update_proposal":
        summary=p.get("summary") if isinstance(p.get("summary"),Mapping) else {}
        return str(p.get("event_scan_through") or ""),str(summary.get("as_of") or "")
    return str(p.get("event_scan_through") or ""),""


def _entry_version(entry: Mapping[str, Any]) -> tuple[str,str]:
    return str(entry.get("event_scan_through") or ""),str(entry.get("quality_as_of") or "")


def _dominates(a: tuple[str,str], b: tuple[str,str]) -> bool:
    return a[0] >= b[0] and a[1] >= b[1] and a != b


def _validate_receipt_record(value: Mapping[str, Any]) -> dict[str, Any]:
    return registry.validate_receipt_record(value)


def _validate_passport_hash(root: Path, scope: str, passport_hash: str) -> dict[str, Any]:
    path=_passport_file(root,scope,passport_hash)
    ensure(path.exists(),f"v4 receipt references missing Passport: {path}")
    payload=_read(path)
    ensure(semantic_hash(payload)==passport_hash,"v4 receipt Passport content hash mismatch")
    return payload


def _validate_current_entry_receipt(root: Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    proposal_sha=str(entry.get("last_proposal_sha256") or "")
    ensure(proposal_sha,"v4 current Registry pointer has no last proposal")
    rpath=_receipt_path(root,proposal_sha)
    ensure(rpath.exists(),"v4 current Registry pointer is missing its receipt")
    receipt=_validate_receipt_record(_read(rpath))
    ensure(receipt.get("proposal_sha256")==proposal_sha,"v4 current receipt proposal mismatch")
    ensure(receipt.get("research_scope_key")==entry.get("research_scope_key"),"v4 current receipt scope mismatch")
    ensure(receipt.get("passport_hash")==entry.get("passport_hash"),"v4 current receipt Passport mismatch")
    ensure(receipt.get("entry_sha256")==entry.get("entry_sha256"),"v4 current receipt entry mismatch")
    _validate_passport_hash(root,str(entry["research_scope_key"]),str(entry["passport_hash"]))
    return receipt


def _validate_existing_receipt(root: Path, p: Mapping[str, Any]) -> dict[str, Any] | None:
    proposal_sha=str(p["proposal_sha256"]); rpath=_receipt_path(root,proposal_sha)
    if not rpath.exists(): return None
    receipt=_validate_receipt_record(_read(rpath))
    scope=str(p["research_scope_key"])
    expected_ph=str(p.get("passport_sha256") or p.get("passport_hash") or "")
    ensure(receipt["proposal_sha256"]==proposal_sha,"v4 replay receipt proposal mismatch")
    ensure(receipt["research_scope_key"]==scope,"v4 replay receipt scope mismatch")
    ensure(receipt["passport_hash"]==expected_ph,"v4 replay receipt Passport mismatch")
    _validate_passport_hash(root,scope,expected_ph)
    pointer=root/registry.pointer_path(scope)
    ensure(pointer.exists(),"v4 receipted proposal has no current Registry pointer")
    entry=registry.validate_entry(_read(pointer))
    ensure(entry["research_scope_key"]==scope,"v4 replay pointer scope mismatch")
    if entry.get("last_proposal_sha256")==proposal_sha:
        ensure(receipt["entry_sha256"]==entry["entry_sha256"],"v4 replay receipt/current entry mismatch")
        return {"status":"already_applied","entry":entry,"receipt":receipt,"passport_path":entry["passport_path"],"pointer_path":registry.pointer_path(scope),"receipt_path":rpath.relative_to(root).as_posix()}
    if p.get("kind")=="qrgf_v4_passport_update_proposal":
        ensure(_dominates(_entry_version(entry),_proposal_version(p)),"v4 receipted historical Passport proposal is not dominated by current pointer")
    else:
        ensure(str(entry.get("event_scan_through") or "") > str(p.get("event_scan_through") or ""),"v4 receipted historical freshness proposal is not older than current pointer")
    return {"status":"already_applied_historical","entry":entry,"receipt":receipt,"passport_path":registry.passport_path(scope,expected_ph),"pointer_path":registry.pointer_path(scope),"receipt_path":rpath.relative_to(root).as_posix()}


def _entry_matches_passport_proposal(entry: Mapping[str, Any], p: Mapping[str, Any]) -> bool:
    s=p.get("summary") if isinstance(p.get("summary"),Mapping) else {}
    return (
        entry.get("research_scope_key")==p.get("research_scope_key") and
        entry.get("passport_hash")==p.get("passport_sha256") and
        str(entry.get("event_scan_through") or "")==str(p.get("event_scan_through") or "") and
        str(entry.get("quality_as_of") or "")==str(s.get("as_of") or "") and
        entry.get("quality_status")==s.get("quality_status") and
        entry.get("quality_score")==s.get("quality_score") and
        entry.get("quality_coverage_pct")==s.get("quality_coverage_pct") and
        bool(entry.get("quality_eligible"))==(s.get("quality_eligible") is True) and
        entry.get("next_review_date")==s.get("next_review_date")
    )


def _make_receipt(*, p: Mapping[str, Any], entry: Mapping[str, Any], published_at: str, producer_release_sha256: str) -> dict[str, Any]:
    body={
        "schema_version":"1.0.0","kind":"qrgf_v4_registry_write_receipt","proposal_sha256":p["proposal_sha256"],
        "research_scope_key":p["research_scope_key"],"passport_hash":entry["passport_hash"],"entry_sha256":entry["entry_sha256"],
        "published_at":published_at,"producer_release_sha256":producer_release_sha256,
    }
    return {**body,"receipt_sha256":semantic_hash(body)}


def apply_passport_proposal(root: Path, proposal_value: Mapping[str, Any], *, producer_release_sha256: str, published_at: str) -> dict[str, Any]:
    p=registry.validate_proposal(proposal_value)
    replay=_validate_existing_receipt(root,p)
    if replay is not None: return replay

    scope=str(p["research_scope_key"]); phash=str(p["passport_sha256"])
    ppath=_passport_file(root,scope,phash)
    _write_immutable(ppath,dict(p["passport_payload"]))
    summary=dict(p["summary"]); event_scan=p.get("event_scan_through")
    entry_body={
        "schema_version":"1.0.0","kind":"qrgf_v4_registry_scope_entry",
        "research_scope_key":scope,"issuer_id":p["issuer_id"],"security_overlay":p["security_overlay"],
        "passport_hash":phash,"passport_path":registry.passport_path(scope,phash),
        "freshness_status":"fresh" if event_scan else "needs_refresh","event_scan_through":event_scan,
        "quality_policy_version":p["quality_policy_version"],"quality_status":summary.get("quality_status"),
        "quality_score":summary.get("quality_score"),"quality_coverage_pct":summary.get("quality_coverage_pct"),
        "quality_eligible":summary.get("quality_eligible") is True,"quality_as_of":summary.get("as_of"),
        "next_review_date":summary.get("next_review_date"),
        "last_proposal_sha256":p["proposal_sha256"],"producer_release_sha256":producer_release_sha256,
    }
    candidate={**entry_body,"entry_sha256":semantic_hash(entry_body)}
    pointer=root/registry.pointer_path(scope); status="applied"
    if pointer.exists():
        old=registry.validate_entry(_read(pointer))
        if old.get("last_proposal_sha256")==p["proposal_sha256"]:
            ensure(_entry_matches_passport_proposal(old,p),"v4 partial Passport state does not match proposal")
            entry=old; status="receipt_recovered"
        else:
            _validate_current_entry_receipt(root,old)
            oldv,newv=_entry_version(old),_proposal_version(p)
            if oldv==newv:
                raise ValueError("same logical Registry version has conflicting Passport proposal")
            if _dominates(oldv,newv):
                raise ValueError("historical Passport proposal is missing its receipt; refusing to invent history")
            if not _dominates(newv,oldv):
                raise ValueError("incomparable or regressing Passport proposal version")
            entry=candidate; write_json(pointer,entry)
    else:
        entry=candidate; write_json(pointer,entry)

    receipt=_make_receipt(p=p,entry=entry,published_at=published_at,producer_release_sha256=producer_release_sha256)
    rpath=_receipt_path(root,p["proposal_sha256"]); _write_immutable(rpath,receipt)
    return {"status":status,"entry":entry,"receipt":receipt,"passport_path":entry["passport_path"],"pointer_path":registry.pointer_path(scope),"receipt_path":rpath.relative_to(root).as_posix()}


def apply_freshness_proposal(root: Path, proposal_value: Mapping[str, Any], *, producer_release_sha256: str, published_at: str) -> dict[str, Any]:
    p=registry.validate_freshness_proposal(proposal_value)
    replay=_validate_existing_receipt(root,p)
    if replay is not None: return replay

    scope=str(p["research_scope_key"]); pointer=root/registry.pointer_path(scope)
    ensure(pointer.exists(),"v4 freshness update requires existing Registry pointer")
    old=registry.validate_entry(_read(pointer)); ensure(old["passport_hash"]==p["passport_hash"],"v4 freshness proposal references stale Passport")
    current_event=str(old.get("event_scan_through") or ""); new_event=str(p["event_scan_through"])
    status="applied"
    if old.get("last_proposal_sha256")==p["proposal_sha256"]:
        ensure(current_event==new_event and old.get("freshness_status")==p.get("freshness_status"),"v4 partial freshness state does not match proposal")
        entry=old; status="receipt_recovered"
    else:
        _validate_current_entry_receipt(root,old)
        if new_event < current_event:
            raise ValueError("stale v4 freshness proposal")
        if new_event == current_event:
            raise ValueError("same freshness watermark has conflicting proposal")
        body={k:v for k,v in old.items() if k!="entry_sha256"}
        body.update({"freshness_status":p["freshness_status"],"event_scan_through":new_event,"last_proposal_sha256":p["proposal_sha256"],"producer_release_sha256":producer_release_sha256})
        entry={**body,"entry_sha256":semantic_hash(body)}; write_json(pointer,entry)

    receipt=_make_receipt(p=p,entry=entry,published_at=published_at,producer_release_sha256=producer_release_sha256)
    rpath=_receipt_path(root,p["proposal_sha256"]); _write_immutable(rpath,receipt)
    return {"status":status,"entry":entry,"receipt":receipt,"pointer_path":registry.pointer_path(scope),"receipt_path":rpath.relative_to(root).as_posix()}


def apply_batch(root: Path, batch_value: Mapping[str, Any], *, producer_release_sha256: str, published_at: str) -> dict[str, Any]:
    batch=registry.validate_batch(batch_value); results=[]
    for item in batch["items"]:
        if item["kind"]=="qrgf_v4_passport_update_proposal": results.append(apply_passport_proposal(root,item,producer_release_sha256=producer_release_sha256,published_at=published_at))
        else: results.append(apply_freshness_proposal(root,item,producer_release_sha256=producer_release_sha256,published_at=published_at))
    return {"batch_sha256":batch["batch_sha256"],"items":len(results),"applied":sum(x["status"] in {"applied","receipt_recovered"} for x in results),"skipped":sum(x["status"].startswith("already_applied") for x in results),"results":results}


def _expand_file(path: Path) -> list[dict[str, Any]]:
    value=_read(path); kind=str(value.get("kind") or "")
    if kind=="qrgf_v4_passport_update_proposal": return [registry.validate_proposal(value)]
    if kind=="qrgf_v4_freshness_update_proposal": return [registry.validate_freshness_proposal(value)]
    if kind=="qrgf_v4_registry_batch": return list(registry.validate_batch(value)["items"])
    raise ValueError(f"unsupported v4 Registry proposal kind: {kind}")


def promote_directory(root: Path, proposals_dir: Path, *, producer_release_sha256: str, published_at: str) -> dict[str, Any]:
    paths=sorted(proposals_dir.glob("*.json")) if proposals_dir.is_dir() else []
    expanded=[(path,item) for path in paths for item in _expand_file(path)]

    # Two distinct unreceipted proposals for the same scope in one promotion run
    # are ambiguous. Historical receipted proposals may coexist with newer ones.
    pending_by_scope: dict[str,str]={}
    for _,item in expanded:
        if _receipt_path(root,str(item["proposal_sha256"])).exists():
            _validate_existing_receipt(root,item)
            continue
        scope=str(item["research_scope_key"]); sha=str(item["proposal_sha256"])
        previous=pending_by_scope.get(scope)
        ensure(previous in {None,sha},f"multiple unreceipted proposals for one scope in a single promotion run: {scope}")
        pending_by_scope[scope]=sha

    results=[]
    for _,item in expanded:
        if item["kind"]=="qrgf_v4_passport_update_proposal":
            results.append(apply_passport_proposal(root,item,producer_release_sha256=producer_release_sha256,published_at=published_at))
        else:
            results.append(apply_freshness_proposal(root,item,producer_release_sha256=producer_release_sha256,published_at=published_at))
    return {"files":len(paths),"items":len(results),"applied":sum(x["status"] in {"applied","receipt_recovered"} for x in results),"skipped":sum(x["status"].startswith("already_applied") for x in results),"results":results}
