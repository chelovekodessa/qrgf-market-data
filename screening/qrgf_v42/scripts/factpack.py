#!/usr/bin/env python3
"""V4 Structural Fact Pack: normalized primary-evidence inputs before deep L3.

A Fact Pack is not a Quality score. It contains durable structural facts,
clearances and evidence lineage only; current price/recovery/news fields are forbidden.
"""
from __future__ import annotations

from typing import Any, Mapping

from common import ensure, semantic_hash
from contracts import validate as validate_contract
import selection

FORBIDDEN_TOP_LEVEL={"quote","price","technicals","market_context","analyst_actions","events","recovery_setup_score","l2_setup_score"}


def build(payload: Mapping[str, Any]) -> dict[str, Any]:
    p=dict(payload)
    bad=sorted(k for k in p if k in FORBIDDEN_TOP_LEVEL)
    ensure(not bad,f"Structural Fact Pack contains fast/recovery fields: {bad}")
    ticker=str(p.get("ticker") or "").strip().upper(); contract=str(p.get("contract_id") or "").strip()
    ensure(ticker and contract,"Structural Fact Pack requires ticker and contract_id")
    issuer=selection.derive_issuer_id(p); overlay=selection.security_overlay(p); scope=f"{issuer}|{overlay}"
    body={
        "schema_version":"1.0.0","kind":"qrgf_v4_structural_fact_pack","ticker":ticker,"contract_id":contract,
        "issuer_id":issuer,"security_overlay":overlay,"research_scope_key":scope,"security_type":p.get("security_type"),
        "instrument_status":p.get("instrument_status"),"structure":dict(p.get("structure") or {}),"sector":p.get("sector"),
        "economic_archetype":p.get("economic_archetype"),"facts":dict(p.get("facts") or {}),"clearances":dict(p.get("clearances") or {}),
        "evidence":list(p.get("evidence") or []),"research_cutoff_at":p.get("research_cutoff_at"),"collection_status":p.get("collection_status") or "ready",
    }
    value={**body,"fact_pack_sha256":semantic_hash(body)}; validate(value); return value


def validate(value: Mapping[str, Any]) -> dict[str, Any]:
    validate_contract("v4-structural-fact-pack",value); v=dict(value); body={k:x for k,x in v.items() if k!="fact_pack_sha256"}
    ensure(v.get("fact_pack_sha256")==semantic_hash(body),"v4 Structural Fact Pack self hash mismatch")
    ensure(v.get("research_scope_key")==f"{v.get('issuer_id')}|{v.get('security_overlay')}","v4 Structural Fact Pack scope mismatch")
    return v


def to_l3_input(value: Mapping[str, Any]) -> dict[str, Any]:
    v=validate(value)
    return {k:v.get(k) for k in ("ticker","contract_id","security_type","instrument_status","structure","sector","economic_archetype","facts","clearances","evidence","research_cutoff_at","collection_status") if v.get(k) is not None}
