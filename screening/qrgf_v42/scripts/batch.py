#!/usr/bin/env python3
"""Evaluate one frozen deep-stage wave in-process and preserve exact order."""

from __future__ import annotations

from typing import Any, Mapping

from common import ensure, load_policy
from research import evaluate_l3, evaluate_l4, evaluate_l5

EVALUATORS = {"L3": evaluate_l3, "L4": evaluate_l4, "L5": evaluate_l5}


def evaluate(stage: str, payloads: Any) -> list[dict[str, Any]]:
    ensure(stage in EVALUATORS, "batch stage must be L3, L4 or L5")
    ensure(isinstance(payloads, list) and payloads, "batch input must be a non-empty array")
    ensure(len(payloads) <= int(load_policy()["waves"][stage]), f"{stage} batch exceeds wave limit")
    identities: list[tuple[str, str]] = []
    normalized: list[Mapping[str, Any]] = []
    for payload in payloads:
        ensure(isinstance(payload, Mapping), "batch item must be an object")
        identity = (str(payload.get("ticker") or "").upper(), str(payload.get("contract_id") or ""))
        ensure(all(identity), "batch item identity missing")
        identities.append(identity)
        normalized.append(payload)
    ensure(len(set(identities)) == len(identities), "batch contains duplicate identity")
    results = [EVALUATORS[stage](payload) for payload in normalized]
    ensure([(row["ticker"], row["contract_id"]) for row in results] == identities, "batch evaluator changed identity or order")
    return results
