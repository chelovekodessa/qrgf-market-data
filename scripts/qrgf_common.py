from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

MISSING_TEXT = {"", "n/a", "na", "none", "null", "nan", "not_available", "not available", "-", "--"}
TRUE_TEXT = {"true", "yes", "y", "1", "positive", "profitable", "pass", "checked"}
FALSE_TEXT = {"false", "no", "n", "0", "negative", "unprofitable", "fail"}


def normalize_ticker(value: Any) -> str:
    return str(value or "").strip().upper()


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip().lower() in MISSING_TEXT:
        return True
    return False


def strict_float(value: Any, *, field: str = "value", allow_none: bool = True, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if is_missing(value):
        if allow_none:
            return None
        raise ValueError(f"{field} is required")
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric, not boolean")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        multiplier = 1.0
        if text and text[-1].upper() in {"K", "M", "B", "T"}:
            multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[text[-1].upper()]
            text = text[:-1]
        text = text.replace("$", "").strip()
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            number = float(text) * multiplier
        except ValueError as exc:
            raise ValueError(f"{field} is not a valid number: {value!r}") from exc
    if not math.isfinite(number):
        if allow_none:
            return None
        raise ValueError(f"{field} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return number


def tolerant_float(value: Any) -> float | None:
    try:
        return strict_float(value)
    except ValueError:
        return None


def strict_bool(value: Any, *, field: str = "value", allow_none: bool = True) -> bool | None:
    if is_missing(value):
        if allow_none:
            return None
        raise ValueError(f"{field} is required")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        raise ValueError(f"{field} numeric boolean must be 0 or 1")
    text = str(value).strip().lower()
    if text in TRUE_TEXT:
        return True
    if text in FALSE_TEXT:
        return False
    raise ValueError(f"{field} is not a valid boolean: {value!r}")


def tolerant_bool(value: Any) -> bool | None:
    try:
        return strict_bool(value)
    except ValueError:
        return None


def parse_percent(value: Any, *, unit: str, field: str) -> float | None:
    if is_missing(value):
        return None
    raw_text = str(value).strip() if isinstance(value, str) else ""
    explicit_percent = raw_text.endswith("%")
    number = tolerant_float(value)
    if number is None:
        return None
    normalized_unit = str(unit or "").strip().lower()
    if explicit_percent:
        return number
    if normalized_unit == "ratio":
        return number * 100.0
    if normalized_unit == "percent":
        return number
    raise ValueError(f"{field}: unsupported percent unit {unit!r}")


def parse_datetime(value: Any) -> dt.datetime | None:
    if is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        parsed = dt.datetime.combine(value, dt.time.min)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if abs(number) >= 1e12:
            number /= 1000.0
        parsed = dt.datetime.fromtimestamp(number, tz=dt.timezone.utc)
    else:
        text = str(value).strip()
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
            number = float(text)
            if abs(number) >= 1e12:
                number /= 1000.0
            parsed = dt.datetime.fromtimestamp(number, tz=dt.timezone.utc)
        else:
            candidate = text.replace("Z", "+00:00")
            try:
                parsed = dt.datetime.fromisoformat(candidate)
            except ValueError:
                parsed = None
                for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%m-%Y"):
                    try:
                        parsed = dt.datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue
                if parsed is None:
                    raise ValueError(f"Unsupported timestamp: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def source_rank(meta: Mapping[str, Any], source_id: str) -> tuple[int, float, float, str]:
    priority = int(tolerant_float(meta.get("priority")) or 0)
    as_of = parse_datetime(meta.get("as_of"))
    retrieved = parse_datetime(meta.get("retrieved_at"))
    return (priority, as_of.timestamp() if as_of else float("-inf"), retrieved.timestamp() if retrieved else float("-inf"), source_id)


CANONICAL_CSV_JSON_FIELDS = {
    "history_evidence", "sources", "source_conflicts", "component_scores",
    "risk_component_scores", "opportunity_flags", "risk_flags", "hard_vetoes",
    "checks_failed", "checks_missing", "critical_missing", "evidence",
    "component_evidence_ids", "decision", "decision_validation",
}
CANONICAL_CSV_BOOL_FIELDS = {
    "profitable", "fcf_positive", "quality_seed", "rankable",
    "limited_history_recheck", "decision_eligible", "research_eligible",
    "evidence_validated", "critical_complete", "final_status_valid", "stale",
    "ruleset_migration_required_recheck", "selected_for_next_stage",
}
CANONICAL_CSV_NUMBER_FIELDS = {
    "price", "current_price", "market_cap", "avg_dollar_volume",
    "return_1m", "return_3m", "return_6m", "return_12m", "drawdown_52w",
    "historical_volatility", "quality_prior_score", "trading_history_days",
    "l1_score", "research_priority_score", "research_priority_coverage_pct",
    "opportunity_score", "opportunity_coverage_pct", "risk_score",
    "risk_coverage_pct", "risk_upper_bound", "l2_opportunity_score",
    "l2_risk_score", "l3_score", "l3_coverage_pct", "l4_score", "l4_coverage_pct",
}


def parse_canonical_csv_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    raw = str(value).strip()
    if field in CANONICAL_CSV_JSON_FIELDS:
        if not raw:
            return [] if field in {"sources", "source_conflicts", "evidence", "opportunity_flags", "risk_flags", "hard_vetoes", "checks_failed", "checks_missing", "critical_missing"} else None
        return json.loads(raw)
    if field in CANONICAL_CSV_BOOL_FIELDS:
        return strict_bool(raw, field=field, allow_none=True)
    if field in CANONICAL_CSV_NUMBER_FIELDS:
        return strict_float(raw, field=field, allow_none=True)
    return raw


def parse_canonical_csv_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: parse_canonical_csv_value(field, value) for field, value in row.items()}
