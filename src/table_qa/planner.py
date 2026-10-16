"""Prompt construction and response parsing for the plan_tools flow."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from .contracts import LLMRequest, PipelineError, QueryContext, ValidatedPlan
from .contract_validation import load_schema, validate_plan
from .json_utils import strict_json_loads


PROMPT_VERSION = "planner_v1"
PROMPT_PATH = Path(__file__).with_name("prompts") / f"{PROMPT_VERSION}.txt"
OPERATOR_GUIDE = """\
- lookup_cell: read one cell; args {sheet, cell}.
- select_records: read a row range into records; args {sheet, row_range, fields}; each field maps to a column selector and optional as_type.
- collect: build a flat scalar list or a consistent record list from literals or whole-step references; args {items}. A final scalar list may mix labels and numbers. Never use paths like $s1.name; project fields first.
- derive: add a calculated field to every record; args {source, field, fn, inputs}. Use date_diff_days for exact age ranking.
- add/subtract/abs_diff/divide: scalar arithmetic; args {left, right}.
- aggregate: reduce a scalar list or record field; args {source, fn, optional field}.
- sort: order records; args {source, by, order}.
- take: select records by zero-based offset and count; args {source, offset, count}.
- project: extract one field; args {source, field, single}.
- compare: compare scalar operands; args {left, right, predicate}.
- filter: retain matching records; args {source, predicates}.
- argmax/argmin: select extreme record(s); args {source, by, optional ties}.

REFERENCE EXAMPLES:
1. Exact age: select birth/admission -> derive date_diff_days -> sort -> take -> project.
2. Label plus value: project each scalar into a step, then collect {items: ["$s2", "$s3"]} in the exact answer order.
3. Subtotals: select either a parent total or its non-overlapping children, never both.
4. Multi-row headers: match the complete header path and requested endpoint periods before choosing a column.
"""
REPAIRABLE_ERRORS = {
    "INVALID_PLAN_JSON", "INVALID_PLAN_SCHEMA", "UNKNOWN_OPERATOR", "UNSUPPORTED_SELECTOR",
    "CELL_NOT_FOUND", "AMBIGUOUS_CELL", "MISSING_VALUE", "TYPE_ERROR", "DIVISION_BY_ZERO",
    "EMPTY_SELECTION", "TIE_DETECTED", "FORMAT_INVALID",
}
DIRECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": ["string", "null"]}},
    "required": ["answer"],
    "additionalProperties": False,
}


def planner_system_prompt(prompt_variant: str = "A") -> str:
    if prompt_variant not in {"A", "B"}:
        raise ValueError("prompt_variant must be A or B")
    schema = load_schema("plan_v1")
    prompt_path = PROMPT_PATH if prompt_variant == "A" else PROMPT_PATH.with_name("planner_v1_b.txt")
    return prompt_path.read_text(encoding="utf-8").format(
        operator_catalog="lookup_cell, select_records, collect, derive, add, subtract, abs_diff, divide, aggregate, sort, take, project, compare, filter, argmax, argmin",
        operator_guide=OPERATOR_GUIDE,
        minified_json_schema=json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
    )


def _base_user_prompt(context: QueryContext) -> str:
    return "\n".join((
        f"QUERY_ID: {context.query_id}", f"TABLE_ID: {context.table_id}", "ANSWER_MODE=plan_tools",
        "QUESTION_JSON: " + json.dumps(context.query, ensure_ascii=False),
        "OUTPUT_CONTRACT: Follow the explicit order, precision, separator, and units requested by QUESTION_JSON. Declare only separator/decimal_places in return.format when needed.",
        f"INPUT_MODE: {context.input_mode}", "TABLE_CONTEXT:", context.table_view,
    ))


def build_plan_request(context: QueryContext, *, model: str = "mock", repair_plan: Any = None,
                       repair_error: PipelineError | None = None, json_mode: str = "json_object",
                       max_output_tokens: int = 2400,
                       provider_options: dict[str, Any] | None = None,
                       prompt_variant: str = "A") -> LLMRequest:
    user_prompt = _base_user_prompt(context)
    if repair_plan is not None and repair_error is not None:
        error_json = json.dumps(repair_error.to_dict(), ensure_ascii=False, default=str)
        plan_json = json.dumps(repair_plan, ensure_ascii=False, default=str)
        user_prompt += (
            "\n\nYour previous plan failed local validation, execution, or final rendering. Revise the plan once. "
            "Use the same question and full table above. Preserve correct earlier work when possible. "
            "For a final-shape failure, fix project/collect/return.format. Do not answer directly."
            "\nFAILED_PLAN_JSON:\n" + plan_json + "\nOBSERVED_ERROR_JSON:\n" + error_json
        )
    return LLMRequest(system_prompt=planner_system_prompt(prompt_variant), user_prompt=user_prompt, model=model,
                      json_mode=json_mode, response_schema=load_schema("plan_v1"),
                      max_output_tokens=max_output_tokens, provider_options=provider_options or {})


def parse_plan_response(response_text: str, context: QueryContext) -> ValidatedPlan:
    try:
        plan = strict_json_loads(response_text, parse_float=Decimal)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PipelineError(code="INVALID_PLAN_JSON", stage="plan_validate",
                            message=f"Planner response is not strict JSON: {exc}", query_id=context.query_id) from exc
    return validate_plan_data(plan, context)


def validate_plan_data(plan: Any, context: QueryContext) -> ValidatedPlan:
    if not isinstance(plan, dict):
        raise PipelineError(code="INVALID_PLAN_SCHEMA", stage="plan_validate",
                            message="Planner response must be a JSON object", query_id=context.query_id)
    try:
        return validate_plan(plan, context)
    except PipelineError as exc:
        exc.query_id = exc.query_id or context.query_id
        raise
