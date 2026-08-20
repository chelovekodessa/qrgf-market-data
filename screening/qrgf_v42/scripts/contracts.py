#!/usr/bin/env python3
"""Apply bundled JSON Schemas at every production boundary."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"


@lru_cache(maxsize=None)
def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def validate(name: str, payload: Any) -> None:
    schema = _schema(name)
    try:
        from jsonschema import Draft202012Validator
    except ModuleNotFoundError:
        errors: list[str] = []
        _validate_basic(schema, payload, "$", errors)
        if errors:
            raise ValueError("; ".join(errors))
    else:
        errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda error: list(error.path))
        if errors:
            rendered = [f"{'/'.join(str(part) for part in error.path) or '$'}: {error.message}" for error in errors]
            raise ValueError("; ".join(rendered))


def _validate_basic(schema: Mapping[str, Any], value: Any, path: str, errors: list[str]) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if any(_matches_type(value, item) for item in expected):
            pass
        else:
            errors.append(f"{path}: expected one of {expected}")
            return
    elif expected and not _matches_type(value, expected):
        errors.append(f"{path}: expected {expected}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is outside enum")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: value does not equal required constant")
    if isinstance(value, Mapping):
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{path}: missing required property {name}")
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        for name, child in value.items():
            if name in properties:
                _validate_basic(properties[name], child, f"{path}.{name}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {name}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: too few items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: too many items")
        child_schema = schema.get("items")
        if isinstance(child_schema, Mapping):
            for index, child in enumerate(value):
                _validate_basic(child_schema, child, f"{path}[{index}]", errors)
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{path}: string is too short")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path}: string is too long")


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)
