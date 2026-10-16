"""Offline boundary checks for M0. No workbook reader or tool executor here."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import math
from pathlib import Path
import re
import sys
from typing import Any, get_args

from jsonschema import Draft202012Validator, FormatChecker, validators
from referencing import Registry, Resource

from .contracts import (
    AnswerSpec, DTO, Operator, PipelineError, Query, QueryContext, RuntimeLimits,
    TableContext, TableView, ValidatedPlan,
)


def _decimal_number(_checker: Any, instance: Any) -> bool:
    return type(instance) in (int, float) or isinstance(instance, Decimal)


def _decimal_integer(_checker: Any, instance: Any) -> bool:
    return (
        type(instance) is int
        or (type(instance) is float and math.isfinite(instance) and instance.is_integer())
        or (isinstance(instance, Decimal) and instance.is_finite() and instance == instance.to_integral_value())
    )


_DECIMAL_TYPE_CHECKER = Draft202012Validator.TYPE_CHECKER.redefine("number", _decimal_number).redefine("integer", _decimal_integer)
_DECIMAL_VALIDATOR = validators.extend(Draft202012Validator, type_checker=_DECIMAL_TYPE_CHECKER)

SCHEMA_NAMES = (
    "plan_v1", "table_context_v1", "answer_spec_v1", "query_context_v1",
    "query_v1", "table_view_v1", "runtime_limits_v1",
)


@lru_cache
def _schemas() -> dict[str, dict[str, Any]]:
    import json

    packaged = Path(__file__).resolve().parent / "schemas"
    source = Path(__file__).resolve().parents[2] / "schemas"
    installed = Path(sys.prefix) / "share" / "table_qa" / "schemas"
    root = (packaged if (packaged / "plan_v1.json").is_file() else
            source if (source / "plan_v1.json").is_file() else installed)
    return {name: json.loads((root / f"{name}.json").read_text(encoding="utf-8")) for name in SCHEMA_NAMES}


def load_schema(name: str) -> dict[str, Any]:
    """Return an independent schema copy; only known local names are accepted."""
    if name not in SCHEMA_NAMES:
        raise ValueError(f"Unknown schema: {name}")
    return deepcopy(_schemas()[name])


@lru_cache
def _registry() -> Registry:
    return Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in _schemas().values()
    )


def _fail(code: str, stage: str, message: str, **details: Any) -> None:
    raise PipelineError(code=code, stage=stage, message=message, details=details)


def _finite(value: Any) -> bool:
    if isinstance(value, Decimal):
        return value.is_finite()
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, list):
        return all(_finite(v) for v in value)
    return True


def validate_schema(name: str, data: Any, *, code: str, stage: str) -> None:
    """Validate with local refs only; never download a schema over the network."""
    if not _finite(data):
        _fail(code, stage, "Non-finite JSON number")
    validator = _DECIMAL_VALIDATOR(load_schema(name), registry=_registry(), format_checker=FormatChecker())
    error = next(validator.iter_errors(data), None)
    if error is not None:
        _fail(code, stage, error.message, path=list(error.absolute_path), schema=name)


def _wire(data: DTO | dict[str, Any]) -> dict[str, Any]:
    return data.to_dict() if isinstance(data, DTO) else data


def cell_position(cell: str) -> tuple[int, int]:
    """Return (row, column), enforcing Excel's XFD / 1048576 bounds."""
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", cell)
    if match is None:
        raise ValueError(f"Invalid A1 coordinate: {cell}")
    col = 0
    for char in match[1]:
        col = col * 26 + ord(char) - ord("A") + 1
    row = int(match[2])
    if row > 1048576 or col > 16384:
        raise ValueError(f"Coordinate outside Excel limits: {cell}")
    return row, col


def _bounds(value: str) -> tuple[int, int, int, int]:
    start, end = value.split(":")
    r1, c1 = cell_position(start)
    r2, c2 = cell_position(end)
    if r1 > r2 or c1 > c2:
        raise ValueError(f"Reversed range: {value}")
    return r1, c1, r2, c2


def _inside(cell: str, bounds: tuple[int, int, int, int]) -> bool:
    row, col = cell_position(cell)
    r1, c1, r2, c2 = bounds
    return r1 <= row <= r2 and c1 <= col <= c2


def validate_table_context(data: TableContext | dict[str, Any]) -> TableContext:
    wire = _wire(data)
    validate_schema("table_context_v1", wire, code="INVALID_TABLE_CONTEXT", stage="load")
    table = TableContext.from_dict(wire)
    names: set[str] = set()
    modes: set[str] = set()
    try:
        for sheet in table.sheets:
            if sheet.name in names:
                raise ValueError(f"Duplicate sheet {sheet.name}")
            names.add(sheet.name)
            bounds = _bounds(sheet.dimensions)
            modes.add("raw" if sheet.raw_rows is not None else "parsed")
            if sheet.raw_rows is not None:
                indices = [row.row_index for row in sheet.raw_rows]
                if indices != sorted(set(indices)):
                    raise ValueError("Raw rows must be unique and ordered")
                for row in sheet.raw_rows:
                    if not bounds[0] <= row.row_index <= bounds[2]:
                        raise ValueError("Row outside sheet dimensions")
                    if any(c.row_index != row.row_index for c in row.cells):
                        raise ValueError("Cell row does not match its RawRow")
                cells = [cell for row in sheet.raw_rows for cell in row.cells]
            else:
                cells = sheet.parsed_cells or []
            positions = [cell_position(c.cell) for c in cells]
            if positions != sorted(set(positions)):
                raise ValueError("Cells must be unique and in row/column order")
            merged = [_bounds(value) for value in sheet.merged_ranges]
            for value in sheet.merged_ranges:
                start, end = value.split(":")
                if not _inside(start, bounds) or not _inside(end, bounds):
                    raise ValueError("Merged range outside sheet dimensions")
            for c in cells:
                if c.sheet != sheet.name or cell_position(c.cell) != (c.row_index, c.column_index) or not _inside(c.cell, bounds):
                    raise ValueError(f"Inconsistent cell provenance: {c.cell}")
                if c.merged_anchor is not None:
                    anchor = cell_position(c.merged_anchor)
                    if not any(anchor == (b[0], b[1]) and _inside(c.cell, b) for b in merged):
                        raise ValueError("Merged anchor does not match a containing merged range")
            if any(not bounds[0] <= row <= bounds[2] for row in sheet.header_rows):
                raise ValueError("Header outside sheet dimensions")
            for section in sheet.sections:
                if not bounds[0] <= section.row_start <= section.row_end <= bounds[2] or not _inside(section.label_cell, bounds):
                    raise ValueError("Section outside sheet dimensions or reversed")
        if len(modes) != 1:
            raise ValueError("All sheets must use the same input mode")
    except ValueError as exc:
        _fail("INVALID_TABLE_CONTEXT", "load", str(exc))
    return table


def validate_answer_spec(data: AnswerSpec | dict[str, Any]) -> AnswerSpec:
    wire = _wire(data)
    validate_schema("answer_spec_v1", wire, code="ANSWER_SPEC_UNSUPPORTED", stage="format")
    return AnswerSpec.from_dict(wire)


def _check_meta(text: str, meta: dict[str, Any]) -> None:
    if sha256(text.encode("utf-8")).hexdigest() != meta["table_view_hash"]:
        raise ValueError("table_view_hash does not match the full view text")
    if set(meta["sheets_included"]) != set(meta["included_ranges"]):
        raise ValueError("Included sheet names/ranges disagree")
    for ranges in meta["included_ranges"].values():
        for value in ranges:
            _bounds(value)


def validate_table_view(data: TableView | dict[str, Any]) -> TableView:
    wire = _wire(data)
    validate_schema("table_view_v1", wire, code="INVALID_TABLE_CONTEXT", stage="context")
    try:
        _check_meta(wire["text"], wire["context_meta"])
        if wire["table_view_hash"] != wire["context_meta"]["table_view_hash"]:
            raise ValueError("TableView hash fields disagree")
    except ValueError as exc:
        _fail("INVALID_TABLE_CONTEXT", "context", str(exc))
    return TableView.from_dict(wire)


def validate_query_context(data: QueryContext | dict[str, Any]) -> QueryContext:
    wire = _wire(data)
    validate_schema("query_context_v1", wire, code="INVALID_QUERY", stage="context")
    try:
        _check_meta(wire["table_view"], wire["context_meta"])
    except ValueError as exc:
        _fail("INVALID_QUERY", "context", str(exc))
    return QueryContext.from_dict(wire)


def validate_query(data: Query | dict[str, Any]) -> Query:
    wire = _wire(data)
    validate_schema("query_v1", wire, code="INVALID_QUERY", stage="load")
    if (not wire["id"].strip() or wire["id"] != wire["id"].strip()
            or len(wire["id"]) > 256 or any(char in wire["id"] for char in "\r\n\x00")):
        _fail("INVALID_QUERY", "load", "id must be a non-empty single-line string of at most 256 characters")
    if not re.fullmatch(r"[^/\\\x00]+\.xlsx", wire["file_name"]):
        _fail("INVALID_QUERY", "load", "file_name must be an XLSX basename")
    return Query.from_dict(wire)


def _refs(value: Any):
    if isinstance(value, str) and re.fullmatch(r"\$s[1-9][0-9]*", value):
        yield value[1:]
    elif isinstance(value, dict):
        if set(value) != {"literal"}:
            for child in value.values():
                yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


def validate_plan(plan: dict[str, Any], query_context: QueryContext) -> ValidatedPlan:
    if isinstance(plan, dict) and isinstance(plan.get("steps"), list):
        for step in plan["steps"]:
            if isinstance(step, dict) and isinstance(step.get("op"), str) and step["op"] not in get_args(Operator):
                _fail("UNKNOWN_OPERATOR", "plan_validate", f"Unknown operator: {step['op']}")
    validate_schema("plan_v1", plan, code="INVALID_PLAN_SCHEMA", stage="plan_validate")
    if plan["query_id"] != query_context.query_id or plan["table_id"] != query_context.table_id:
        _fail("INVALID_PLAN_SCHEMA", "plan_validate", "Plan query/table does not match request")
    seen: set[str] = set()
    for step in plan["steps"]:
        if step["id"] in seen or any(ref not in seen for ref in _refs(step["args"])):
            _fail("INVALID_PLAN_SCHEMA", "plan_validate", "Duplicate step ID, self/forward or missing reference", step=step["id"])
        args = step["args"]
        if query_context.input_mode == "raw" and step["op"] in ("lookup_cell", "select_records"):
            if "section_path" in args or any("column_header_path" in selector for selector in args.get("fields", {}).values()):
                _fail("UNSUPPORTED_SELECTOR", "plan_validate", "Raw mode requires coordinate selectors", step=step["id"])
        try:
            if step["op"] == "lookup_cell" and "cell" in args:
                cell_position(args["cell"])
            if step["op"] == "select_records":
                if "row_range" in args:
                    start, end = map(int, args["row_range"].split(":"))
                    if not 1 <= start <= end <= 1048576:
                        raise ValueError("Invalid record row range")
                for selector in args["fields"].values():
                    if "column" in selector:
                        cell_position(selector["column"] + "1")
        except ValueError as exc:
            _fail("INVALID_PLAN_SCHEMA", "plan_validate", str(exc), step=step["id"])
        seen.add(step["id"])
    if plan["return"]["ref"][1:] not in seen:
        _fail("INVALID_PLAN_SCHEMA", "plan_validate", "Missing return reference")
    # Own a copy so caller mutation cannot change a previously checked plan.
    return ValidatedPlan(deepcopy(plan))


def validate_runtime_limits(data: RuntimeLimits | dict[str, Any]) -> RuntimeLimits:
    wire = _wire(data)
    validate_schema("runtime_limits_v1", wire, code="INVALID_QUERY", stage="load")
    return RuntimeLimits.from_dict(wire)
