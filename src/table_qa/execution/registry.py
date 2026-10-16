"""Closed plan_v1 tool registry. Tools only read the supplied TableContext."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any

from ..contracts import CellRef, PipelineError, RawCell, RuntimeLimits, TableContext, validate_runtime_value
from ..contract_validation import cell_position
from ..numeric import MissingNumericValue, excel_percent_count, numeric_unit, parse_numeric_text
from .resolver import (
    TrackedValue, ValuePath, normalize_label, remap_sequence_paths, root_origins,
    unique_cell_refs, unit_values_at,
)


@dataclass
class ToolOutcome:
    value: Any
    evidence: list[CellRef] = field(default_factory=list)
    origins: dict[ValuePath, list[CellRef]] = field(default_factory=dict)
    units: dict[ValuePath, set[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ExecutionBudget:
    limits: RuntimeLimits
    selected_cells: int = 0
    cell_indexes: dict[str, dict[str, RawCell]] = field(default_factory=dict)

    def cells_for(self, sheet) -> dict[str, RawCell]:
        if sheet.name not in self.cell_indexes:
            self.cell_indexes[sheet.name] = _sheet_cells(sheet)
        return self.cell_indexes[sheet.name]

    def reserve_cells(self, count: int, *, query_id: str, step_id: str) -> None:
        if count < 0 or self.selected_cells + count > self.limits.max_selected_cells:
            raise PipelineError(code="RESOURCE_LIMIT", stage="execute", message="Plan exceeds max_selected_cells", query_id=query_id, step_id=step_id,
                                details={"requested": count, "already_selected": self.selected_cells, "limit": self.limits.max_selected_cells})
        self.selected_cells += count

    def check_records(self, count: int, *, query_id: str, step_id: str) -> None:
        if count > self.limits.max_records:
            raise PipelineError(code="RESOURCE_LIMIT", stage="execute", message="Plan exceeds max_records", query_id=query_id, step_id=step_id,
                                details={"requested": count, "limit": self.limits.max_records})


def _error(code: str, message: str, query_id: str, step_id: str, **details: Any) -> PipelineError:
    return PipelineError(code=code, stage="execute", message=message, query_id=query_id, step_id=step_id, details=details)


def _sheet(table: TableContext, name: str, query_id: str, step_id: str):
    for sheet in table.sheets:
        if sheet.name == name:
            return sheet
    raise _error("CELL_NOT_FOUND", f"Worksheet not found: {name}", query_id, step_id, sheet=name)


def _cells(sheet) -> list[RawCell]:
    if sheet.raw_rows is not None:
        return [cell for row in sheet.raw_rows for cell in row.cells]
    return list(sheet.parsed_cells or [])


def _sheet_cells(sheet) -> dict[str, RawCell]:
    return {cell.cell: cell for cell in _cells(sheet)}


def _range_bounds(value: str) -> tuple[int, int, int, int]:
    start, end = value.split(":")
    r1, c1 = cell_position(start)
    r2, c2 = cell_position(end)
    if r1 > r2 or c1 > c2:
        raise ValueError(f"Invalid or reversed cell range: {value}")
    return r1, c1, r2, c2


def _inside(cell: str, bounds: tuple[int, int, int, int]) -> bool:
    row, column = cell_position(cell)
    r1, c1, r2, c2 = bounds
    return r1 <= row <= r2 and c1 <= column <= c2


def _ref(cell: RawCell) -> CellRef:
    return CellRef(sheet=cell.sheet, cell=cell.cell)


def _cell_candidate(cell: RawCell) -> dict[str, Any]:
    return {"sheet": cell.sheet, "cell": cell.cell, "value": cell.display_value}


def _find_coordinate_cell(sheet, cell: str, *, follow_merge: bool, cells: dict[str, RawCell] | None = None) -> RawCell | None:
    cells = cells or _sheet_cells(sheet)
    found = cells.get(cell)
    if found is not None:
        if follow_merge and found.merged_anchor:
            return cells.get(found.merged_anchor)
        return found
    if follow_merge:
        target_row, target_col = cell_position(cell)
        for merged in sheet.merged_ranges:
            bounds = _range_bounds(merged)
            if bounds[0] <= target_row <= bounds[2] and bounds[1] <= target_col <= bounds[3]:
                start = merged.split(":", 1)[0]
                return cells.get(start)
    return None


def _excel_serial_date(value: Any, table: TableContext, query_id: str, step_id: str, ref: CellRef) -> date:
    number = _number(value, query_id, step_id)
    serial = float(number)
    if not math.isfinite(serial):
        raise _error("TYPE_ERROR", "Excel date serial must be finite", query_id, step_id, cell=f"{ref.sheet}!{ref.cell}")
    whole_days = math.floor(serial)
    if table.metadata["date_system"] == "1904":
        base = datetime(1904, 1, 1)
    elif whole_days == 60:
        raise _error("TYPE_ERROR", "Excel serial 60 is not a valid Gregorian date", query_id, step_id, cell=f"{ref.sheet}!{ref.cell}")
    else:
        base = datetime(1899, 12, 31) if whole_days < 60 else datetime(1899, 12, 30)
    try:
        return (base + timedelta(days=whole_days)).date()
    except (OverflowError, ValueError) as exc:
        raise _error("TYPE_ERROR", "Excel date serial is outside the supported date range", query_id, step_id, cell=f"{ref.sheet}!{ref.cell}") from exc


def _number(value: Any, query_id: str, step_id: str) -> int | Decimal:
    if type(value) is int:
        return value
    if isinstance(value, Decimal) and value.is_finite():
        return value
    if isinstance(value, float) and math.isfinite(value):
        return Decimal(str(value))
    raise _error("TYPE_ERROR", "Expected a finite number; booleans and numeric strings are not implicit numbers", query_id, step_id)


def _numeric_pair(left: Any, right: Any, query_id: str, step_id: str) -> tuple[int | Decimal, int | Decimal]:
    return _number(left, query_id, step_id), _number(right, query_id, step_id)


def _check_unit_compatibility(left: TrackedValue, right: TrackedValue, query_id: str, step_id: str) -> set[str]:
    left_units, right_units = unit_values_at(left), unit_values_at(right)
    if left_units and right_units and left_units != right_units:
        raise _error("UNIT_MISMATCH", "Arithmetic combines incompatible known units", query_id, step_id,
                     left_units=sorted(left_units), right_units=sorted(right_units))
    return left_units or right_units


def _check_units_many(units: list[set[str]], query_id: str, step_id: str) -> set[str]:
    known = [item for item in units if item]
    if not known:
        return set()
    expected = known[0]
    if any(item != expected for item in known[1:]):
        raise _error("UNIT_MISMATCH", "Operation combines incompatible known units", query_id, step_id,
                     units=[sorted(item) for item in known])
    return set(expected)


def _cell_value(cell: RawCell, as_type: str | None, table: TableContext, query_id: str, step_id: str) -> Any:
    ref = _ref(cell)
    if cell.value_type == "error":
        raise _error("MISSING_VALUE", "Workbook cell contains an Excel error", query_id, step_id, cell=f"{ref.sheet}!{ref.cell}", value=cell.value_raw)
    if cell.value_type == "empty" or cell.value_raw is None:
        raise _error("MISSING_VALUE", "Workbook cell is empty or has no cached formula value", query_id, step_id, cell=f"{ref.sheet}!{ref.cell}")
    if cell.value_type == "date":
        if cell.date_iso:
            typed: Any = date.fromisoformat(cell.date_iso)
        elif isinstance(cell.value_raw, (int, float, Decimal)) and type(cell.value_raw) is not bool:
            typed = _excel_serial_date(cell.value_raw, table, query_id, step_id, ref)
        else:
            raise _error("TYPE_ERROR", "Date cell has no usable ISO value or Excel serial", query_id, step_id, cell=f"{ref.sheet}!{ref.cell}")
    elif cell.value_type == "number":
        typed = _number(cell.value_raw, query_id, step_id)
    elif cell.value_type == "boolean":
        if type(cell.value_raw) is not bool:
            raise _error("TYPE_ERROR", "Boolean cell has an invalid value", query_id, step_id, cell=f"{ref.sheet}!{ref.cell}")
        typed = cell.value_raw
    else:
        typed = cell.value_raw

    if as_type is None:
        return typed
    if as_type == "number":
        if cell.value_type == "string" and isinstance(cell.value_raw, str):
            try:
                return parse_numeric_text(cell.value_raw)
            except MissingNumericValue as exc:
                raise _error("MISSING_VALUE", str(exc), query_id, step_id,
                             cell=f"{ref.sheet}!{ref.cell}", value=cell.value_raw) from exc
            except ValueError as exc:
                raise _error("TYPE_ERROR", "String cell cannot be parsed as a number", query_id, step_id,
                             cell=f"{ref.sheet}!{ref.cell}", value=cell.value_raw) from exc
        number = _number(typed, query_id, step_id)
        percent_count = excel_percent_count(cell.number_format)
        return _as_decimal(number) * (Decimal(100) ** percent_count) if percent_count else number
    if as_type == "date":
        if isinstance(typed, date):
            return typed
        if isinstance(typed, str):
            try:
                return date.fromisoformat(typed.strip())
            except ValueError as exc:
                raise _error("TYPE_ERROR", "String cell is not an ISO date", query_id, step_id, cell=f"{ref.sheet}!{ref.cell}") from exc
        return _excel_serial_date(typed, table, query_id, step_id, ref)
    if as_type == "string":
        return cell.display_value
    if as_type == "boolean":
        if type(typed) is bool:
            return typed
        if isinstance(typed, str) and typed.strip().casefold() in ("true", "false"):
            return typed.strip().casefold() == "true"
        raise _error("TYPE_ERROR", "Cell cannot be explicitly converted to boolean", query_id, step_id, cell=f"{ref.sheet}!{ref.cell}")
    raise _error("TYPE_ERROR", f"Unsupported as_type: {as_type}", query_id, step_id)


def _lookup_cell(args: dict[str, Any], table: TableContext, query_id: str, step_id: str, budget: ExecutionBudget) -> ToolOutcome:
    sheet = _sheet(table, args["sheet"], query_id, step_id)
    candidate: RawCell | None = None
    if "cell" in args:
        coordinate = args["cell"]
        try:
            bounds = _range_bounds(sheet.dimensions)
            if not _inside(coordinate, bounds):
                raise _error("CELL_NOT_FOUND", "Cell is outside worksheet dimensions", query_id, step_id, sheet=sheet.name, cell=coordinate)
        except ValueError as exc:
            raise _error("INVALID_TABLE_CONTEXT", str(exc), query_id, step_id, sheet=sheet.name) from exc
        candidate = _find_coordinate_cell(sheet, coordinate, follow_merge=True, cells=budget.cells_for(sheet))
        if candidate is None:
            row, column = cell_position(coordinate)
            suggestions = sorted(_cells(sheet), key=lambda item: (abs(item.row_index - row) + abs(item.column_index - column), item.row_index, item.column_index))[:10]
            raise _error("CELL_NOT_FOUND", "No cell exists at the requested coordinate", query_id, step_id,
                         sheet=sheet.name, cell=coordinate, candidates=[_cell_candidate(item) for item in suggestions])
    else:
        if sheet.parsed_cells is None:
            raise PipelineError(code="UNSUPPORTED_SELECTOR", stage="resolve", message="Semantic selectors require parsed input", query_id=query_id, step_id=step_id)
        wanted = tuple(
            tuple(normalize_label(part) for part in args[path_name])
            for path_name in ("section_path", "row_header_path", "column_header_path")
        )
        matches = [cell for cell in sheet.parsed_cells if (
            tuple(normalize_label(part) for part in cell.section_path),
            tuple(normalize_label(part) for part in cell.row_header_path),
            tuple(normalize_label(part) for part in cell.column_header_path),
        ) == wanted]
        if not matches:
            def path_score(cell: RawCell) -> int:
                paths = (cell.section_path, cell.row_header_path, cell.column_header_path)
                return sum(tuple(normalize_label(part) for part in actual) == expected for actual, expected in zip(paths, wanted))

            suggestions = sorted(sheet.parsed_cells, key=lambda item: (-path_score(item), item.row_index, item.column_index))[:10]
            raise PipelineError(code="CELL_NOT_FOUND", stage="resolve", message="No parsed fact matches the complete semantic selector", query_id=query_id, step_id=step_id,
                                details={"sheet": sheet.name, "selector": {key: args[key] for key in ("section_path", "row_header_path", "column_header_path")},
                                         "candidates": [{**_cell_candidate(item), "section_path": item.section_path,
                                                         "row_header_path": item.row_header_path,
                                                         "column_header_path": item.column_header_path,
                                                         "matched_paths": path_score(item)} for item in suggestions]})
        if len(matches) > 1:
            raise PipelineError(code="AMBIGUOUS_CELL", stage="resolve", message="Semantic selector matches multiple parsed facts", query_id=query_id, step_id=step_id,
                                details={"candidates": [{"sheet": item.sheet, "cell": item.cell, "value": item.display_value} for item in matches]})
        candidate = matches[0]
        if candidate.merged_anchor:
            candidate = _find_coordinate_cell(sheet, candidate.cell, follow_merge=True, cells=budget.cells_for(sheet))
            if candidate is None:
                raise _error("CELL_NOT_FOUND", "Merged cell anchor is missing", query_id, step_id, sheet=sheet.name)
    assert candidate is not None
    budget.reserve_cells(1, query_id=query_id, step_id=step_id)
    value = _cell_value(candidate, args.get("as_type"), table, query_id, step_id)
    reference = _ref(candidate)
    unit = candidate.unit
    if unit is None and args.get("as_type") == "number":
        unit = numeric_unit(candidate.value_raw if isinstance(candidate.value_raw, str) else None,
                            candidate.number_format)
    units = {(): {unit}} if unit else {}
    warnings = []
    if (args.get("as_type") == "date" or candidate.value_type == "date") and isinstance(candidate.value_raw, (int, float, Decimal)):
        if Decimal(str(candidate.value_raw)) % 1:
            warnings.append(f"time_component_discarded={reference.sheet}!{reference.cell}")
    return ToolOutcome(value=value, evidence=[reference], origins={(): [reference]}, units=units, warnings=warnings)


def _select_records(args: dict[str, Any], table: TableContext, query_id: str, step_id: str, budget: ExecutionBudget) -> ToolOutcome:
    sheet = _sheet(table, args["sheet"], query_id, step_id)
    if "row_range" in args:
        start, end = (int(part) for part in args["row_range"].split(":"))
        scope_kind = "row_range"
    else:
        if sheet.parsed_cells is None:
            raise PipelineError(code="UNSUPPORTED_SELECTOR", stage="resolve", message="Section selectors require parsed input", query_id=query_id, step_id=step_id)
        wanted = tuple(normalize_label(part) for part in args["section_path"])
        sections = [section for section in sheet.sections if tuple(normalize_label(part) for part in section.path) == wanted]
        if not sections:
            raise PipelineError(code="CELL_NOT_FOUND", stage="resolve", message="No parsed section matches the complete section_path", query_id=query_id, step_id=step_id,
                                details={"sheet": sheet.name, "section_path": args["section_path"], "candidates": [section.path for section in sheet.sections]})
        if len(sections) > 1:
            raise PipelineError(code="AMBIGUOUS_CELL", stage="resolve", message="Section path matches multiple parsed sections", query_id=query_id, step_id=step_id,
                                details={"section_path": args["section_path"], "candidates": [{"path": section.path, "rows": [section.row_start, section.row_end]} for section in sections]})
        start, end = sections[0].row_start, sections[0].row_end
        scope_kind = "section"
    try:
        _, _, dimension_end, _ = _range_bounds(sheet.dimensions)
    except ValueError as exc:
        raise _error("INVALID_TABLE_CONTEXT", str(exc), query_id, step_id, sheet=sheet.name) from exc
    if start < 1 or end < start or end > dimension_end:
        raise _error("CELL_NOT_FOUND", "Record scope is outside worksheet dimensions", query_id, step_id,
                     sheet=sheet.name, scope=[start, end], dimensions=sheet.dimensions)
    row_count = end - start + 1
    budget.check_records(row_count, query_id=query_id, step_id=step_id)
    fields = args["fields"]
    budget.reserve_cells(row_count * len(fields), query_id=query_id, step_id=step_id)

    columns: dict[str, int] = {}
    cells = list(budget.cells_for(sheet).values())
    for field_name, selector in fields.items():
        if "column" in selector:
            column = cell_position(selector["column"] + "1")[1]
            columns[field_name] = column
        else:
            if sheet.parsed_cells is None:
                raise PipelineError(code="UNSUPPORTED_SELECTOR", stage="resolve", message="Header-path fields require parsed input", query_id=query_id, step_id=step_id)
            wanted = tuple(normalize_label(part) for part in selector["column_header_path"])
            candidate_columns = {
                cell.column_index for cell in sheet.parsed_cells
                if start <= cell.row_index <= end
                and tuple(normalize_label(part) for part in cell.column_header_path) == wanted
            }
            if not candidate_columns:
                suggestions = sorted(
                    (cell for cell in sheet.parsed_cells if start <= cell.row_index <= end),
                    key=lambda cell: (cell.row_index, cell.column_index),
                )[:10]
                raise PipelineError(code="CELL_NOT_FOUND", stage="resolve", message=f"No column matches field {field_name}", query_id=query_id, step_id=step_id,
                                    details={"sheet": sheet.name, "field": field_name, "column_header_path": selector["column_header_path"],
                                             "scope": [start, end], "candidates": [{**_cell_candidate(item), "column_header_path": item.column_header_path} for item in suggestions]})
            if len(candidate_columns) > 1:
                raise PipelineError(code="AMBIGUOUS_CELL", stage="resolve", message=f"Multiple columns match field {field_name}", query_id=query_id, step_id=step_id,
                                    details={"sheet": sheet.name, "field": field_name, "columns": sorted(candidate_columns)})
            columns[field_name] = next(iter(candidate_columns))

    rows_by_index: dict[int, dict[int, RawCell]] = {}
    selected_columns = set(columns.values())
    for cell in cells:
        if start <= cell.row_index <= end and cell.column_index in selected_columns:
            rows_by_index.setdefault(cell.row_index, {})[cell.column_index] = cell
    records: list[dict[str, Any]] = []
    evidence: list[CellRef] = []
    origins: dict[ValuePath, list[CellRef]] = {}
    units: dict[ValuePath, set[str]] = {}
    warnings: list[str] = []
    for index, row_index in enumerate(range(start, end + 1)):
        record: dict[str, Any] = {}
        for field_name, selector in fields.items():
            column = columns[field_name]
            cell = rows_by_index.get(row_index, {}).get(column)
            # A non-anchor merged cell does not duplicate the anchor in another record.
            if cell is not None and cell.merged_anchor:
                cell = None
            if cell is None:
                record[field_name] = None
                continue
            ref = _ref(cell)
            if cell.value_type == "empty" and cell.formula is None:
                value = None
            else:
                value = _cell_value(cell, selector.get("as_type"), table, query_id, step_id)
            record[field_name] = value
            evidence.append(ref)
            origins[(index, field_name)] = [ref]
            unit = cell.unit
            if unit is None and selector.get("as_type") == "number":
                unit = numeric_unit(cell.value_raw if isinstance(cell.value_raw, str) else None,
                                    cell.number_format)
            if unit:
                units[(index, field_name)] = {unit}
            if (selector.get("as_type") == "date" or cell.value_type == "date") and isinstance(cell.value_raw, (int, float, Decimal)):
                if Decimal(str(cell.value_raw)) % 1:
                    warnings.append(f"time_component_discarded={ref.sheet}!{ref.cell}")
        records.append(record)
    warnings.insert(0, f"selected_scope={scope_kind}:{start}:{end}")
    return ToolOutcome(value=records, evidence=evidence, origins=origins, units=units, warnings=warnings)


def _check_scalar_collection(items: Any, query_id: str, step_id: str) -> None:
    if not isinstance(items, list):
        raise _error("TYPE_ERROR", "Expected a list", query_id, step_id)
    if len(items) == 0:
        return
    scalar_items = all(not isinstance(item, (dict, list)) for item in items)
    record_items = all(isinstance(item, dict) for item in items)
    if not scalar_items and not record_items:
        raise _error("TYPE_ERROR", "Collections cannot mix scalars and records or contain nested collections", query_id, step_id)
    try:
        validate_runtime_value(items)
    except ValueError as exc:
        raise _error("TYPE_ERROR", str(exc), query_id, step_id) from exc


def _add_numeric(left: Any, right: Any, operation: str, query_id: str, step_id: str) -> Any:
    lhs, rhs = _numeric_pair(left, right, query_id, step_id)
    if operation == "add":
        return lhs + rhs
    if operation == "subtract":
        return lhs - rhs
    if operation == "abs_diff":
        return abs(lhs - rhs)
    if operation == "divide":
        if rhs == 0:
            raise _error("DIVISION_BY_ZERO", "Division denominator is zero", query_id, step_id)
        try:
            return _as_decimal(lhs) / _as_decimal(rhs)
        except (ArithmeticError, InvalidOperation) as exc:
            raise _error("TYPE_ERROR", "Division produced an invalid number", query_id, step_id) from exc
    raise _error("EXECUTION_ERROR", f"Unknown arithmetic operation: {operation}", query_id, step_id)


def _as_decimal(value: int | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(value)


def _family(value: Any) -> str:
    if type(value) is int or isinstance(value, Decimal):
        return "number"
    if type(value) is bool:
        return "boolean"
    if type(value) is str:
        return "string"
    if type(value) is date:
        return "date"
    if value is None:
        return "null"
    return "invalid"


def _compare(left: Any, right: Any, predicate: str, query_id: str, step_id: str) -> bool:
    left_family, right_family = _family(left), _family(right)
    if left_family == right_family == "number":
        pass
    elif left_family == right_family == "date":
        pass
    elif left_family == right_family and left_family in ("string", "boolean", "null") and predicate in ("eq", "ne"):
        pass
    else:
        raise _error("TYPE_ERROR", f"Incompatible values for {predicate}: {left_family} and {right_family}", query_id, step_id)
    try:
        if predicate == "eq":
            return left == right
        if predicate == "ne":
            return left != right
        if predicate == "gt":
            return left > right
        if predicate == "gte":
            return left >= right
        if predicate == "lt":
            return left < right
        if predicate == "lte":
            return left <= right
    except TypeError as exc:
        raise _error("TYPE_ERROR", "Values cannot be ordered", query_id, step_id) from exc
    raise _error("INVALID_PLAN_SCHEMA", f"Unknown predicate: {predicate}", query_id, step_id)


def _ordered_value(value: Any, query_id: str, step_id: str) -> tuple[str, Any]:
    family = _family(value)
    if family == "number":
        return family, _as_decimal(_number(value, query_id, step_id))
    if family in ("string", "date"):
        return family, value
    if value is None:
        raise _error("MISSING_VALUE", "Cannot order a null field", query_id, step_id)
    raise _error("TYPE_ERROR", f"Cannot order field of type {family}", query_id, step_id)


def _same_ordered_family(values: list[Any], query_id: str, step_id: str) -> list[tuple[str, Any]]:
    converted = [_ordered_value(value, query_id, step_id) for value in values]
    if converted and any(item[0] != converted[0][0] for item in converted):
        raise _error("TYPE_ERROR", "Ordered values must have compatible types", query_id, step_id)
    return converted


def _add_months(start: date, months: int) -> date:
    month_index = start.year * 12 + (start.month - 1) + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    if not 1 <= year <= 9999:
        raise OverflowError("Date anniversary is outside supported years")
    first_next = date(year + (month == 12), 1 if month == 12 else month + 1, 1) if year < 9999 or month < 12 else None
    last_day = (first_next - timedelta(days=1)).day if first_next is not None else 31
    return date(year, month, min(start.day, last_day))


def _add_years(start: date, years: int) -> date:
    year = start.year + years
    if not 1 <= year <= 9999:
        raise OverflowError("Date anniversary is outside supported years")
    try:
        return date(year, start.month, start.day)
    except ValueError:
        return date(year, start.month, 28 if start.month == 2 else 30)


def _date_diff(end: Any, start: Any, function: str, query_id: str, step_id: str) -> int:
    if type(end) is not date or type(start) is not date:
        raise _error("TYPE_ERROR", f"{function} requires date inputs", query_id, step_id)
    sign = 1
    if end < start:
        end, start = start, end
        sign = -1
    if function == "date_diff_days":
        return sign * (end - start).days
    if function == "date_diff_months":
        count = (end.year - start.year) * 12 + end.month - start.month
        try:
            if _add_months(start, count) > end:
                count -= 1
        except OverflowError as exc:
            raise _error("TYPE_ERROR", str(exc), query_id, step_id) from exc
        return sign * count
    if function == "date_diff_years":
        count = end.year - start.year
        try:
            if _add_years(start, count) > end:
                count -= 1
        except OverflowError as exc:
            raise _error("TYPE_ERROR", str(exc), query_id, step_id) from exc
        return sign * count
    raise _error("INVALID_PLAN_SCHEMA", f"Unsupported date function: {function}", query_id, step_id)


def _records(value: Any, query_id: str, step_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _error("TYPE_ERROR", "Expected a list of records", query_id, step_id)
    if value:
        fields = set(value[0])
        if any(set(item) != fields for item in value):
            raise _error("TYPE_ERROR", "Record fields are inconsistent", query_id, step_id)
    return value


def _field(record: dict[str, Any], name: str, query_id: str, step_id: str) -> Any:
    if name not in record:
        raise _error("TYPE_ERROR", f"Record has no field {name}", query_id, step_id, field=name)
    value = record[name]
    if value is None:
        raise _error("MISSING_VALUE", f"Record field {name} is null", query_id, step_id, field=name)
    return value


def _mapped_paths(track: TrackedValue, old_indices: list[int]) -> tuple[dict[ValuePath, list[CellRef]], dict[ValuePath, set[str]]]:
    return remap_sequence_paths(track, old_indices)


def run_tool(
    op: str,
    args: dict[str, Any],
    tracks: dict[str, TrackedValue],
    table: TableContext,
    budget: ExecutionBudget,
    query_id: str,
    step_id: str,
) -> ToolOutcome:
    if op == "lookup_cell":
        return _lookup_cell(args, table, query_id, step_id, budget)
    if op == "select_records":
        return _select_records(args, table, query_id, step_id, budget)
    if op == "collect":
        items = args["items"]
        budget.check_records(len(items), query_id=query_id, step_id=step_id)
        _check_scalar_collection(items, query_id, step_id)
        source = tracks["items"]
        return ToolOutcome(value=items, origins=source.origins, units=source.units)
    if op in ("add", "subtract", "abs_diff", "divide"):
        left_track, right_track = tracks["left"], tracks["right"]
        units: set[str] = set()
        if op != "divide":
            units = _check_unit_compatibility(left_track, right_track, query_id, step_id)
        value = _add_numeric(args["left"], args["right"], op, query_id, step_id)
        return ToolOutcome(value=value, origins=root_origins([left_track, right_track]), units={(): units} if units else {})
    if op == "derive":
        source_track = tracks["source"]
        records = _records(args["source"], query_id, step_id)
        budget.check_records(len(records), query_id=query_id, step_id=step_id)
        if any(args["field"] in record for record in records):
            raise _error("TYPE_ERROR", f"Derived field already exists: {args['field']}", query_id, step_id)
        output: list[dict[str, Any]] = []
        origins = {path: list(refs) for path, refs in source_track.origins.items()}
        units = {path: set(values) for path, values in source_track.units.items()}
        for index, record in enumerate(records):
            values = [_field(record, name, query_id, step_id) for name in args["inputs"]]
            function = args["fn"]
            if function == "absolute":
                result = abs(_number(values[0], query_id, step_id))
                source_origins = [source_track.origins.get((index, name), []) for name in args["inputs"]]
                derived_origins = unique_cell_refs([ref for group in source_origins for ref in group])
                derived_units = set(source_track.units.get((index, args["inputs"][0]), set()))
            elif function.startswith("date_diff_"):
                result = _date_diff(values[0], values[1], function, query_id, step_id)
                unit = "days" if function.endswith("days") else "months" if function.endswith("months") else "years"
                source_origins = [source_track.origins.get((index, name), []) for name in args["inputs"]]
                derived_origins = unique_cell_refs([ref for group in source_origins for ref in group])
                derived_units = {unit}
            else:
                if function != "divide":
                    units_by_field = [source_track.units.get((index, name), set()) for name in args["inputs"]]
                    derived_units = _check_units_many(units_by_field, query_id, step_id)
                else:
                    derived_units = set()
                result = _add_numeric(values[0], values[1], function, query_id, step_id)
                source_origins = [source_track.origins.get((index, name), []) for name in args["inputs"]]
                derived_origins = unique_cell_refs([ref for group in source_origins for ref in group])
            output_record = dict(record)
            output_record[args["field"]] = result
            output.append(output_record)
            if derived_origins:
                origins[(index, args["field"])] = derived_origins
            if derived_units:
                units[(index, args["field"])] = derived_units
        return ToolOutcome(value=output, origins=origins, units=units)
    if op == "aggregate":
        source_track = tracks["source"]
        source = args["source"]
        if not isinstance(source, list):
            raise _error("TYPE_ERROR", "aggregate source must be a list", query_id, step_id)
        budget.check_records(len(source), query_id=query_id, step_id=step_id)
        function = args["fn"]
        if function == "count":
            return ToolOutcome(value=len(source), origins=root_origins([source_track]))
        if not source:
            raise _error("EMPTY_SELECTION", f"aggregate {function} has no values", query_id, step_id)
        if all(isinstance(item, dict) for item in source):
            field_name = args.get("field")
            if field_name is None:
                raise _error("INVALID_PLAN_SCHEMA", "Record aggregate requires field", query_id, step_id)
            values = [_field(item, field_name, query_id, step_id) for item in source]
            paths = [(index, field_name) for index in range(len(source))]
        elif all(not isinstance(item, (dict, list)) for item in source):
            values = source
            paths = [(index,) for index in range(len(source))]
        else:
            raise _error("TYPE_ERROR", "aggregate source must be scalar values or records", query_id, step_id)
        numeric = [_number(value, query_id, step_id) for value in values]
        output_units = _check_units_many([source_track.units.get(path, set()) for path in paths], query_id, step_id)
        if function == "sum":
            result = sum(numeric, 0)
        elif function == "mean":
            result = sum((_as_decimal(value) for value in numeric), Decimal(0)) / Decimal(len(numeric))
        elif function == "max":
            result = max(numeric)
        elif function == "min":
            result = min(numeric)
        else:
            raise _error("INVALID_PLAN_SCHEMA", f"Unsupported aggregate function: {function}", query_id, step_id)
        evidence = [ref for path in paths for ref in source_track.origins.get(path, [])]
        origins = {(): unique_cell_refs(evidence)} if evidence else {}
        return ToolOutcome(value=result, origins=origins, units={(): output_units} if output_units else {})
    if op == "sort":
        source_track = tracks["source"]
        records = _records(args["source"], query_id, step_id)
        budget.check_records(len(records), query_id=query_id, step_id=step_id)
        values = [_field(record, args["by"], query_id, step_id) for record in records]
        _check_units_many([source_track.units.get((index, args["by"]), set()) for index in range(len(records))], query_id, step_id)
        normalized = _same_ordered_family(values, query_id, step_id)
        reverse = args["order"] == "desc"
        indices = sorted(range(len(records)), key=lambda index: normalized[index][1], reverse=reverse)
        sorted_values = [normalized[index][1] for index in indices]
        tie_indices: list[tuple[int, int]] = []
        for pair_index in range(1, len(sorted_values)):
            if sorted_values[pair_index] == sorted_values[pair_index - 1]:
                tie_indices.append((indices[pair_index - 1], indices[pair_index]))
        if tie_indices and args.get("tie_breaker", "row_order") == "error":
            raise _error("TIE_DETECTED", "sort key contains tied records", query_id, step_id, tie_detected=True, tied_pairs=tie_indices)
        origins, units = _mapped_paths(source_track, indices)
        warnings = [f"tie_detected={len(tie_indices)}; stable_input_order_used"] if tie_indices else []
        return ToolOutcome(value=[records[index] for index in indices], origins=origins, units=units, warnings=warnings)
    if op == "take":
        source_track = tracks["source"]
        source = args["source"]
        if not isinstance(source, list):
            raise _error("TYPE_ERROR", "take source must be a list", query_id, step_id)
        budget.check_records(len(source), query_id=query_id, step_id=step_id)
        offset, count = args["offset"], args["count"]
        if offset + count > len(source):
            raise _error("EMPTY_SELECTION", "take range exceeds available items", query_id, step_id,
                         offset=offset, count=count, available=len(source))
        indices = list(range(offset, offset + count))
        origins, units = _mapped_paths(source_track, indices)
        return ToolOutcome(value=[source[index] for index in indices], origins=origins, units=units)
    if op == "project":
        source_track = tracks["source"]
        records = _records(args["source"], query_id, step_id)
        budget.check_records(len(records), query_id=query_id, step_id=step_id)
        field_name = args["field"]
        values = [_field(record, field_name, query_id, step_id) for record in records]
        single = args.get("single", False)
        if single:
            if len(values) != 1:
                raise _error("EMPTY_SELECTION", "project(single=true) requires exactly one record", query_id, step_id, records=len(values))
            path = (0, field_name)
            refs = source_track.origins.get(path, [])
            units = source_track.units.get(path, set())
            return ToolOutcome(value=values[0], origins={(): list(refs)} if refs else {}, units={(): set(units)} if units else {})
        origins: dict[ValuePath, list[CellRef]] = {}
        units: dict[ValuePath, set[str]] = {}
        for index in range(len(values)):
            old_path, new_path = (index, field_name), (index,)
            if old_path in source_track.origins:
                origins[new_path] = list(source_track.origins[old_path])
            if old_path in source_track.units:
                units[new_path] = set(source_track.units[old_path])
        _check_scalar_collection(values, query_id, step_id)
        return ToolOutcome(value=values, origins=origins, units=units)
    if op == "compare":
        if _family(args["left"]) == _family(args["right"]) == "number":
            _check_unit_compatibility(tracks["left"], tracks["right"], query_id, step_id)
        value = _compare(args["left"], args["right"], args["predicate"], query_id, step_id)
        return ToolOutcome(value=value, origins=root_origins([tracks["left"], tracks["right"]]))
    if op == "filter":
        source_track = tracks["source"]
        records = _records(args["source"], query_id, step_id)
        budget.check_records(len(records), query_id=query_id, step_id=step_id)
        selected: list[int] = []
        for index, record in enumerate(records):
            matches = True
            for predicate_index, predicate in enumerate(args["predicates"]):
                field_name = predicate["field"]
                if field_name not in record:
                    raise _error("TYPE_ERROR", f"Record has no field {field_name}",
                                 query_id, step_id, field=field_name)
                actual = record[field_name]
                expected = predicate["value"]
                operation = predicate["predicate"]
                actual_units = source_track.units.get((index, field_name), set())
                if operation == "in" and (
                    not isinstance(expected, list)
                    or any(isinstance(item, (list, dict)) for item in expected)
                ):
                    raise _error("TYPE_ERROR", "in predicate requires a scalar list",
                                 query_id, step_id)
                # Missing cells do not satisfy ordinary filter predicates. Explicit
                # null equality/membership remains available for plans that need it.
                if actual is None:
                    if operation == "eq":
                        accepted = expected is None
                    elif operation == "in":
                        accepted = isinstance(expected, list) and any(item is None for item in expected)
                    else:
                        accepted = False
                elif expected is None and operation in ("eq", "ne"):
                    accepted = operation == "ne"
                elif operation == "in":
                    value_units = [tracks["predicates"].units.get((predicate_index, "value", value_index), set()) for value_index in range(len(expected))]
                    for expected_value, expected_units in zip(expected, value_units):
                        if _family(actual) == _family(expected_value) == "number":
                            _check_units_many([actual_units, expected_units], query_id, step_id)
                    accepted = any(item is not None and _compare(actual, item, "eq", query_id, step_id)
                                   for item in expected)
                else:
                    expected_units = tracks["predicates"].units.get((predicate_index, "value"), set())
                    if _family(actual) == _family(expected) == "number":
                        _check_units_many([actual_units, expected_units], query_id, step_id)
                    accepted = _compare(actual, expected, operation, query_id, step_id)
                if not accepted:
                    matches = False
                    break
            if matches:
                selected.append(index)
        origins, units = _mapped_paths(source_track, selected)
        return ToolOutcome(value=[records[index] for index in selected], origins=origins, units=units)
    if op in ("argmax", "argmin"):
        source_track = tracks["source"]
        records = _records(args["source"], query_id, step_id)
        budget.check_records(len(records), query_id=query_id, step_id=step_id)
        if not records:
            raise _error("EMPTY_SELECTION", f"{op} has no records", query_id, step_id)
        values = [_field(record, args["by"], query_id, step_id) for record in records]
        _check_units_many([source_track.units.get((index, args["by"]), set()) for index in range(len(records))], query_id, step_id)
        normalized = _same_ordered_family(values, query_id, step_id)
        if normalized and normalized[0][0] not in ("number", "date"):
            raise _error("TYPE_ERROR", f"{op} requires a numeric or date field", query_id, step_id)
        target = (max if op == "argmax" else min)(item[1] for item in normalized)
        indices = [index for index, item in enumerate(normalized) if item[1] == target]
        ties = len(indices) > 1
        policy = args.get("ties", "error")
        if ties and policy == "error":
            raise _error("TIE_DETECTED", f"{op} found tied records", query_id, step_id, tie_detected=True, tied_indices=indices)
        if ties and policy == "first":
            indices = indices[:1]
        origins, units = _mapped_paths(source_track, indices)
        warnings = [f"tie_detected={len(indices) if policy == 'all' else 'yes'}; policy={policy}"] if ties else []
        return ToolOutcome(value=[records[index] for index in indices], origins=origins, units=units, warnings=warnings)
    raise _error("UNKNOWN_OPERATOR", f"No tool registered for operator {op}", query_id, step_id)


def _bind(operator: str):
    def dispatch(args, tracks, table, budget, query_id, step_id):
        return run_tool(operator, args, tracks, table, budget, query_id, step_id)
    return dispatch


REGISTRY = {
    operator: _bind(operator)
    for operator in (
        "lookup_cell", "select_records", "collect", "derive", "add", "subtract",
        "abs_diff", "divide", "aggregate", "sort", "take", "project", "compare",
        "filter", "argmax", "argmin",
    )
}
