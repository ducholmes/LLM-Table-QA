"""Plan compiler and bounded executor for the plan_v1 DSL."""

from __future__ import annotations

import json
from decimal import InvalidOperation
import re
import time
from typing import Any, Callable

from ..contracts import ExecutionResult, PipelineError, RuntimeLimits, StepResult, StepTrace, TableContext, validate_runtime_value
from ..contract_validation import validate_runtime_limits, validate_schema
from .registry import ExecutionBudget, REGISTRY
from .resolver import StateValue, artifact_value, resolve_arguments, union_evidence


StepResultSink = Callable[[str, StepResult], None]


def _preview(value: Any, max_chars: int = 1200) -> str:
    encoded = json.dumps(artifact_value(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return encoded if len(encoded) <= max_chars else encoded[:max_chars] + "…"


def _attach_identity(error: PipelineError, query_id: str, step_id: str | None = None) -> None:
    if error.query_id is None:
        error.query_id = query_id
    if step_id is not None and error.step_id is None:
        error.step_id = step_id


def _serialize_trace(trace: list[StepTrace]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in trace]


def _references(value: Any):
    if isinstance(value, str) and re.fullmatch(r"\$s[1-9][0-9]*", value):
        yield value[1:]
    elif isinstance(value, dict):
        if set(value) != {"literal"}:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def execute_plan(
    plan: dict[str, Any],
    table: TableContext,
    *,
    limits: RuntimeLimits | None = None,
    step_result_sink: StepResultSink | None = None,
) -> ExecutionResult:
    """Execute an already query-validated plan without filesystem or code access.

    The runner validates plan/query identity before calling this function. This
    boundary repeats local schema, table identity, dependency and budget checks
    so direct callers cannot dispatch an arbitrary operator or file reference.
    """
    runtime_limits = validate_runtime_limits(limits or RuntimeLimits())
    if isinstance(plan, dict) and isinstance(plan.get("steps"), list):
        for step in plan["steps"]:
            if isinstance(step, dict) and isinstance(step.get("op"), str) and step["op"] not in REGISTRY:
                raise PipelineError(code="UNKNOWN_OPERATOR", stage="plan_validate", message=f"Unknown operator: {step['op']}", query_id=plan.get("query_id"))
    validate_schema("plan_v1", plan, code="INVALID_PLAN_SCHEMA", stage="plan_validate")
    if plan["table_id"] != table.table_id:
        raise PipelineError(code="INVALID_PLAN_SCHEMA", stage="plan_validate", message="Plan table_id does not match the loaded table", query_id=plan["query_id"])
    if len(plan["steps"]) > runtime_limits.max_steps:
        raise PipelineError(code="RESOURCE_LIMIT", stage="execute", message="Plan exceeds max_steps", query_id=plan["query_id"],
                            details={"steps": len(plan["steps"]), "limit": runtime_limits.max_steps})

    seen: set[str] = set()
    for step in plan["steps"]:
        if step["id"] in seen or any(reference not in seen for reference in _references(step["args"])):
            raise PipelineError(code="INVALID_PLAN_SCHEMA", stage="plan_validate", message="Duplicate step ID or self/forward/missing reference",
                                query_id=plan["query_id"], step_id=step["id"])
        seen.add(step["id"])
    if plan["return"]["ref"][1:] not in seen:
        raise PipelineError(code="INVALID_PLAN_SCHEMA", stage="plan_validate", message="Return reference does not identify a plan step", query_id=plan["query_id"])

    query_id = plan["query_id"]
    state: dict[str, StateValue] = {}
    traces: list[StepTrace] = []
    budget = ExecutionBudget(runtime_limits)

    for step in plan["steps"]:
        step_id = step["id"]
        started = time.perf_counter()
        resolved_args: dict[str, Any] = {}
        input_refs: list[str] = []
        input_evidence = []
        try:
            resolved_args, tracks, input_refs, input_evidence = resolve_arguments(
                step["args"], state, query_id=query_id, step_id=step_id
            )
            outcome = REGISTRY[step["op"]](resolved_args, tracks, table, budget, query_id, step_id)
            evidence = union_evidence(table, input_evidence, outcome.evidence)
            try:
                validate_runtime_value(outcome.value)
            except ValueError as exc:
                raise PipelineError(code="TYPE_ERROR", stage="execute", message=str(exc), query_id=query_id, step_id=step_id) from exc
            result = StepResult(value=outcome.value, evidence=evidence)
            state[step_id] = StateValue(result=result, origins=outcome.origins, units=outcome.units)
            trace_item = StepTrace(
                step=step_id, op=step["op"], args=step["args"], resolved_args=resolved_args,
                input_refs=input_refs, value_preview=_preview(result.value), evidence=evidence,
                elapsed_ms=(time.perf_counter() - started) * 1000, status="ok", error=None,
                warnings=outcome.warnings,
            )
            if step_result_sink is not None:
                step_result_sink(step_id, result)
            traces.append(trace_item)
        except PipelineError as exc:
            _attach_identity(exc, query_id, step_id)
            trace_item = StepTrace(
                step=step_id, op=step["op"], args=step["args"], resolved_args=resolved_args,
                input_refs=input_refs, value_preview="", evidence=union_evidence(table, input_evidence),
                elapsed_ms=(time.perf_counter() - started) * 1000, status="error", error=exc,
                warnings=[],
            )
            traces.append(trace_item)
            exc.details = {**exc.details, "trace": _serialize_trace(traces)}
            raise
        except (ArithmeticError, InvalidOperation, OSError, TypeError, ValueError, KeyError, IndexError) as exc:
            error = PipelineError(code="EXECUTION_ERROR", stage="execute", message=str(exc), query_id=query_id, step_id=step_id)
            trace_item = StepTrace(
                step=step_id, op=step["op"], args=step["args"], resolved_args=resolved_args,
                input_refs=input_refs, value_preview="", evidence=union_evidence(table, input_evidence),
                elapsed_ms=(time.perf_counter() - started) * 1000, status="error", error=error,
                warnings=[],
            )
            traces.append(trace_item)
            error.details = {"trace": _serialize_trace(traces)}
            raise error from exc
        except Exception as exc:
            error = PipelineError(code="EXECUTION_ERROR", stage="execute", message=f"Unexpected executor failure: {type(exc).__name__}: {exc}", query_id=query_id, step_id=step_id)
            trace_item = StepTrace(
                step=step_id, op=step["op"], args=step["args"], resolved_args=resolved_args,
                input_refs=input_refs, value_preview="", evidence=union_evidence(table, input_evidence),
                elapsed_ms=(time.perf_counter() - started) * 1000, status="error", error=error,
                warnings=[],
            )
            traces.append(trace_item)
            error.details = {"trace": _serialize_trace(traces)}
            raise error from exc

    final_id = plan["return"]["ref"][1:]
    final = state.get(final_id)
    if final is None:
        raise PipelineError(code="INVALID_PLAN_SCHEMA", stage="plan_validate", message="Return reference does not identify an executed step", query_id=query_id,
                            details={"return_ref": plan["return"]["ref"]})
    return ExecutionResult(value=final.result.value, evidence=final.result.evidence, trace=traces)
