"""Provider-independent DTOs and artifact serialization for contract version 1.

Dataclass constructors are typed containers. Use contract_validation at trust
boundaries for schema and cross-field validation; constructors do not run tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import math
from types import UnionType
from typing import Any, Literal, NewType, TypeAlias, TypedDict, Union, get_args, get_origin, get_type_hints

InputMode = Literal["raw", "parsed"]
AnswerMode = Literal["plan_tools", "direct"]
Provider = Literal["openrouter", "gemini", "mock"]
ValueType = Literal["number", "date", "string", "boolean", "empty", "error"]
AnswerKind = Literal["integer", "fixed_decimal", "yes_no", "last_name", "category", "ordered_labels", "number_pair", "decimal_pair", "generic_string"]
Stage = Literal["load", "context", "llm", "plan_validate", "resolve", "execute", "format", "collect"]
ErrorCode = Literal[
    "AUTH_ERROR", "MODEL_NOT_FOUND", "STRUCTURED_OUTPUT_UNSUPPORTED", "EMPTY_RESPONSE",
    "PROVIDER_FORBIDDEN", "INVALID_QUERY", "TABLE_NOT_FOUND", "INVALID_TABLE_CONTEXT",
    "CONTEXT_TOO_LARGE", "ANSWER_SPEC_UNSUPPORTED", "INVALID_PLAN_JSON",
    "INVALID_PLAN_SCHEMA", "OUTPUT_TRUNCATED", "UNKNOWN_OPERATOR", "UNSUPPORTED_SELECTOR", "CELL_NOT_FOUND",
    "AMBIGUOUS_CELL", "MISSING_VALUE", "TYPE_ERROR", "UNIT_MISMATCH", "DIVISION_BY_ZERO",
    "EMPTY_SELECTION", "TIE_DETECTED", "RESOURCE_LIMIT", "EXECUTION_ERROR", "API_TIMEOUT",
    "API_RATE_LIMIT", "API_BAD_REQUEST", "API_SERVER_ERROR", "FORMAT_INVALID", "BUDGET_EXCEEDED",
    "QUERY_TIMEOUT", "RUN_TIMEOUT",
]
Operator = Literal[
    "lookup_cell", "select_records", "collect", "derive", "add", "subtract", "abs_diff",
    "divide", "aggregate", "sort", "take", "project", "compare", "filter", "argmax", "argmin",
]
Scalar: TypeAlias = int | Decimal | bool | str | date | None
Record: TypeAlias = dict[str, Scalar]
RuntimeValue: TypeAlias = Scalar | Record | list[Scalar] | list[Record]
RawValue: TypeAlias = int | float | str | bool | None


def _runtime_family(value: Any) -> str:
    if type(value) is int or isinstance(value, Decimal):
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("Runtime numbers must be finite")
        return "number"
    if type(value) in (bool, str, date, type(None)):
        return type(value).__name__
    raise ValueError("Runtime scalars must be int/Decimal/bool/str/date/null")


def validate_runtime_value(value: Any) -> None:
    """Enforce bounded-depth runtime collections and artifact tags.

    Flat scalar collections may mix scalar families so a plan can return
    tuples such as ``[label, value]`` without inventing a record type.
    """
    def record(item: dict) -> None:
        if "$type" in item or any(not isinstance(k, str) or not k for k in item):
            raise ValueError("Record fields must be nonempty strings; $type is reserved")
        for child in item.values():
            _runtime_family(child)

    if isinstance(value, dict):
        record(value)
    elif isinstance(value, list):
        if not value:
            return
        if all(isinstance(v, dict) for v in value):
            for item in value:
                record(item)
                if set(item) != set(value[0]):
                    raise ValueError("Record collections must have consistent fields")
        else:
            for item in value:
                if isinstance(item, (dict, list)):
                    raise ValueError("Scalar collections cannot contain records or nested collections")
                _runtime_family(item)
    else:
        _runtime_family(value)


def _runtime_from_wire(value: Any) -> RuntimeValue:
    def decode(item: Any) -> Any:
        if isinstance(item, dict):
            if "$type" in item:
                if item.get("$type") == "decimal":
                    return _decode(Decimal, item)
                if item.get("$type") == "date":
                    return _decode(date, item)
                raise ValueError("Unknown runtime artifact tag")
            return {k: decode(v) for k, v in item.items()}
        if isinstance(item, list):
            return [decode(v) for v in item]
        return item
    result = decode(value)
    validate_runtime_value(result)
    return result


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite Decimal is not a contract value")
        return {"$type": "decimal", "value": str(value)}
    if type(value) is date:
        return {"$type": "date", "value": value.isoformat()}
    if is_dataclass(value):
        if isinstance(value, StepResult):
            validate_runtime_value(value.value)
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite float is not a JSON value")
    return value


def _decode(annotation: Any, value: Any) -> Any:
    """Strict DTO reconstruction, including tagged runtime Decimal/date values."""
    if annotation == RuntimeValue:
        return _runtime_from_wire(value)
    if annotation is Any:
        return value
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        for alternative in args:
            try:
                return _decode(alternative, value)
            except (ValueError, TypeError):
                pass
        raise ValueError(f"Value does not match {annotation}")
    if origin is Literal:
        if not any(type(value) is type(a) and value == a for a in args):
            raise ValueError(f"Expected one of {args}")
        return value
    if origin is list:
        if not isinstance(value, list):
            raise ValueError("Expected array")
        return [_decode(args[0], v) for v in value]
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("Expected object")
        return {_decode(args[0], k): _decode(args[1], v) for k, v in value.items()}
    if isinstance(annotation, type) and is_dataclass(annotation):
        return annotation.from_dict(value)
    if annotation is Decimal:
        if not isinstance(value, dict) or set(value) != {"$type", "value"} or value["$type"] != "decimal" or not isinstance(value["value"], str):
            raise ValueError("Expected tagged Decimal")
        try:
            result = Decimal(value["value"])
        except InvalidOperation as exc:
            raise ValueError("Invalid Decimal artifact") from exc
        if not result.is_finite():
            raise ValueError("Expected finite Decimal")
        return result
    if annotation is date:
        if not isinstance(value, dict) or set(value) != {"$type", "value"} or value["$type"] != "date":
            raise ValueError("Expected tagged date")
        return date.fromisoformat(value["value"])
    if annotation is type(None):
        if value is not None:
            raise ValueError("Expected null")
    elif annotation is float and type(value) in (int, float):
        if not math.isfinite(value):
            raise ValueError("Expected finite float")
        return float(value)
    elif type(value) is not annotation:
        raise ValueError(f"Expected {annotation}, got {type(value)}")
    return value


class DTO:
    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        if not isinstance(data, dict):
            raise ValueError(f"{cls.__name__} requires an object")
        names = {f.name for f in fields(cls)}
        if extra := set(data) - names:
            raise ValueError(f"Unknown {cls.__name__} fields: {sorted(extra)}")
        hints = get_type_hints(cls)
        return cls(**{key: _decode(hints[key], value) for key, value in data.items()})


@dataclass(kw_only=True)
class Query(DTO):
    id: str
    file_name: str
    query: str


@dataclass(kw_only=True)
class CellRef(DTO):
    sheet: str
    cell: str


@dataclass(kw_only=True)
class RawCell(DTO):
    sheet: str
    cell: str
    row_index: int
    column_index: int
    value_raw: RawValue
    value_type: ValueType
    display_value: str
    unit: str | None
    number_format: str | None = None
    formula: str | None = None
    date_iso: str | None = None
    merged_anchor: str | None = None


@dataclass(kw_only=True)
class CellFact(RawCell):
    section_path: list[str]
    row_header_path: list[str]
    column_header_path: list[str]


@dataclass(kw_only=True)
class RawRow(DTO):
    row_index: int
    cells: list[RawCell]


@dataclass(kw_only=True)
class Section(DTO):
    path: list[str]
    label_cell: str
    row_start: int
    row_end: int


@dataclass(kw_only=True)
class SheetContext(DTO):
    name: str
    visibility: Literal["visible", "hidden", "veryHidden"]
    dimensions: str
    raw_rows: list[RawRow] | None
    parsed_cells: list[CellFact] | None
    header_rows: list[int]
    sections: list[Section]
    merged_ranges: list[str]


@dataclass(kw_only=True)
class TableContext(DTO):
    version: Literal["table_context_v1"]
    table_id: str
    source_hash: str
    sheets: list[SheetContext]
    metadata: dict[str, Any]


@dataclass(kw_only=True)
class ContextMeta(DTO):
    sheets_included: list[str]
    included_ranges: dict[str, list[str]]
    table_view_hash: str
    coverage: Literal["full"] = "full"
    truncated: Literal[False] = False


@dataclass(kw_only=True)
class TableView(DTO):
    text: str
    table_view_hash: str
    context_meta: ContextMeta


@dataclass(kw_only=True)
class AnswerSpec(DTO):
    kind: AnswerKind
    decimals: int | None
    separator: str | None
    allowed_values: list[str] | None
    item_count: int | None = None
    rounding: Literal["ROUND_HALF_UP"] = "ROUND_HALF_UP"


@dataclass(kw_only=True)
class QueryContext(DTO):
    query_id: str
    table_id: str
    query: str
    input_mode: InputMode
    table_view: str
    answer_spec: AnswerSpec
    context_meta: ContextMeta


@dataclass(kw_only=True)
class PipelineError(DTO, Exception):
    code: ErrorCode
    stage: Stage
    message: str
    query_id: str | None = None
    step_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code} ({self.stage}): {self.message}"


@dataclass(kw_only=True)
class StepResult(DTO):
    value: RuntimeValue
    evidence: list[CellRef]


@dataclass(kw_only=True)
class StepTrace(DTO):
    step: str
    op: Operator
    args: dict[str, Any]
    resolved_args: dict[str, Any]
    input_refs: list[str]
    value_preview: str
    evidence: list[CellRef]
    elapsed_ms: float
    status: Literal["ok", "error"]
    error: PipelineError | None
    warnings: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class ExecutionResult(StepResult):
    trace: list[StepTrace]


@dataclass(kw_only=True)
class TokenUsage(DTO):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    thinking_tokens: int | None = None
    cached_prompt_tokens: int | None = None


@dataclass(kw_only=True)
class LLMRequest(DTO):
    system_prompt: str
    user_prompt: str
    model: str
    json_mode: Literal["json_object", "json_schema", "prompt_only"]
    response_schema: dict[str, Any] | None = None
    max_output_tokens: int = 2400
    timeout_seconds: float | None = None
    provider_options: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class LLMResponse(DTO):
    text: str
    provider: Provider
    model_requested: str
    model_returned: str | None
    usage: TokenUsage
    llm_elapsed_ms: float
    actual_cost_usd: float | None = None
    provider_request_id: str | None = None
    finish_reason: str | None = None
    raw_usage: dict[str, Any] | None = None


@dataclass(kw_only=True)
class QueryResult(DTO):
    query_id: str
    table_id: str
    status: Literal["ok", "error"]
    answer: str | None
    format_valid: bool
    exact_match: bool | None
    error: PipelineError | None
    trace_path: str | None
    llm_calls: int
    planner_repair_calls: int
    usage: TokenUsage = field(default_factory=TokenUsage)
    actual_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    llm_elapsed_ms: float | None = None
    total_elapsed_ms: float | None = None


@dataclass(kw_only=True)
class RuntimeLimits(DTO):
    max_steps: int = 20
    max_selected_cells: int = 100000
    max_records: int = 10000
    max_api_attempts: int = 3
    max_planner_repair_calls: Literal[0, 1] = 1
    request_timeout_seconds: int = 90
    query_timeout_seconds: int = 240
    run_timeout_seconds: int = 2400


class PlanStep(TypedDict):
    id: str
    op: Operator
    args: dict[str, Any]


class PlanReturnFormat(TypedDict, total=False):
    separator: str
    decimal_places: int


class PlanReturn(TypedDict, total=False):
    ref: str
    format: PlanReturnFormat


# `return` is a Python keyword, so use the functional TypedDict syntax.
Plan = TypedDict("Plan", {
    "version": Literal["plan_v1"], "query_id": str, "table_id": str,
    "steps": list[PlanStep], "return": PlanReturn,
})
ValidatedPlan = NewType("ValidatedPlan", Plan)
