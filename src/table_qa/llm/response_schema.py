"""Provider-compatible response schemas for the app's structured outputs."""

from __future__ import annotations

from typing import Any


_DIRECT_ENVELOPE = {
    "type": "object",
    "properties": {
        "answer": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["answer"],
    "additionalProperties": False,
}


def _provider_compatible(schema: Any, root: dict[str, Any]) -> Any:
    """Inline local refs while retaining operator-specific argument schemas."""
    if isinstance(schema, list):
        return [_provider_compatible(item, root) for item in schema]
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.rsplit("/", 1)[-1]
        definition = root.get("$defs", {}).get(name)
        if not isinstance(definition, dict):
            raise ValueError(f"Unknown local schema reference: {ref}")
        return _provider_compatible(definition, root)

    converted: dict[str, Any] = {}
    for key, value in schema.items():
        if key in {"$schema", "$id", "$defs", "pattern", "allOf"}:
            continue
        if key == "const":
            converted["enum"] = [value]
        elif key == "oneOf":
            converted["anyOf"] = _provider_compatible(value, root)
        else:
            converted[key] = _provider_compatible(value, root)
    return converted


def provider_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a provider-compatible schema for a known application response."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Application response schema has no properties")
    if {"version", "query_id", "table_id", "steps", "return"}.issubset(properties):
        return _provider_compatible(schema, schema)
    if set(properties) == {"answer"}:
        return _provider_compatible(_DIRECT_ENVELOPE, _DIRECT_ENVELOPE)
    raise ValueError("No provider-compatible response envelope is defined for this schema")
