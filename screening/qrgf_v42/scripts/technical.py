#!/usr/bin/env python3
"""Validate Interactive Brokers history and compute technical features locally."""

from __future__ import annotations

import datetime as dt
import math
import statistics
from typing import Any, Mapping

from common import mean, number


def normalize_bars(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        for key in ("bars", "data", "results", "history"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise ValueError("history must contain an array of bars")
    bars: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            raise ValueError(f"bar {index} must be an object")
        date_text = str(raw.get("date") or raw.get("timestamp") or raw.get("time") or "")
        if not date_text:
            raise ValueError(f"bar {index} date missing")
        try:
            date = dt.date.fromisoformat(date_text[:10])
        except ValueError as exc:
            raise ValueError(f"bar {index} date invalid") from exc
        if date.isoformat() in seen:
            raise ValueError(f"duplicate bar date: {date}")
        seen.add(date.isoformat())
        close = number(raw.get("close"))
        open_price = number(raw.get("open", close))
        high, low = number(raw.get("high")), number(raw.get("low"))
        volume = number(raw.get("volume"))
        if None in {open_price, high, low, close} or min(open_price, high, low, close) <= 0:
            raise ValueError(f"bar {index} has invalid OHLC")
        if high < low or not low <= open_price <= high or not low <= close <= high:
            raise ValueError(f"bar {index} has inconsistent OHLC")
        if volume is not None and volume < 0:
            raise ValueError(f"bar {index} volume is negative")
        bars.append({"date": date.isoformat(), "open": open_price, "high": high, "low": low, "close": close, "volume": volume})
    bars.sort(key=lambda row: row["date"])
    return bars


def _sma(values: list[float], period: int) -> float | None:
    return sum(values[-period:]) / period if len(values) >= period else None


def _period_return(values: list[float], sessions: int) -> float | None:
    return 100 * (values[-1] / values[-1 - sessions] - 1) if len(values) > sessions else None


def _atr(bars: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    true_ranges = []
    for index in range(len(bars) - period, len(bars)):
        row, previous = bars[index], bars[index - 1]["close"]
        true_ranges.append(max(row["high"] - row["low"], abs(row["high"] - previous), abs(row["low"] - previous)))
    return mean(true_ranges)


def _volatility(closes: list[float], period: int = 30) -> float | None:
    if len(closes) < period + 1:
        return None
    returns = [math.log(closes[index] / closes[index - 1]) for index in range(len(closes) - period, len(closes))]
    return statistics.stdev(returns) * math.sqrt(252) * 100 if len(returns) > 1 else None


def _swing_levels(bars: list[dict[str, Any]], lookback: int = 126, window: int = 3) -> tuple[list[float], list[float]]:
    segment = bars[-lookback:]
    supports, resistances = [], []
    for index in range(window, len(segment) - window):
        neighbors = segment[index - window:index] + segment[index + 1:index + window + 1]
        if segment[index]["low"] <= min(row["low"] for row in neighbors):
            supports.append(segment[index]["low"])
        if segment[index]["high"] >= max(row["high"] for row in neighbors):
            resistances.append(segment[index]["high"])
    return supports, resistances


def _nearest(levels: list[float], current: float, above: bool) -> list[float]:
    candidates = sorted((value for value in levels if (value > current if above else value < current)), reverse=not above)
    result: list[float] = []
    for value in candidates:
        if not result or abs(value / result[-1] - 1) > 0.015:
            result.append(value)
        if len(result) == 3:
            break
    return result


def _stabilization(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if len(bars) < 20:
        return {"coverage": "insufficient", "signals": {}, "signals_passed": 0, "signals_total": 0, "stabilization_status": "not_evaluated"}
    recent, prior = bars[-10:], bars[-20:-10]
    recent_ranges = [(row["high"] - row["low"]) / row["close"] for row in recent]
    prior_ranges = [(row["high"] - row["low"]) / row["close"] for row in prior]
    signals = {
        "range_contraction": mean(recent_ranges) <= mean(prior_ranges),
        "no_new_low": min(row["low"] for row in recent) >= min(row["low"] for row in prior),
        "higher_recent_close": recent[-1]["close"] >= recent[0]["close"],
        "positive_day_balance": sum(recent[i]["close"] > recent[i - 1]["close"] for i in range(1, len(recent))) >= 4,
        "holds_five_day_midpoint": recent[-1]["close"] >= (max(row["high"] for row in recent[-5:]) + min(row["low"] for row in recent[-5:])) / 2,
    }
    passed = sum(signals.values())
    return {"coverage": "full", "signals": signals, "signals_passed": passed, "signals_total": 5, "stabilization_status": "confirmed" if passed >= 4 else "emerging" if passed >= 3 else "absent"}


def _gaps(bars: list[dict[str, Any]]) -> dict[str, Any]:
    segment = bars[-64:]
    gaps = [100 * (segment[index]["open"] / segment[index - 1]["close"] - 1) for index in range(1, len(segment))]
    return {
        "sample": len(gaps),
        "mean_abs_gap_pct": round(mean([abs(value) for value in gaps]), 4) if gaps else None,
        "max_down_gap_pct": round(min(gaps), 4) if gaps else None,
        "large_gap_frequency_pct": round(100 * sum(abs(value) >= 3 for value in gaps) / len(gaps), 4) if gaps else None,
    }


def compute(daily_payload: Any, weekly_payload: Any) -> dict[str, Any]:
    daily, weekly = normalize_bars(daily_payload), normalize_bars(weekly_payload)
    if not daily or not weekly:
        raise ValueError("daily and weekly history are required")
    if daily[-1]["date"] < weekly[-1]["date"]:
        raise ValueError("weekly history extends past daily history")
    closes, weekly_closes = [row["close"] for row in daily], [row["close"] for row in weekly]
    current = closes[-1]
    high_52w, low_52w = max(row["high"] for row in daily[-252:]), min(row["low"] for row in daily[-252:])
    atr = _atr(daily)
    supports, resistances = _swing_levels(daily)
    nearest_support, nearest_resistance = _nearest(supports, current, False), _nearest(resistances, current, True)
    volume_rows = [row for row in daily[-63:] if row["volume"] is not None]
    return {
        "data_sufficiency": {
            "daily_bars": len(daily),
            "daily_history_status": "full" if len(daily) >= 253 else "limited_but_usable" if len(daily) >= 126 else "insufficient",
            "weekly_bars": len(weekly),
            "weekly_history_status": "full" if len(weekly) >= 156 else "limited_but_usable" if len(weekly) >= 52 else "insufficient",
            "volume_coverage_pct_63d": round(100 * len(volume_rows) / min(63, len(daily)), 4),
        },
        "current_price": current,
        "last_date": daily[-1]["date"],
        "return_1m_pct": _period_return(closes, 21),
        "return_3m_pct": _period_return(closes, 63),
        "return_6m_pct": _period_return(closes, 126),
        "return_12m_pct": _period_return(closes, 252),
        "drawdown_52w_pct": round(100 * (current / high_52w - 1), 4),
        "high_52w": high_52w,
        "low_52w": low_52w,
        "sma_20": _sma(closes, 20), "sma_50": _sma(closes, 50), "sma_100": _sma(closes, 100), "sma_200": _sma(closes, 200),
        "weekly_sma_10": _sma(weekly_closes, 10), "weekly_sma_40": _sma(weekly_closes, 40),
        "atr_14": atr,
        "atr_14_pct": round(100 * atr / current, 4) if atr is not None else None,
        "historical_volatility_30d_pct": _volatility(closes),
        "average_volume_63d": mean([row["volume"] for row in volume_rows]),
        "average_dollar_volume_63d": mean([row["close"] * row["volume"] for row in volume_rows]),
        "support_levels": nearest_support,
        "resistance_levels": nearest_resistance,
        "nearest_support": nearest_support[0] if nearest_support else None,
        "nearest_resistance": nearest_resistance[0] if nearest_resistance else None,
        "distance_to_nearest_resistance_pct": round(100 * (nearest_resistance[0] / current - 1), 4) if nearest_resistance else None,
        "resistance_evaluation_status": "not_evaluated" if len(daily) < 63 else "confirmed_level" if nearest_resistance else "no_resistance_found_in_126d",
        "support_evaluation_status": "not_evaluated" if len(daily) < 63 else "confirmed_level" if nearest_support else "no_support_found_in_126d",
        "gap_statistics": _gaps(daily),
        "stabilization": _stabilization(daily),
    }
