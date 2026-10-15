"""Resolve plan references while preserving evidence and unit lineage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
import re
import unicodedata

from ..contracts import CellRef, PipelineError, StepResult, TableContext
from ..contract_validation import cell_position

PathPart = str | int
ValuePath = tuple[PathPart, ...]


@dataclass
class TrackedValue:
    value: Any
    evidence: list[CellRef] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    origins: dict[ValuePath, list[CellRef]] = field(default_factory=dict)
    units: dict[ValuePath, set[str]] = field(default_factory=dict)


@dataclass
class StateValue:
    result: StepResult
    origins: dict[ValuePath, list[CellRef]]
    units: dict[ValuePath, set[str]]


def normalize_label(value: str) -> str:
    """Normalize semantic labels without fuzzy matching or alias expansion."""
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def unique_refs(refs: list[str]) -> list[str]:
    return list(dict.fromkeys(refs))


def unique_cell_refs(refs: list[CellRef]) -> list[CellRef]:
    """Deduplicate immutable evidence by its stable worksheet coordinate."""
    unique: dict[tuple[str, str], CellRef] = {}
    for ref in refs:
        unique[(ref.sheet, ref.cell)] = ref
    return list(unique.values())


def union_evidence(table: TableContext, *groups: list[CellRef]) -> list[CellRef]:
    unique: dict[tuple[str, str], CellRef] = {}
    for group in groups:
        for ref in group:
            unique[(ref.sheet, ref.cell)] = ref
    sheet_order = {sheet.name: index for index, sheet in enumerate(table.sheets)}
    return sorted(unique.values(), key=lambda ref: (sheet_order.get(ref.sheet, len(sheet_order)), cell_position(ref.cell)))


def _merge_path_maps(
    maps: list[dict[ValuePath, list[CellRef]]],
) -> dict[ValuePath, list[CellRef]]:
    merged: dict[ValuePath, list[CellRef]] = {}
    for mapping in maps:
        for path, refs in mapping.items():
            merged[path] = unique_cell_refs([*merged.get(path, []), *refs])
    return merged


def _merge_unit_maps(maps: list[dict[ValuePath, set[str]]]) -> dict[ValuePath, set[str]]:
    merged: dict[ValuePath, set[str]] = {}
    for mapping in maps:
        for path, units in mapping.items():
            merged.setdefault(path, set()).update(units)
    return merged


def _prefix_origins(mapping: dict[ValuePath, list[CellRef]], part: PathPart) -> dict[ValuePath, list[CellRef]]:
    return {(part, *path): list(refs) for path, refs in mapping.items()}


def _prefix_units(mapping: dict[ValuePath, set[str]], part: PathPart) -> dict[ValuePath, set[str]]:
    return {(part, *path): set(units) for path, units in mapping.items()}


def _literal_scalar(value: Any) -> bool:
    return value is None or type(value) in (str, int, bool) or isinstance(value, (float, Decimal, date))


def _resolve_node(
    value: Any,
    state: dict[str, StateValue],
    *,
    query_id: str,
    step_id: str,
) -> TrackedValue:
    if isinstance(value, str) and re.fullmatch(r"\$s[1-9][0-9]*", value):
        ref = value[1:]
        entry = state.get(ref)
        if entry is None:
            raise PipelineError(code="INVALID_PLAN_SCHEMA", stage="resolve", message=f"Reference {value} is not available", query_id=query_id, step_id=step_id)
        return TrackedValue(
            value=entry.result.value,
            evidence=list(entry.result.evidence),
            refs=[ref],
            origins={path: list(refs) for path, refs in entry.origins.items()},
            units={path: set(units) for path, units in entry.units.items()},
        )
    if isinstance(value, dict):
        if set(value) == {"literal"}:
            literal = value["literal"]
            if not _literal_scalar(literal):
                raise PipelineError(code="TYPE_ERROR", stage="resolve", message="literal wrapper must contain a scalar", query_id=query_id, step_id=step_id)
            return TrackedValue(literal)
        children = {key: _resolve_node(child, state, query_id=query_id, step_id=step_id) for key, child in value.items()}
        return TrackedValue(
            value={key: child.value for key, child in children.items()},
            evidence=[ref for child in children.values() for ref in child.evidence],
            refs=unique_refs([ref for child in children.values() for ref in child.refs]),
            origins=_merge_path_maps([_prefix_origins(child.origins, key) for key, child in children.items()]),
            units=_merge_unit_maps([_prefix_units(child.units, key) for key, child in children.items()]),
        )
    if isinstance(value, list):
        children = [_resolve_node(child, state, query_id=query_id, step_id=step_id) for child in value]
        return TrackedValue(
            value=[child.value for child in children],
            evidence=[ref for child in children for ref in child.evidence],
            refs=unique_refs([ref for child in children for ref in child.refs]),
            origins=_merge_path_maps([_prefix_origins(child.origins, index) for index, child in enumerate(children)]),
            units=_merge_unit_maps([_prefix_units(child.units, index) for index, child in enumerate(children)]),
        )
    return TrackedValue(value)


def resolve_arguments(
    args: dict[str, Any],
    state: dict[str, StateValue],
    *,
    query_id: str,
    step_id: str,
) -> tuple[dict[str, Any], dict[str, TrackedValue], list[str], list[CellRef]]:
    tracked = {
        key: _resolve_node(value, state, query_id=query_id, step_id=step_id)
        for key, value in args.items()
    }
    resolved = {key: item.value for key, item in tracked.items()}
    refs = unique_refs([ref for item in tracked.values() for ref in item.refs])
    evidence = [ref for item in tracked.values() for ref in item.evidence]
    return resolved, tracked, refs, evidence


def root_origins(tracks: list[TrackedValue]) -> dict[ValuePath, list[CellRef]]:
    refs = [ref for track in tracks for refs in track.origins.values() for ref in refs]
    return {(): unique_cell_refs(refs)} if refs else {}


def root_units(tracks: list[TrackedValue]) -> dict[ValuePath, set[str]]:
    units = {unit for track in tracks for values in track.units.values() for unit in values}
    return {(): units} if units else {}


def unit_values_at(track: TrackedValue, path: ValuePath = ()) -> set[str]:
    return set(track.units.get(path, set()))


def remap_sequence_paths(track: TrackedValue, source_indices: list[int]) -> tuple[dict[ValuePath, list[CellRef]], dict[ValuePath, set[str]]]:
    origins: dict[ValuePath, list[CellRef]] = {}
    units: dict[ValuePath, set[str]] = {}
    old_to_new = {old: new for new, old in enumerate(source_indices)}
    for path, refs in track.origins.items():
        if path and isinstance(path[0], int) and path[0] in old_to_new:
            origins[(old_to_new[path[0]], *path[1:])] = list(refs)
    for path, values in track.units.items():
        if path and isinstance(path[0], int) and path[0] in old_to_new:
            units[(old_to_new[path[0]], *path[1:])] = set(values)
    return origins, units


def artifact_value(value: Any) -> Any:
    """Convert runtime values to lossless JSON artifact values."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite Decimal cannot be written to an artifact")
        return {"$type": "decimal", "value": str(value)}
    if type(value) is date:
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, dict):
        return {key: artifact_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [artifact_value(child) for child in value]
    return value
