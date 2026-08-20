#!/usr/bin/env python3
"""Canonical instrument admissibility for broad and single-ticker paths."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Mapping

from common import ROOT, load_policy, number, truth


def validate(payload: Mapping[str, Any]) -> dict[str, Any]:
    policy = load_policy()["universe"]
    ticker = str(payload.get("ticker") or "").upper()
    contract_id = str(payload.get("contract_id") or "")
    security_type = str(payload.get("security_type") or "").lower()
    status = str(payload.get("instrument_status") or "").lower()
    errors: list[str] = []
    if not ticker or not contract_id:
        errors.append("instrument identity missing")
    if security_type not in policy["allowed_security_types"]:
        errors.append("security_type is ineligible")
    if status not in {"eligible", "verified", "active"}:
        errors.append("instrument is not active and eligible")
    structural = payload.get("structure") if isinstance(payload.get("structure"), Mapping) else {}
    for flag in ("leveraged", "inverse", "daily_reset", "warrant", "right", "unit", "preferred", "otc", "spac", "closed_end_fund", "exchange_traded_note"):
        if truth(structural.get(flag)) is True:
            errors.append(f"prohibited structure: {flag}")
    price = number(payload.get("current_price"))
    liquidity = number(payload.get("avg_dollar_volume"))
    market_cap = number(payload.get("market_cap"))
    if price is not None and price < policy["minimum_price_usd"]:
        errors.append("price below strategy minimum")
    if liquidity is not None and liquidity < policy["minimum_average_dollar_volume_usd"]:
        errors.append("liquidity below strategy minimum")
    if security_type == "common_equity" and market_cap is not None and market_cap < policy["minimum_known_market_cap_usd"]:
        errors.append("known market cap below strategy minimum")
    if security_type == "etf":
        allowlist = _etf_allowlist()
        approved = allowlist.get((ticker, contract_id))
        if approved is None:
            errors.append("ETF is absent from the exact allowlist")
        else:
            if approved["status"] != "approved" or approved["leverage_multiple"] != "1" or approved["inverse"] != "false" or approved["daily_reset"] != "false":
                errors.append("ETF allowlist structure is not plain approved exposure")
    return {
        "valid": not errors,
        "ticker": ticker,
        "contract_id": contract_id,
        "security_type": security_type,
        "instrument_status": status,
        "hard_vetoes": ["instrument_ineligible"] if errors else [],
        "errors": errors,
    }


def _etf_allowlist() -> dict[tuple[str, str], dict[str, str]]:
    path = ROOT / "config" / "approved-etfs.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {(row["ticker"].upper(), row["contract_id"]): row for row in csv.DictReader(handle)}
