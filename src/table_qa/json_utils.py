"""Strict JSON decoding helpers for model generated payloads."""

from __future__ import annotations

from decimal import Decimal
import json
from typing import Any, Callable


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON numeric constant: {value}")


def strict_json_loads(text: str, *, parse_float: Callable[[str], Any] = Decimal) -> Any:
    """Decode JSON while rejecting duplicate keys, NaN, and Infinity."""
    return json.loads(text, object_pairs_hook=_unique_object, parse_float=parse_float,
                      parse_constant=_reject_constant)
