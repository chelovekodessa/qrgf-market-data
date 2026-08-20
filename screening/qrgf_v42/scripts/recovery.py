#!/usr/bin/env python3
"""Measure independent historical recovery episodes without look-ahead."""

from __future__ import annotations

import statistics
from typing import Any

from technical import normalize_bars


def _drawdown(bars: list[dict[str, Any]], index: int, lookback: int) -> float:
    high = max(row["high"] for row in bars[max(0, index - lookback + 1):index + 1])
    return 100 * (bars[index]["close"] / high - 1)


def _first_high(future: list[dict[str, Any]], price: float) -> int | None:
    return next((index for index, row in enumerate(future, 1) if row["high"] >= price), None)


def analyze(payload: Any, *, target_drawdown_pct: float, tolerance_pct: float = 3, horizon: int = 50, min_spacing: int = 20, minimum_sample: int = 5, lookback: int = 252, serious_drawdown_pct: float = -10) -> dict[str, Any]:
    bars = normalize_bars(payload)
    if horizon < 10 or horizon > 50:
        raise ValueError("horizon must remain within the 10-50 trading-day strategy")
    if len(bars) < lookback + horizon:
        return {"episode_count": 0, "eligible_episode_count": 0, "sample_status": "limited", "hit_5pct_rate_pct": None, "hit_7pct_rate_pct": None, "episodes": []}
    target = -abs(float(target_drawdown_pct))
    lower, upper = target - abs(tolerance_pct), target + abs(tolerance_pct)
    last_index = len(bars) - horizon - 1
    episodes: list[dict[str, Any]] = []
    last_selected = -10**9
    in_regime = False
    for index in range(lookback - 1, last_index + 1):
        drawdown = _drawdown(bars, index, lookback)
        if drawdown > min(-2, upper / 3):
            in_regime = False
        if not lower <= drawdown <= upper or in_regime:
            continue
        if index - last_selected < max(1, min_spacing):
            in_regime = True
            continue
        entry = bars[index]["close"]
        future = bars[index + 1:index + 1 + horizon]
        if len(future) != horizon:
            continue
        day5, day7 = _first_high(future, entry * 1.05), _first_high(future, entry * 1.07)
        serious_price = entry * (1 + serious_drawdown_pct / 100)
        serious_day = next((offset for offset, row in enumerate(future, 1) if row["low"] <= serious_price), None)
        if day7 is None and serious_day is None:
            order = "neither"
        elif day7 is None or (serious_day is not None and serious_day < day7):
            order = "serious_drawdown_first"
        elif serious_day is None or day7 < serious_day:
            order = "target_hit_first"
        else:
            order = "intraday_order_unknown"
        episodes.append({
            "date": bars[index]["date"], "entry_index": index, "entry_price": entry,
            "drawdown_pct": round(drawdown, 6), "future_bars": horizon,
            "hit_5pct": day5 is not None, "days_to_5pct": day5,
            "hit_7pct": day7 is not None, "days_to_7pct": day7,
            "path_order_7pct": order,
            "maximum_adverse_excursion_horizon_pct": round(100 * (min(row["low"] for row in future) / entry - 1), 6),
            "maximum_favorable_excursion_horizon_pct": round(100 * (max(row["high"] for row in future) / entry - 1), 6),
        })
        in_regime, last_selected = True, index
    sample = len(episodes)
    hit5, hit7 = [row for row in episodes if row["hit_5pct"]], [row for row in episodes if row["hit_7pct"]]
    median = lambda values: round(statistics.median(values), 4) if values else None
    return {
        "episode_count": sample,
        "eligible_episode_count": sample,
        "sample_status": "adequate" if sample >= minimum_sample else "limited",
        "hit_5pct_rate_pct": round(100 * len(hit5) / sample, 4) if sample else None,
        "hit_7pct_rate_pct": round(100 * len(hit7) / sample, 4) if sample else None,
        "median_days_to_5pct": median([row["days_to_5pct"] for row in hit5]),
        "median_days_to_7pct": median([row["days_to_7pct"] for row in hit7]),
        "target_first_7pct_rate_pct": round(100 * sum(row["path_order_7pct"] == "target_hit_first" for row in episodes) / sample, 4) if sample else None,
        "intraday_order_unknown_count": sum(row["path_order_7pct"] == "intraday_order_unknown" for row in episodes),
        "episodes": episodes,
    }
