#!/usr/bin/env python3
"""Shared deterministic helpers. Standard-library only."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config" / "policy.json"
CONNECTORS_PATH = ROOT / "config" / "connectors.json"


def read_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path: Path | str, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_hash(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def truth(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def mean(values: Iterable[float | None]) -> float | None:
    known = [float(value) for value in values if value is not None]
    return sum(known) / len(known) if known else None


def weighted_score(values: Mapping[str, Any], weights: Mapping[str, Any]) -> tuple[float | None, float]:
    known_weight = 0.0
    total_weight = sum(float(weight) for weight in weights.values())
    numerator = 0.0
    for name, raw_weight in weights.items():
        value = number(values.get(name))
        if value is None:
            continue
        weight = float(raw_weight)
        numerator += clamp(value) * weight
        known_weight += weight
    coverage = 100.0 * known_weight / total_weight if total_weight else 0.0
    return (round(numerator / known_weight, 2) if known_weight else None, round(coverage, 2))


def piecewise(value: Any, points: list[tuple[float, float]]) -> float | None:
    numeric = number(value)
    if numeric is None:
        return None
    ordered = sorted(points)
    if numeric <= ordered[0][0]:
        return float(ordered[0][1])
    if numeric >= ordered[-1][0]:
        return float(ordered[-1][1])
    for (x0, y0), (x1, y1) in zip(ordered, ordered[1:]):
        if x0 <= numeric <= x1:
            return clamp(y0 + (numeric - x0) * (y1 - y0) / (x1 - x0))
    return None


def get_path(value: Mapping[str, Any], dotted: str) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


@lru_cache(maxsize=1)
def load_policy() -> dict[str, Any]:
    return read_json(POLICY_PATH)


@lru_cache(maxsize=1)
def load_connectors() -> dict[str, Any]:
    return read_json(CONNECTORS_PATH)


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
