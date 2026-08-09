#!/usr/bin/env python3
"""Shared deterministic helpers for Quality Recovery Gem Finder.

The module deliberately uses only the Python standard library so the Skill can
run in restricted execution environments. Network access is handled outside of
this module; every imported fact must carry source and timestamp metadata.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping

MISSING_TEXT = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "nan",
    "not_available",
    "not available",
    "-",
    "--",
}

TRUE_TEXT = {"true", "yes", "y", "1", "positive", "profitable", "pass", "checked"}
FALSE_TEXT = {"false", "no", "n", "0", "negative", "unprofitable", "fail"}


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def strict_float(
    value: Any,
    *,
    field: str = "value",
    allow_none: bool = True,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Parse a finite number without treating booleans as numbers."""
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


CANONICAL_CSV_JSON_FIELDS = {
    "history_evidence", "sources", "source_conflicts", "component_scores",
    "risk_component_scores", "opportunity_flags", "risk_flags", "hard_vetoes",
    "checks_failed", "checks_missing", "critical_missing", "evidence",
    "component_evidence_ids", "decision", "decision_validation",
}
CANONICAL_CSV_BOOL_FIELDS = {
    "profitable", "fcf_positive", "quality_seed", "rankable",
    "limited_history_recheck", "decision_eligible", "research_eligible",
    "evidence_validated", "fundamental_eligible", "critical_complete", "final_status_valid", "stale",
    "ruleset_migration_required_recheck", "selected_for_next_stage",
}
CANONICAL_CSV_NUMBER_FIELDS = {
    "price", "current_price", "market_cap", "avg_dollar_volume",
    "return_1m", "return_3m", "return_6m", "return_12m", "drawdown_52w",
    "historical_volatility", "quality_prior_score", "trading_history_days",
    "l1_score", "research_priority_score", "research_priority_coverage_pct",
    "opportunity_score", "opportunity_coverage_pct", "risk_score",
    "risk_coverage_pct", "risk_upper_bound", "l2_opportunity_score",
    "l2_risk_score", "l3_score", "l3_coverage_pct", "l4_score",
    "l4_coverage_pct",
}


def parse_canonical_csv_value(field: str, value: Any) -> Any:
    """Restore canonical types after a CSV export without guessing source units."""
    if value is None:
        return None
    raw = str(value).strip()
    if field in CANONICAL_CSV_JSON_FIELDS:
        if not raw:
            return [] if field in {
                "sources", "source_conflicts", "evidence", "opportunity_flags",
                "risk_flags", "hard_vetoes", "checks_failed", "checks_missing",
                "critical_missing",
            } else None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in canonical CSV field {field}") from exc
    if field in CANONICAL_CSV_BOOL_FIELDS:
        return strict_bool(raw, field=field, allow_none=True)
    if field in CANONICAL_CSV_NUMBER_FIELDS:
        return strict_float(raw, field=field, allow_none=True)
    return raw


def parse_canonical_csv_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: parse_canonical_csv_value(field, value) for field, value in row.items()}


def parse_percent(value: Any, *, unit: str, field: str) -> float | None:
    """Return percent points using an explicit source unit.

    ``unit`` must be ``percent`` or ``ratio``. Strings ending in ``%`` are
    always percent points. The function never guesses the unit from magnitude.
    """
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


def timestamp_sort_value(value: Any) -> float:
    """Chronological key supporting ISO dates and Unix seconds/milliseconds.

    Small numeric strings such as ``1, 2, 10`` are intentionally treated as
    numeric sequence values rather than Unix dates, preserving deterministic
    ordering in synthetic and vendor-indexed histories.
    """
    if is_missing(value):
        raise ValueError("timestamp/date is required")
    if isinstance(value, bool):
        raise ValueError("timestamp cannot be boolean")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("timestamp must be finite")
        if abs(number) >= 1e12:
            return number / 1000.0
        return number
    text = str(value).strip()
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        number = float(text)
        if abs(number) >= 1e12:
            return number / 1000.0
        return number
    parsed = parse_datetime(text)
    assert parsed is not None
    return parsed.timestamp()


def canonical_timestamp(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value.strip()):
        return value.strip()
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError("timestamp is required")
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def deep_merge(previous: Any, patch: Any) -> Any:
    """Recursively merge mappings while preserving omitted nested fields.

    Lists are replaced by default because their semantics are domain-specific;
    callers should explicitly union lists such as sources or evidence IDs.
    """
    if isinstance(previous, Mapping) and isinstance(patch, Mapping):
        result: dict[str, Any] = copy.deepcopy(dict(previous))
        for key, value in patch.items():
            if key in result:
                result[key] = deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result
    return copy.deepcopy(patch)


def unique_preserve_order(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(copy.deepcopy(value))
    return result


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def semantic_hash(payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return sha256_bytes(data)


def source_rank(meta: Mapping[str, Any], source_id: str) -> tuple[int, float, float, str]:
    """Deterministic source precedence, independent of input order."""
    priority = int(tolerant_float(meta.get("priority")) or 0)
    as_of = parse_datetime(meta.get("as_of"))
    retrieved = parse_datetime(meta.get("retrieved_at"))
    return (
        priority,
        as_of.timestamp() if as_of else float("-inf"),
        retrieved.timestamp() if retrieved else float("-inf"),
        source_id,
    )



def normalize_evidence_records(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize and validate evidence records against the trusted core contract."""
    if isinstance(raw, Mapping):
        rows = []
        for key, value in raw.items():
            if isinstance(value, Mapping):
                rows.append({"evidence_id": key, **dict(value)})
    elif isinstance(raw, list):
        rows = raw
    else:
        return [], ["evidence must be an array or object"]
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    valid_quality = {"verified", "usable", "conflict", "stale", "invalid", "missing"}
    for index, value in enumerate(rows):
        if not isinstance(value, Mapping):
            errors.append(f"evidence[{index}] must be an object")
            continue
        row = dict(value)
        ident = str(row.get("evidence_id") or row.get("id") or "").strip()
        if not ident:
            errors.append(f"evidence[{index}] missing evidence_id")
            continue
        if ident in seen:
            errors.append(f"duplicate evidence_id {ident}")
            continue
        seen.add(ident)
        field = str(row.get("field") or "").strip()
        if not field:
            errors.append(f"evidence {ident} missing field")
        if "value" not in row:
            errors.append(f"evidence {ident} missing value")
        source_value = row.get("source") or row.get("source_id")
        if isinstance(source_value, Mapping):
            source_id = str(source_value.get("source_id") or "").strip()
            source_type = str(source_value.get("source_type") or "other").strip().lower()
            source = dict(source_value)
        else:
            source_id = str(source_value or "").strip()
            source_type = str(row.get("source_type") or "other").strip().lower()
            source = {"source_id": source_id, "source_type": source_type}
        if not source_id:
            errors.append(f"evidence {ident} missing source")
        for date_field in ("as_of", "retrieved_at"):
            try:
                parsed = parse_datetime(row.get(date_field))
                if parsed is None:
                    raise ValueError("missing")
            except ValueError:
                errors.append(f"evidence {ident} invalid or missing {date_field}")
        quality = str(row.get("quality_status") or "").strip().lower()
        if quality not in valid_quality:
            errors.append(f"evidence {ident} invalid or missing quality_status")
        elif quality not in {"verified", "usable"}:
            errors.append(f"evidence {ident} is not usable: {quality}")
        normalized.append({
            **row,
            "evidence_id": ident,
            "field": field,
            "source": source,
            "quality_status": quality,
        })
    return normalized, errors



def evidence_ids_for_field(records: list[dict[str, Any]], field: str) -> list[str]:
    """Return usable evidence IDs that directly cover a canonical field.

    A record may cover an exact field (``fundamentals.revenue_growth_pct``) or
    a structured parent object (``fundamentals``) that actually contains the
    requested nested value. Generic unrelated records never count.
    """
    expected = str(field or "").strip()
    if not expected:
        return []
    parts = expected.split(".")
    result: list[str] = []
    for row in records:
        if str(row.get("quality_status") or "").lower() not in {"verified", "usable"}:
            continue
        observed = str(row.get("field") or "").strip()
        covered = observed == expected
        if not covered and len(parts) > 1 and observed == parts[0]:
            value: Any = row.get("value")
            covered = True
            for part in parts[1:]:
                if not isinstance(value, Mapping) or part not in value:
                    covered = False
                    break
                value = value.get(part)
        if covered:
            result.append(str(row.get("evidence_id")))
    return sorted(set(result))


def required_evidence_links(
    records: list[dict[str, Any]],
    fields: list[str] | tuple[str, ...] | set[str],
) -> tuple[dict[str, list[str]], list[str]]:
    links: dict[str, list[str]] = {}
    missing: list[str] = []
    for field in sorted({str(value) for value in fields if str(value).strip()}):
        ids = evidence_ids_for_field(records, field)
        links[field] = ids
        if not ids:
            missing.append(field)
    return links, missing

def validate_json_schema(instance: Any, schema_path: Path) -> list[str]:
    """Validate against a bundled JSON Schema without remote network lookup."""
    try:
        import jsonschema  # type: ignore
    except Exception:
        return ["jsonschema package unavailable"]
    schema = load_json(schema_path)
    store: dict[str, Any] = {}
    for sibling in schema_path.parent.glob("*.json"):
        try:
            child = load_json(sibling)
        except Exception:
            continue
        if isinstance(child, Mapping):
            child_id = str(child.get("$id") or "").strip()
            if child_id:
                store[child_id] = child
            store[sibling.resolve().as_uri()] = child
            # Relative references under the current schema's HTTPS base.
            base_id = str(schema.get("$id") or "")
            if base_id and "/" in base_id:
                store[base_id.rsplit("/", 1)[0] + "/" + sibling.name] = child
    resolver = jsonschema.RefResolver.from_schema(schema, store=store)
    validator = jsonschema.Draft202012Validator(schema, resolver=resolver)
    try:
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    except Exception as exc:
        return [f"schema resolution failed: {type(exc).__name__}: {exc}"]
    result: list[str] = []
    for error in errors:
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        result.append(f"{path}: {error.message}")
    return result
