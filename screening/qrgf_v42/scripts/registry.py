#!/usr/bin/env python3
"""V4 durable Structural Quality Registry contracts.

Bootstrap work is considered complete only after proposal -> publish -> readback
validation of pointer + immutable Passport. The Registry remains a cache, never
a universe whitelist.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any, Mapping

from common import ensure, load_connectors, load_policy, semantic_hash
from contracts import validate as validate_contract
import passport

SENSITIVE = {
    "account","account_id","account_number","balances","positions","allocation",
    "available_funds","buying_power","cash_balance","quote","bid","ask",
    "raw_connector_response","licensed_payload","raw_payload",
}


def _reject_sensitive(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            dotted=f"{path}.{key}" if path else str(key)
            ensure(str(key).lower() not in SENSITIVE, f"sensitive field in v4 Registry payload: {dotted}")
            _reject_sensitive(child,dotted)
    elif isinstance(value,list):
        for i,child in enumerate(value): _reject_sensitive(child,f"{path}[{i}]")


def pointer_path(research_scope_key: str) -> str:
    key=str(research_scope_key or "").strip(); ensure(key,"v4 Registry scope key missing")
    digest=hashlib.sha256(key.encode("utf-8")).hexdigest()
    prefix=load_connectors()["quality_registry_v4"]["scope_pointer_prefix"].rstrip("/")
    return f"{prefix}/{digest}.json"


def passport_path(research_scope_key: str, passport_hash: str) -> str:
    key=str(research_scope_key or "").strip(); ensure(key,"v4 Passport scope key missing")
    ensure(len(str(passport_hash))==64,"v4 Passport hash invalid")
    digest=hashlib.sha256(key.encode("utf-8")).hexdigest()
    prefix=load_connectors()["quality_registry_v4"]["passport_prefix"].rstrip("/")
    return f"{prefix}/{digest}/{passport_hash}.json"


def proposal(result: Mapping[str, Any], *, research_scope_key: str, event_scan_through: str | None) -> dict[str, Any]:
    summary=passport.durable_summary(result)
    issuer=str(summary.get("issuer_id") or ""); ensure(issuer,"v4 Passport issuer missing")
    overlay=str(research_scope_key).rsplit("|",1)[-1]
    ensure(research_scope_key==f"{issuer}|{overlay}","v4 Passport proposal scope mismatch")
    expected_listing="adr" if overlay=="adr" else "none"
    if overlay=="etf": expected_listing="none"
    ensure(str(summary.get("listing_overlay") or "none").lower()==expected_listing,"v4 Passport proposal listing overlay mismatch")
    base=passport.proposal(result,event_scan_through=event_scan_through)
    payload=dict(base["passport_payload"])
    body={
        "schema_version":"1.0.0","kind":"qrgf_v4_passport_update_proposal",
        "research_scope_key":research_scope_key,"issuer_id":issuer,"security_overlay":overlay,
        "quality_policy_version":summary.get("quality_policy_version"),"event_scan_through":event_scan_through,
        "summary":summary,"passport_sha256":semantic_hash(payload),"passport_payload":payload,
    }
    _reject_sensitive(body)
    value={**body,"proposal_sha256":semantic_hash(body)}
    validate_proposal(value)
    return value


def validate_proposal(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_contract("v4-passport-update-proposal",value); p=dict(value)
    body={k:v for k,v in p.items() if k!="proposal_sha256"}
    ensure(p.get("proposal_sha256")==semantic_hash(body),"v4 proposal self hash mismatch")
    issuer=str(p.get("issuer_id") or ""); overlay=str(p.get("security_overlay") or "")
    ensure(p.get("research_scope_key")==f"{issuer}|{overlay}","v4 proposal scope identity mismatch")
    ensure(p.get("passport_sha256")==semantic_hash(p.get("passport_payload") or {}),"v4 proposal Passport hash mismatch")
    ensure(p.get("quality_policy_version")==load_policy()["quality_registry"]["quality_policy_version"],"v4 proposal quality policy mismatch")
    _reject_sensitive(p)
    return p


def freshness_proposal(*, entry: Mapping[str, Any], event_scan_through: str, delta_status: str) -> dict[str, Any]:
    current=validate_entry(entry)
    ensure(delta_status in {"no_structural_change","needs_refresh","invalidated"},"invalid v4 delta status")
    status={"no_structural_change":"fresh","needs_refresh":"needs_refresh","invalidated":"invalidated"}[delta_status]
    body={
        "schema_version":"1.0.0","kind":"qrgf_v4_freshness_update_proposal",
        "research_scope_key":current["research_scope_key"],"issuer_id":current["issuer_id"],"security_overlay":current["security_overlay"],
        "passport_hash":current["passport_hash"],"quality_policy_version":current["quality_policy_version"],
        "event_scan_through":event_scan_through,"freshness_status":status,"delta_status":delta_status,
    }
    return {**body,"proposal_sha256":semantic_hash(body)}


def validate_freshness_proposal(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_contract("v4-freshness-update-proposal",value); p=dict(value)
    body={k:v for k,v in p.items() if k!="proposal_sha256"}
    ensure(p.get("proposal_sha256")==semantic_hash(body),"v4 freshness proposal self hash mismatch")
    ensure(p.get("research_scope_key")==f"{p.get('issuer_id')}|{p.get('security_overlay')}","v4 freshness scope mismatch")
    ensure(p.get("quality_policy_version")==load_policy()["quality_registry"]["quality_policy_version"],"v4 freshness quality policy mismatch")
    return p


def validate_passport(value: Mapping[str, Any], *, entry: Mapping[str, Any]) -> dict[str, Any]:
    e=validate_entry(entry); validate_contract("quality-passport",value); p=dict(value)
    ensure(semantic_hash(p)==str(e.get("passport_hash") or ""),"v4 immutable Passport content hash mismatch")
    ensure(str(p.get("issuer_id") or "")==str(e.get("issuer_id") or ""),"v4 Passport issuer mismatch")
    ensure(str(p.get("quality_policy_version") or "")==str(e.get("quality_policy_version") or ""),"v4 Passport policy mismatch")
    summary=p.get("summary") if isinstance(p.get("summary"),Mapping) else {}
    for field in ("quality_status","quality_score","quality_coverage_pct","quality_eligible","next_review_date"):
        ensure(summary.get(field)==e.get(field),f"v4 Registry/Passport {field} mismatch")
    ensure(str(summary.get("reuse_class") or "")=="structural_quality","v4 Passport not Structural Quality")
    return p


def validate_entry(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_contract("v4-registry-entry",value); e=dict(value)
    body={k:v for k,v in e.items() if k!="entry_sha256"}
    ensure(e.get("entry_sha256")==semantic_hash(body),"v4 Registry entry self hash mismatch")
    issuer=str(e.get("issuer_id") or ""); overlay=str(e.get("security_overlay") or "")
    key=f"{issuer}|{overlay}"; ensure(e.get("research_scope_key")==key,"v4 Registry scope mismatch")
    ensure(e.get("passport_path")==passport_path(key,str(e.get("passport_hash") or "")),"v4 Registry Passport path mismatch")
    ensure(e.get("quality_policy_version")==load_policy()["quality_registry"]["quality_policy_version"],"v4 Registry quality policy mismatch")
    if str(e.get("quality_status") or "") == "insufficient_data":
        review = str(e.get("next_review_date") or "").strip()
        ensure(review, "insufficient_data Registry entry requires next_review_date")
        try:
            dt.date.fromisoformat(review)
        except ValueError as exc:
            raise ValueError("Registry next_review_date must use YYYY-MM-DD") from exc
    return e


def validate_receipt_record(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_contract("v4-registry-receipt",value); r=dict(value)
    body={k:v for k,v in r.items() if k!="receipt_sha256"}
    ensure(r.get("receipt_sha256")==semantic_hash(body),"v4 Registry receipt self hash mismatch")
    return r


def validate_receipt(value: Mapping[str, Any], *, entry: Mapping[str, Any], passport_value: Mapping[str, Any]) -> dict[str, Any]:
    r=validate_receipt_record(value); e=validate_entry(entry); p=validate_passport(passport_value,entry=e)
    ensure(r.get("research_scope_key")==e["research_scope_key"],"v4 receipt scope mismatch")
    ensure(r.get("passport_hash")==e["passport_hash"],"v4 receipt Passport mismatch")
    ensure(r.get("entry_sha256")==e["entry_sha256"],"v4 receipt entry mismatch")
    ensure(semantic_hash(p)==r.get("passport_hash"),"v4 receipt Passport bytes mismatch")
    return r


def reuse(entry: Mapping[str, Any], *, passport_value: Mapping[str, Any], market_session_id: str) -> dict[str, Any] | None:
    e=validate_entry(entry); p=validate_passport(passport_value,entry=e)
    if str(e.get("freshness_status") or "")!="fresh": return None
    if str(e.get("event_scan_through") or "") < str(market_session_id): return None
    if str(e.get("quality_status") or "") not in set(load_policy()["quality_registry"]["reusable_quality_statuses"]): return None
    summary=dict(p["summary"]); summary["passport_hash"]=e["passport_hash"]; summary["reused_from_registry"]=True
    return {"research_scope_key":e["research_scope_key"],"issuer_id":e["issuer_id"],"security_overlay":e["security_overlay"],"quality":summary,"passport_path":e["passport_path"]}



def batch_proposal(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    ensure(isinstance(items,list) and 1 <= len(items) <= int(load_policy()["bootstrap"]["wave_size"]),"v4 Registry batch must contain 1..wave_size proposals")
    normalized=[]
    scopes=set()
    for item in items:
        p=validate_proposal(item) if str(item.get("kind") or "")=="qrgf_v4_passport_update_proposal" else validate_freshness_proposal(item)
        scope=str(p["research_scope_key"]); ensure(scope not in scopes,"v4 Registry batch contains duplicate scope"); scopes.add(scope); normalized.append(p)
    body={"schema_version":"1.0.0","kind":"qrgf_v4_registry_batch","items":normalized}
    return {**body,"batch_sha256":semantic_hash(body)}

def validate_batch(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_contract("v4-registry-batch",value); v=dict(value); body={k:x for k,x in v.items() if k!="batch_sha256"}
    ensure(v.get("batch_sha256")==semantic_hash(body),"v4 Registry batch self hash mismatch")
    scopes=set()
    for item in v["items"]:
        if str(item.get("kind") or "")=="qrgf_v4_passport_update_proposal": p=validate_proposal(item)
        elif str(item.get("kind") or "")=="qrgf_v4_freshness_update_proposal": p=validate_freshness_proposal(item)
        else: raise ValueError("unsupported v4 Registry batch item")
        scope=str(p["research_scope_key"]); ensure(scope not in scopes,"v4 Registry batch duplicate scope"); scopes.add(scope)
    return v

def bootstrap_durable_complete(*, receipt: Mapping[str, Any], entry: Mapping[str, Any], passport_value: Mapping[str, Any]) -> dict[str, Any]:
    r=validate_receipt(receipt,entry=entry,passport_value=passport_value); e=validate_entry(entry)
    status=str(e.get("quality_status") or "")
    reviewed=status in set(load_policy()["bootstrap"]["durable_terminal_statuses"])
    resolved=status in {"pass","conditional","rejected"}
    return {"research_scope_key":e["research_scope_key"],"quality_status":status,"durable_reviewed":reviewed,"quality_resolved":resolved,"durable_incomplete":reviewed and not resolved,"next_review_date":e.get("next_review_date"),"receipt_sha256":r["receipt_sha256"],"passport_hash":e["passport_hash"]}
