#!/usr/bin/env python3
"""Optional position sizing, isolated from stock analysis and order creation."""

from __future__ import annotations

import math
from typing import Any, Mapping

from common import number, truth


def calculate(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("explicit_user_request") is not True:
        raise ValueError("position sizing requires an explicit user request")
    entry = number(payload.get("entry_price"))
    if entry is None or entry <= 0:
        raise ValueError("entry_price must be positive")
    if payload.get("amount") is not None:
        amount = number(payload.get("amount"))
        mode = "fixed_amount"
    else:
        account_value, percent = number(payload.get("account_value")), number(payload.get("percent_of_capital"))
        if account_value is None or percent is None:
            raise ValueError("provide amount or account_value plus percent_of_capital")
        if not 0 <= percent <= 100:
            raise ValueError("percent_of_capital must be 0-100 without explicit leverage")
        amount, mode = account_value * percent / 100, "percent_of_capital"
    if amount is None or amount < 0:
        raise ValueError("position amount is invalid")
    use_margin = truth(payload.get("use_margin")) if payload.get("use_margin") is not None else False
    if use_margin is not False:
        raise ValueError("this skill does not size margin positions by default")
    fractional = truth(payload.get("allow_fractional_shares")) if payload.get("allow_fractional_shares") is not None else True
    if fractional is None:
        raise ValueError("allow_fractional_shares must be boolean")
    shares_raw = amount / entry
    shares = shares_raw if fractional else math.floor(shares_raw)
    invested = shares * entry
    return {
        "module": "optional_position_sizing", "sizing_mode": mode, "entry_price": entry,
        "requested_amount": amount, "shares": round(shares, 8),
        "estimated_invested_amount": round(invested, 2), "unused_amount": round(amount - invested, 2),
        "uses_margin": False, "order_created": False, "core_scores_unchanged": True,
        "down_10_pct": round(-invested * 0.10, 2), "down_20_pct": round(-invested * 0.20, 2),
        "down_30_pct": round(-invested * 0.30, 2), "up_5_pct": round(invested * 0.05, 2),
        "up_7_pct": round(invested * 0.07, 2),
    }
