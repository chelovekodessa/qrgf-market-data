#!/usr/bin/env python3
"""Join analytical L5 and Interactive Brokers execution validation."""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

from common import load_connectors, load_policy, number, parse_time, semantic_hash
from contracts import validate as validate_contract

FINAL_STATUSES = {"open_now", "prepare_limit_order", "wait", "do_not_enter", "do_not_consider"}


def _nonempty_object(value: Any) -> bool:
    return isinstance(value, Mapping) and bool(value)


def validate_execution(payload: Mapping[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    validate_contract("execution-context", payload)
    policy = load_connectors()["execution"]
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    errors: list[str] = []
    if payload.get("provider") not in {"IBKR", "Interactive Brokers"}:
        errors.append("provider must be Interactive Brokers")
    ticker = str(payload.get("ticker") or "").upper()
    contract = payload.get("contract") if isinstance(payload.get("contract"), Mapping) else {}
    quote = payload.get("quote") if isinstance(payload.get("quote"), Mapping) else {}
    history = payload.get("history") if isinstance(payload.get("history"), Mapping) else {}
    account = payload.get("account") if isinstance(payload.get("account"), Mapping) else {}
    requested_status = str(payload.get("requested_status") or "wait")
    actionable = requested_status in {"open_now", "prepare_limit_order"}

    contract_ticker = str(contract.get("ticker") or "").upper()
    if not ticker or contract_ticker != ticker:
        errors.append("contract ticker mismatch")
    for field in ("contract_id", "exchange", "primary_exchange"):
        if not str(contract.get(field) or "").strip():
            errors.append(f"contract.{field} missing")
    if str(contract.get("currency") or "").upper() != "USD":
        errors.append("contract currency must be USD")
    if str(contract.get("security_type") or "") not in load_policy()["universe"]["allowed_security_types"]:
        errors.append("contract security_type is ineligible")
    if str(contract.get("identity_status") or "") != "verified_ibkr":
        errors.append("contract identity is not verified by IBKR")
    if contract.get("us_listing_verified") is not True:
        errors.append("US listing is not verified")

    last, bid, ask = number(quote.get("last")), number(quote.get("bid")), number(quote.get("ask"))
    adv = number(quote.get("avg_90d_usd_volume"))
    quote_time = parse_time(quote.get("quote_timestamp")) if quote.get("quote_timestamp") else None
    bid_ask_time = parse_time(quote.get("bid_ask_timestamp")) if quote.get("bid_ask_timestamp") else None
    max_age = float(policy["quote"]["max_open_now_quote_age_seconds"])
    if last is None or last <= 0:
        errors.append("quote.last missing or invalid")
    if bid is None or ask is None or bid <= 0 or ask < bid:
        errors.append("quote bid/ask missing or invalid")
        spread = None
    else:
        spread = 100 * (ask - bid) / ((ask + bid) / 2)
        if spread > float(policy["quote"]["max_open_now_spread_pct"]):
            errors.append("quote spread exceeds open-now limit")
    if adv is None or adv < float(policy["quote"]["minimum_average_dollar_volume_usd"]):
        errors.append("90-day dollar liquidity is insufficient")
    for label, timestamp in (("quote", quote_time), ("bid_ask", bid_ask_time)):
        if timestamp is None:
            errors.append(f"{label} timestamp missing")
        else:
            age = (current - timestamp).total_seconds()
            if age < -300 or age > max_age:
                errors.append(f"{label} is stale")
    for field in ("volume", "historical_vol"):
        if number(quote.get(field)) is None:
            errors.append(f"quote.{field} missing")

    daily_bars = int(history.get("daily_bars") or 0)
    weekly_bars = int(history.get("weekly_bars") or 0)
    if history.get("daily_period") != policy["history"]["daily_period"] or history.get("daily_step") != policy["history"]["daily_step"]:
        errors.append("daily history request contract mismatch")
    if history.get("weekly_period") != policy["history"]["weekly_period"] or history.get("weekly_step") != policy["history"]["weekly_step"]:
        errors.append("weekly history request contract mismatch")
    if daily_bars < int(policy["history"]["minimum_daily_bars_if_history_exists"]):
        errors.append("daily history is too short")
    if weekly_bars < int(policy["history"]["minimum_weekly_bars_if_history_exists"]):
        errors.append("weekly history is too short")

    if actionable:
        for field in ("summary", "balances", "positions", "allocation"):
            if not _nonempty_object(account.get(field)):
                errors.append(f"account.{field} missing")
        if str((account.get("allocation") or {}).get("allocation_type") or "") != policy["account"]["allocation_type"]:
            errors.append("account allocation must use ALL")
        account_time = parse_time(account.get("fetched_at")) if account.get("fetched_at") else None
        max_account_age = float(policy["account"]["max_age_hours"]) * 3600
        if account_time is None or not (-300 <= (current - account_time).total_seconds() <= max_account_age):
            errors.append("account context is stale")
    if payload.get("explicit_order_request") is not True and payload.get("order_instruction") is not None:
        errors.append("order instruction requires an explicit user request")

    quote_identity = {
        "ticker": ticker,
        "contract_id": str(contract.get("contract_id") or ""),
        "last": last,
        "bid": bid,
        "ask": ask,
        "quote_timestamp": quote.get("quote_timestamp"),
        "bid_ask_timestamp": quote.get("bid_ask_timestamp"),
    }
    return {
        "valid": not errors,
        "provider": "IBKR",
        "ticker": ticker,
        "contract_id": quote_identity["contract_id"],
        "exchange": contract.get("exchange"),
        "security_type": contract.get("security_type"),
        "quote_identity": quote_identity,
        "quote_sha256": semantic_hash(quote_identity),
        "spread_pct": round(spread, 6) if spread is not None else None,
        "daily_bars": daily_bars,
        "weekly_bars": weekly_bars,
        "account_bundle_present": bool(account) if actionable else None,
        "account_context_used_for_scoring": False,
        "order_instruction_allowed": payload.get("explicit_order_request") is True and not errors,
        "errors": sorted(set(errors)),
        "validated_at": current.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def _trade_plan_errors(plan: Any, status: str, validation_time: dt.datetime) -> list[str]:
    if not isinstance(plan, Mapping):
        return ["trade_plan missing"]
    errors: list[str] = []
    entry = plan.get("entry") if isinstance(plan.get("entry"), Mapping) else {}
    price = number(entry.get("price"))
    if price is None or price <= 0:
        errors.append("trade_plan.entry.price missing")
    quote_time = parse_time(entry.get("quote_as_of")) if entry.get("quote_as_of") else None
    if quote_time is None or not (-300 <= (validation_time - quote_time).total_seconds() <= 180):
        errors.append("trade_plan entry quote is stale")
    target5, target7 = number(plan.get("target_5_price")), number(plan.get("target_7_price"))
    if price is not None and target5 is not None:
        pct = 100 * (target5 / price - 1)
        if not 4.5 <= pct <= 5.5:
            errors.append("target_5_price is not approximately 5 percent")
    else:
        errors.append("target_5_price missing")
    if price is not None and target7 is not None:
        pct = 100 * (target7 / price - 1)
        if not 6.5 <= pct <= 7.5:
            errors.append("target_7_price is not approximately 7 percent")
    else:
        errors.append("target_7_price missing")
    horizon = plan.get("horizon_trading_days") if isinstance(plan.get("horizon_trading_days"), Mapping) else {}
    minimum, maximum = number(horizon.get("min")), number(horizon.get("max"))
    if minimum is None or maximum is None or not 10 <= minimum <= maximum <= 50:
        errors.append("trade_plan horizon must be 10-50 trading days")
    for field in (
        "fundamental_invalidation", "technical_invalidation", "time_invalidation",
        "gap_down_plan", "earnings_plan", "runaway_price_plan",
    ):
        if not str(plan.get(field) or "").strip():
            errors.append(f"trade_plan.{field} missing")
    recheck = parse_time(plan.get("recheck_date")) if plan.get("recheck_date") else None
    if recheck is None or recheck.date() < validation_time.date():
        errors.append("trade_plan.recheck_date missing or past")
    if plan.get("averaging_down_allowed") is None:
        errors.append("trade_plan.averaging_down_allowed missing")
    elif plan.get("averaging_down_allowed") is True and not str(plan.get("predefined_averaging_plan") or "").strip():
        errors.append("trade_plan predefined averaging plan missing")
    if status == "prepare_limit_order" and plan.get("execution_guard") != "revalidate_before_execution":
        errors.append("prepare_limit_order requires revalidate_before_execution")
    return errors


def validate_final(payload: Mapping[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    validate_contract("final-decision", payload)
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    status = str(payload.get("final_status") or "")
    l5 = payload.get("l5") if isinstance(payload.get("l5"), Mapping) else {}
    l5_input = payload.get("l5_input") if isinstance(payload.get("l5_input"), Mapping) else {}
    execution_payload = payload.get("execution_context") if isinstance(payload.get("execution_context"), Mapping) else {}
    execution = validate_execution(execution_payload, now=current) if execution_payload else {"valid": False, "errors": ["execution_context missing"]}
    errors: list[str] = []
    try:
        from research import evaluate_l5
        recomputed_l5 = evaluate_l5(l5_input)
    except (ValueError, TypeError) as exc:
        recomputed_l5 = {}
        errors.append(f"canonical L5 recomputation failed: {exc}")
    if recomputed_l5 and semantic_hash(recomputed_l5) != semantic_hash(l5):
        errors.append("L5 result does not match canonical evaluator output")
    if status not in FINAL_STATUSES:
        errors.append("final_status invalid")
    ticker = str(payload.get("ticker") or "").upper()
    contract_id = str(payload.get("contract_id") or "")
    if ticker != str(l5.get("ticker") or "").upper() or contract_id != str(l5.get("contract_id") or ""):
        errors.append("L5 identity mismatch")
    if execution_payload and (ticker != execution.get("ticker") or contract_id != execution.get("contract_id")):
        errors.append("IBKR identity mismatch")
    l3 = l5.get("l3") if isinstance(l5.get("l3"), Mapping) else {}
    l4 = l5.get("l4") if isinstance(l5.get("l4"), Mapping) else {}
    security_types = {
        str(value).lower() for value in (
            l5.get("security_type"), l3.get("security_type"), l4.get("security_type"), execution.get("security_type")
        ) if value not in (None, "")
    }
    if len(security_types) != 1 or not security_types:
        errors.append("security_type mismatch across analytical and IBKR identity")
    elif "etf" in security_types:
        from eligibility import validate as validate_instrument
        etf_check = validate_instrument({
            "ticker": ticker, "contract_id": contract_id, "security_type": "etf",
            "instrument_status": "verified", "structure": {},
        })
        if not etf_check["valid"]:
            errors.append("ETF exact IBKR identity is not allowlisted")

    plan_errors: list[str] = []
    if status in {"open_now", "prepare_limit_order"}:
        if execution_payload.get("requested_status") != status:
            errors.append("execution requested_status must equal final actionable status")
        plan_errors = _trade_plan_errors(payload.get("trade_plan"), status, current)
        expected_quote = {
            "ticker": ticker,
            "contract_id": contract_id,
            "last": number((l5.get("quote") or {}).get("last")),
            "bid": number((l5.get("quote") or {}).get("bid")),
            "ask": number((l5.get("quote") or {}).get("ask")),
            "quote_timestamp": (l5.get("quote") or {}).get("as_of"),
            "bid_ask_timestamp": (l5.get("quote") or {}).get("bid_ask_as_of", (l5.get("quote") or {}).get("as_of")),
        }
        if execution.get("quote_sha256") != semantic_hash(expected_quote):
            errors.append("L5 and IBKR must use the same quote snapshot")
        plan = payload.get("trade_plan") if isinstance(payload.get("trade_plan"), Mapping) else {}
        entry = plan.get("entry") if isinstance(plan.get("entry"), Mapping) else {}
        entry_price = number(entry.get("price"))
        l5_bid, l5_ask = number(expected_quote["bid"]), number(expected_quote["ask"])
        if entry.get("quote_as_of") != expected_quote["quote_timestamp"]:
            errors.append("trade plan and L5 must use the same quote timestamp")
        if status == "open_now" and (entry_price is None or l5_bid is None or l5_ask is None or not l5_bid <= entry_price <= l5_ask):
            errors.append("open_now entry price is outside the executable bid/ask")
    hard_vetoes = list(l5.get("hard_vetoes") or [])
    canonical_l5_required = bool(recomputed_l5) and (
        l5.get("depth") == "L5"
        and isinstance(l5.get("evidence"), list) and bool(l5.get("evidence"))
        and isinstance(l5.get("evidence_links"), Mapping) and bool(l5.get("evidence_links"))
        and isinstance(l5.get("opportunity_components"), Mapping)
        and isinstance(l5.get("intrinsic_risk_components"), Mapping)
        and l5.get("technical_stabilization") == "confirmed"
        and l5.get("room_to_5pct") is True
        and l5.get("room_to_7pct") is True
        and l5.get("historical_recovery_sample_status") == "adequate"
    )
    analytics_ready = canonical_l5_required and l5.get("critical_complete") is True and l5.get("evidence_validated") is True and l5.get("opportunity_class") != "E" and number(l5.get("intrinsic_risk_coverage_pct")) is not None and float(l5["intrinsic_risk_coverage_pct"]) >= 50
    if status == "open_now":
        if l5.get("entry_readiness") != "open_now_candidate":
            errors.append("L5 is not an open_now candidate")
        if not analytics_ready:
            errors.append("analytical L5 is incomplete")
        if not execution.get("valid"):
            errors.append("IBKR execution context is invalid")
        if hard_vetoes:
            errors.append("hard veto blocks entry")
    elif status == "prepare_limit_order":
        if l5.get("entry_readiness") not in {"prepare_limit_order", "open_now_candidate"}:
            errors.append("L5 is not eligible for limit preparation")
        if not analytics_ready or not execution.get("valid") or hard_vetoes:
            errors.append("limit preparation prerequisites failed")
    elif status == "wait":
        if hard_vetoes:
            errors.append("structural hard veto requires do_not_enter or do_not_consider")
    elif status == "do_not_consider":
        instrument_vetoes = {"prohibited_instrument", "instrument_ineligible", "fund_viability_or_structure_failure", "adr_reporting_or_listing_failure"}
        if not instrument_vetoes.intersection(hard_vetoes):
            errors.append("do_not_consider requires a structural instrument veto")
    # do_not_enter is intentionally conservative and may be valid with either
    # hard vetoes or insufficient actionable data.
    errors.extend(plan_errors)
    valid = status in FINAL_STATUSES and not errors
    return {
        "valid": valid,
        "validator_result": "PASS" if valid else "FAIL",
        "final_status": status,
        "entry_allowed": valid and status == "open_now",
        "order_preparation_allowed": valid and status == "prepare_limit_order",
        "analytics_valid": analytics_ready,
        "execution_valid": bool(execution.get("valid")),
        "execution_receipt": execution,
        "account_context_used_for_scoring": False,
        "hard_vetoes": hard_vetoes,
        "trade_plan_errors": plan_errors,
        "errors": sorted(set(errors)),
        "validation_time": current.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
