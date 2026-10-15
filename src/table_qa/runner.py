"""Offline orchestration with prompt, answer validation, repair, and CSV output."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
import time
from typing import Any
import uuid

from .adapters.parsed import ParsedTableAdapter
from .adapters.raw import RawWorkbookAdapter
from .answer_spec import format_answer
from .context_builder import build_query_context
from .config import LLMConfig
from .contracts import LLMResponse, PipelineError, QueryContext, RuntimeLimits, StepResult, TableContext, TokenUsage
from .contract_validation import validate_runtime_limits, validate_table_context
from .direct import build_direct_request, parse_direct_response
from .execution import execute_plan
from .execution.resolver import artifact_value
from .interfaces import LLMClient
from .budget import RunBudget
from .json_utils import strict_json_loads
from .mock_llm import MockLLMClient
from .llm.telemetry import MeasuredClient
from .logging import append_jsonl
from .planner import REPAIRABLE_ERRORS, build_plan_request, validate_plan_data
from .query_loader import load_queries


_RETRYABLE_API_ERRORS = frozenset({"API_SERVER_ERROR", "API_TIMEOUT", "API_RATE_LIMIT",
                                   "EMPTY_RESPONSE"})
_SERVER_RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 4.0)
_RATE_LIMIT_BACKOFF_SECONDS = (10.0, 30.0, 60.0, 120.0)


def _retry_backoff_seconds(error: PipelineError, http_attempt: int) -> float:
    provider_delay = error.details.get("retry_after_seconds")
    if type(provider_delay) in (int, float) and provider_delay >= 0:
        return float(provider_delay)
    schedule = (_RATE_LIMIT_BACKOFF_SECONDS if error.code == "API_RATE_LIMIT"
                else _SERVER_RETRY_BACKOFF_SECONDS)
    return schedule[min(http_attempt - 1, len(schedule) - 1)]


def _json_default(value: Any) -> Any:
    if isinstance(value, (Decimal, date)):
        return artifact_value(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _log(path: Path, event: dict[str, Any], run_id: str) -> None:
    append_jsonl(path, event, run_id, default=_json_default)


def _sum_known(values: list[int | float | None]) -> int | float | None:
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _sum_reported(values: list[int | float | None]) -> int | float | None:
    reported = [value for value in values if value is not None]
    return sum(reported) if reported else None


def _deadline_error(*, started_run: float, started_query: float | None,
                    limits: RuntimeLimits, query_id: str | None = None,
                    stage: str = "execute") -> PipelineError | None:
    now = time.perf_counter()
    run_elapsed = now - started_run
    if run_elapsed >= limits.run_timeout_seconds:
        return PipelineError(code="RUN_TIMEOUT", stage=stage,
                             message=f"Run exceeded {limits.run_timeout_seconds} seconds",
                             query_id=query_id,
                             details={"elapsed_seconds": run_elapsed,
                                      "timeout_seconds": limits.run_timeout_seconds})
    if started_query is not None:
        query_elapsed = now - started_query
        if query_elapsed >= limits.query_timeout_seconds:
            return PipelineError(code="QUERY_TIMEOUT", stage=stage,
                                 message=f"Query exceeded {limits.query_timeout_seconds} seconds",
                                 query_id=query_id,
                                 details={"elapsed_seconds": query_elapsed,
                                          "timeout_seconds": limits.query_timeout_seconds})
    return None


def _check_deadline(*, started_run: float, started_query: float | None,
                    limits: RuntimeLimits, query_id: str | None = None,
                    stage: str = "execute") -> None:
    error = _deadline_error(started_run=started_run, started_query=started_query,
                            limits=limits, query_id=query_id, stage=stage)
    if error is not None:
        raise error


def _remaining_request_timeout(*, started_run: float, started_query: float,
                               limits: RuntimeLimits, query_id: str) -> float:
    _check_deadline(started_run=started_run, started_query=started_query,
                    limits=limits, query_id=query_id, stage="llm")
    now = time.perf_counter()
    return min(float(limits.request_timeout_seconds),
               started_run + limits.run_timeout_seconds - now,
               started_query + limits.query_timeout_seconds - now)


def _check_run_deadline(started_run: float, limits: RuntimeLimits) -> None:
    error = _deadline_error(started_run=started_run, started_query=None,
                            limits=limits, stage="collect")
    if error is not None:
        raise error


def peak_rss_mb() -> float | None:
    """Return this process' high-water RSS in MiB where the platform exposes it."""
    try:
        import resource

        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # getrusage reports bytes on macOS and KiB on Linux and the BSDs.
        return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024
    except (ImportError, AttributeError, OSError, ValueError):
        try:
            with Path("/proc/self/status").open(encoding="ascii") as stream:
                for line in stream:
                    if line.startswith("VmHWM:"):
                        return float(line.split()[1]) / 1024
        except (OSError, ValueError, IndexError):
            pass
    return None


def _check_memory_limit(limit_mb: int | None, *, query_id: str | None, stage: str) -> None:
    if limit_mb is None:
        return
    measured = peak_rss_mb()
    if measured is None:
        raise PipelineError(code="RESOURCE_LIMIT", stage=stage,
                            message="Could not measure process peak RSS",
                            query_id=query_id, details={"memory_limit_mb": limit_mb})
    if measured >= limit_mb:
        raise PipelineError(code="RESOURCE_LIMIT", stage=stage,
                            message=f"Process peak RSS reached the {limit_mb} MiB limit",
                            query_id=query_id,
                            details={"peak_rss_mb": round(measured, 2), "memory_limit_mb": limit_mb})


def _aggregate_usage(responses: list[LLMResponse], *, partial: bool = False) -> TokenUsage:
    fields = ("prompt_tokens", "completion_tokens", "total_tokens", "thinking_tokens", "cached_prompt_tokens")
    sum_values = _sum_reported if partial else _sum_known
    values = {name: sum_values([getattr(response.usage, name) for response in responses]) for name in fields}
    return TokenUsage(**values)


def _load_labels(labels_path: Path | None, query_ids: set[str]) -> dict[str, str] | None:
    if labels_path is None:
        return None
    try:
        with labels_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ["id", "answer"]:
                raise ValueError("Labels CSV must have exactly the columns id,answer")
            labels: dict[str, str] = {}
            for row in reader:
                query_id, answer = row.get("id"), row.get("answer")
                if not query_id or answer is None or query_id in labels:
                    raise ValueError("Labels CSV contains an empty field or duplicate query ID")
                if query_id not in query_ids:
                    raise ValueError(f"Label query ID is not present in this run: {query_id}")
                labels[query_id] = answer
            return labels
    except (OSError, csv.Error) as exc:
        raise PipelineError(code="INVALID_QUERY", stage="load", message=f"Could not read labels CSV: {exc}") from exc
    except ValueError as exc:
        raise PipelineError(code="INVALID_QUERY", stage="load", message=str(exc)) from exc


def _write_submission(path: Path, rows: list[dict[str, Any]], *, before_commit=None) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["id", "answer"], lineterminator="\r\n", extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow({"id": row["query_id"], "answer": row["answer"]})
    content = buffer.getvalue()
    round_trip = list(csv.DictReader(io.StringIO(content, newline="")))
    expected = [{"id": row["query_id"], "answer": row["answer"]} for row in rows]
    if round_trip != expected:
        raise PipelineError(code="FORMAT_INVALID", stage="collect", message="Submission CSV failed its round-trip check")
    temporary_path = path.with_name(path.name + ".tmp")
    try:
        temporary_path.write_text(content, encoding="utf-8-sig", newline="")
        if before_commit is not None:
            before_commit()
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _response_event(response: LLMResponse, query_id: str, call_index: int, call_purpose: str,
                    answer_mode: str, prompt_version: str, prompt_hash: str,
                    http_attempt: int) -> dict[str, Any]:
    return {
        "query_id": query_id, "event": "llm_response", "call_index": call_index,
        "call_purpose": call_purpose, "http_attempt": http_attempt, "provider": response.provider,
        "model_requested": response.model_requested, "model_returned": response.model_returned,
        "provider_request_id": response.provider_request_id, "finish_reason": response.finish_reason,
        "usage": response.usage.to_dict(), "actual_cost_usd": response.actual_cost_usd,
        "llm_elapsed_ms": response.llm_elapsed_ms, "answer_mode": answer_mode,
        "prompt_version": prompt_version, "prompt_hash": prompt_hash,
        "status": "ok", "text": response.text,
    }


def run_offline(*, queries_path: Path, data_dir: Path, input_mode: str = "raw", answer_mode: str = "plan_tools",
                parsed_dir: Path | None = None, run_root: Path = Path("runs"), client: LLMClient | None = None,
                limits: RuntimeLimits | None = None, labels_path: Path | None = None,
                llm_config: LLMConfig | None = None, run_profile: str = "development",
                prompt_variant: str = "A", max_rss_mb: int | None = None,
                expected_query_count: int | None = None,
                expected_workbook_count: int | None = None) -> Path:
    if input_mode not in ("raw", "parsed") or answer_mode not in ("plan_tools", "direct"):
        raise PipelineError(code="INVALID_QUERY", stage="load", message="Unsupported input or answer mode")
    if prompt_variant not in {"A", "B"} or (answer_mode == "direct" and prompt_variant != "A"):
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message="Prompt variant must be A or B; direct mode currently supports only A")
    if input_mode == "parsed" and parsed_dir is None:
        raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="load", message="--parsed-dir is required for parsed input")
    settings = llm_config or LLMConfig()
    runtime_limits = validate_runtime_limits(limits or RuntimeLimits(
        request_timeout_seconds=settings.request_timeout_seconds,
        max_api_attempts=settings.max_api_attempts,
        max_planner_repair_calls=settings.max_planner_repair_calls,
    ))
    if max_rss_mb is not None and (type(max_rss_mb) is not int or max_rss_mb < 1):
        raise PipelineError(code="INVALID_QUERY", stage="load", message="max_rss_mb must be a positive integer")
    if (expected_query_count is not None
            and (type(expected_query_count) is not int or expected_query_count < 1)):
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message="expected_query_count must be a positive integer when set")
    if (expected_workbook_count is not None
            and (type(expected_workbook_count) is not int or expected_workbook_count < 1)):
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message="expected_workbook_count must be a positive integer when set")
    started_run = time.perf_counter()
    work = load_queries(queries_path, data_dir)
    if not work:
        raise PipelineError(code="INVALID_QUERY", stage="load", message="Query input is empty")
    if expected_query_count is not None and len(work) != expected_query_count:
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message=f"Expected {expected_query_count} queries, found {len(work)}",
                            details={"expected_query_count": expected_query_count,
                                     "actual_query_count": len(work)})
    workbook_count = len({path for _, path in work})
    if expected_workbook_count is not None and workbook_count != expected_workbook_count:
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message=f"Expected {expected_workbook_count} workbooks, found {workbook_count}",
                            details={"expected_workbook_count": expected_workbook_count,
                                     "actual_workbook_count": workbook_count})
    labels = _load_labels(labels_path, {query.id for query, _ in work})
    backend: LLMClient = MeasuredClient(client or MockLLMClient())
    raw_adapter, parsed_adapter = RawWorkbookAdapter(), ParsedTableAdapter()
    table_cache: dict[Path, TableContext] = {}
    view_cache: dict[Path, Any] = {}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "Z_" + uuid.uuid4().hex[:8]
    run_dir = Path(run_root) / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    logs_path, results_path = run_dir / "logs.jsonl", run_dir / "results.jsonl"
    executions_path, step_results_path = run_dir / "executions.jsonl", run_dir / "step_results.jsonl"
    outcomes: list[dict[str, Any]] = []
    all_responses: list[LLMResponse] = []
    all_call_elapsed_ms: list[float] = []
    budget = RunBudget()
    errors: list[dict[str, str]] = []
    api_retries = 0

    for query, workbook_path in work:
        started_query = time.perf_counter()
        context: QueryContext | None = None
        responses: list[LLMResponse] = []
        call_elapsed_ms: list[float] = []
        calls = 0
        repair_calls = 0
        unknown_calls = 0
        prompt_version = (("planner_v1" if prompt_variant == "A" else "planner_v1_b")
                          if answer_mode == "plan_tools" else "direct_v1")
        outcome: dict[str, Any]

        def generate(request, *, purpose: str) -> LLMResponse:
            nonlocal calls, repair_calls, unknown_calls, api_retries
            calls += 1
            if purpose == "plan_repair":
                repair_calls += 1
            prompt_hash = sha256((request.system_prompt + "\0" + request.user_prompt).encode("utf-8")).hexdigest()
            output_token_limit = request.max_output_tokens
            for http_attempt in range(1, runtime_limits.max_api_attempts + 1):
                timeout_seconds = _remaining_request_timeout(
                    started_run=started_run, started_query=started_query,
                    limits=runtime_limits, query_id=query.id)
                attempt_request = replace(request, timeout_seconds=timeout_seconds,
                                          max_output_tokens=output_token_limit)
                _log(logs_path, {"query_id": query.id, "event": "llm_call_started", "call_index": calls,
                                 "call_purpose": purpose, "http_attempt": http_attempt,
                                 "provider": settings.provider, "model_requested": settings.model,
                                 "answer_mode": answer_mode, "prompt_version": prompt_version,
                                 "prompt_hash": prompt_hash, "request_timeout_seconds": timeout_seconds,
                                 "table_view_hash": context.context_meta.table_view_hash if context else None}, stamp)
                call_started = time.perf_counter()
                try:
                    response = backend.generate(attempt_request)
                except PipelineError as exc:
                    elapsed_ms = (time.perf_counter() - call_started) * 1000
                    usage_unknown = exc.details.get("usage_unknown", True) is not False
                    if usage_unknown:
                        unknown_calls += 1
                        budget.record_unknown_call()
                    measured_ms = exc.details.get("llm_elapsed_ms", elapsed_ms)
                    call_elapsed_ms.append(measured_ms)
                    all_call_elapsed_ms.append(measured_ms)
                    error = _deadline_error(started_run=started_run, started_query=started_query,
                                            limits=runtime_limits, query_id=query.id, stage="llm") or exc
                    error.details.setdefault("llm_elapsed_ms", measured_ms)
                    can_retry = (settings.provider != "mock"
                                 and error.code in _RETRYABLE_API_ERRORS
                                 and http_attempt < runtime_limits.max_api_attempts)
                    backoff_seconds = _retry_backoff_seconds(error, http_attempt)
                    if can_retry:
                        remaining = _remaining_request_timeout(
                            started_run=started_run, started_query=started_query,
                            limits=runtime_limits, query_id=query.id)
                        can_retry = remaining > backoff_seconds
                    _log(logs_path, {"query_id": query.id, "event": "llm_call_error", "call_index": calls,
                                     "call_purpose": purpose, "http_attempt": http_attempt,
                                     "provider": settings.provider, "model_requested": settings.model,
                                     "status": "error", "usage_unknown": usage_unknown,
                                     "llm_elapsed_ms": measured_ms, "will_retry": can_retry,
                                     "error": error.to_dict()}, stamp)
                    if not can_retry:
                        if error is exc:
                            raise
                        raise error from exc
                    if (error.code == "EMPTY_RESPONSE"
                            and str(error.details.get("finish_reason") or "").lower()
                            in {"length", "max_tokens", "max_output_tokens"}):
                        output_token_limit = max(output_token_limit * 2, output_token_limit + 512)
                    api_retries += 1
                    _log(logs_path, {"query_id": query.id, "event": "llm_retry_scheduled",
                                     "call_index": calls, "call_purpose": purpose,
                                     "failed_http_attempt": http_attempt,
                                     "next_http_attempt": http_attempt + 1,
                                     "backoff_seconds": backoff_seconds,
                                     "next_max_output_tokens": output_token_limit,
                                     "error_code": error.code}, stamp)
                    time.sleep(backoff_seconds)
                    continue
                except Exception as exc:
                    elapsed_ms = (time.perf_counter() - call_started) * 1000
                    unknown_calls += 1
                    budget.record_unknown_call()
                    call_elapsed_ms.append(elapsed_ms)
                    all_call_elapsed_ms.append(elapsed_ms)
                    error = _deadline_error(started_run=started_run, started_query=started_query,
                                            limits=runtime_limits, query_id=query.id, stage="llm")
                    if error is None:
                        error = PipelineError(code="API_SERVER_ERROR", stage="llm",
                                              message=f"LLM client raised {type(exc).__name__}",
                                              query_id=query.id)
                    can_retry = (settings.provider != "mock"
                                 and error.code in _RETRYABLE_API_ERRORS
                                 and http_attempt < runtime_limits.max_api_attempts)
                    backoff_seconds = _retry_backoff_seconds(error, http_attempt)
                    if can_retry:
                        remaining = _remaining_request_timeout(
                            started_run=started_run, started_query=started_query,
                            limits=runtime_limits, query_id=query.id)
                        can_retry = remaining > backoff_seconds
                    _log(logs_path, {"query_id": query.id, "event": "llm_call_error", "call_index": calls,
                                     "call_purpose": purpose, "http_attempt": http_attempt,
                                     "provider": settings.provider, "model_requested": settings.model,
                                     "status": "error", "usage_unknown": True,
                                     "llm_elapsed_ms": elapsed_ms, "will_retry": can_retry,
                                     "error": error.to_dict()}, stamp)
                    if not can_retry:
                        raise error from exc
                    api_retries += 1
                    _log(logs_path, {"query_id": query.id, "event": "llm_retry_scheduled",
                                     "call_index": calls, "call_purpose": purpose,
                                     "failed_http_attempt": http_attempt,
                                     "next_http_attempt": http_attempt + 1,
                                     "backoff_seconds": backoff_seconds,
                                     "error_code": error.code}, stamp)
                    time.sleep(backoff_seconds)
                    continue
                call_elapsed_ms.append(response.llm_elapsed_ms)
                all_call_elapsed_ms.append(response.llm_elapsed_ms)
                responses.append(response)
                all_responses.append(response)
                budget.record(response)
                _log(logs_path, _response_event(response, query.id, calls, purpose, answer_mode,
                                                prompt_version, prompt_hash, http_attempt), stamp)
                _check_deadline(started_run=started_run, started_query=started_query,
                                limits=runtime_limits, query_id=query.id, stage="llm")
                _check_memory_limit(max_rss_mb, query_id=query.id, stage="llm")
                return response
            raise PipelineError(code="API_SERVER_ERROR", stage="llm",
                                message="LLM retry loop ended without a response", query_id=query.id)

        try:
            _check_memory_limit(max_rss_mb, query_id=query.id, stage="load")
            _check_deadline(started_run=started_run, started_query=started_query,
                            limits=runtime_limits, query_id=query.id, stage="load")
            resolved_workbook = workbook_path.resolve(strict=True)
            if resolved_workbook not in table_cache:
                if input_mode == "raw":
                    table = raw_adapter.load(resolved_workbook)
                else:
                    parsed_root = Path(parsed_dir).resolve(strict=True)
                    parsed_path = (parsed_root / (resolved_workbook.stem + ".json")).resolve(strict=True)
                    if not parsed_path.is_relative_to(parsed_root):
                        raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="load", message="Parsed path escapes --parsed-dir", query_id=query.id)
                    table = parsed_adapter.load(parsed_path, source_path=resolved_workbook)
                    if table.table_id != query.file_name:
                        raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="load", message="Parsed table_id does not match the query workbook", query_id=query.id)
                table_cache[resolved_workbook] = validate_table_context(table)
                view_cache[resolved_workbook] = (raw_adapter if input_mode == "raw" else parsed_adapter).build_view(table_cache[resolved_workbook])
                _check_deadline(started_run=started_run, started_query=started_query,
                                limits=runtime_limits, query_id=query.id, stage="context")
            table = table_cache[resolved_workbook]
            view = view_cache[resolved_workbook]
            context = build_query_context(query, view, input_mode)
            _check_deadline(started_run=started_run, started_query=started_query,
                            limits=runtime_limits, query_id=query.id, stage="context")
            _log(logs_path, {"query_id": query.id, "event": "context_ready", "input_mode": input_mode,
                             "answer_mode": answer_mode, "table_view_hash": context.context_meta.table_view_hash,
                             "included_sheets": context.context_meta.sheets_included,
                             "included_ranges": context.context_meta.included_ranges}, stamp)
            _check_memory_limit(max_rss_mb, query_id=query.id, stage="context")

            if answer_mode == "direct":
                response = generate(build_direct_request(
                    context, model=settings.model, json_mode=settings.json_mode,
                    max_output_tokens=settings.max_output_tokens,
                    provider_options=settings.provider_options.get(settings.provider, {})), purpose="initial")
                answer = parse_direct_response(response.text, context)
                _check_deadline(started_run=started_run, started_query=started_query,
                                limits=runtime_limits, query_id=query.id, stage="format")
                outcome = {"status": "ok", "answer": answer, "format_valid": True, "exact_match": None,
                           "plan_valid": None, "execution_artifact": None, "final_evidence": []}
            else:
                current_plan: dict[str, Any] | None = None
                last_error: PipelineError | None = None
                execution = None
                repair_pending = False
                while True:
                    purpose = "plan_repair" if repair_pending else "initial"
                    request = build_plan_request(
                        context, model=settings.model,
                        repair_plan=current_plan if repair_pending else None,
                        repair_error=last_error if repair_pending else None,
                        json_mode=settings.json_mode, max_output_tokens=settings.max_output_tokens,
                        provider_options=settings.provider_options.get(settings.provider, {}),
                        prompt_variant=prompt_variant)
                    response = generate(request, purpose=purpose)
                    if str(response.finish_reason or "").upper() in {"MAX_TOKENS", "MAX_OUTPUT_TOKENS", "LENGTH"}:
                        expanded_request = replace(
                            request, max_output_tokens=max(request.max_output_tokens * 2,
                                                           request.max_output_tokens + 512))
                        _log(logs_path, {"query_id": query.id, "event": "output_limit_retry",
                                         "plan_attempt": calls, "finish_reason": response.finish_reason,
                                         "previous_max_output_tokens": request.max_output_tokens,
                                         "next_max_output_tokens": expanded_request.max_output_tokens}, stamp)
                        response = generate(expanded_request, purpose="output_retry")
                    repair_pending = False
                    last_error = None
                    try:
                        # Parse once here so malformed JSON can also be repaired with its raw output.
                        current_plan = strict_json_loads(response.text, parse_float=Decimal)
                        validated_plan = validate_plan_data(current_plan, context)
                        current_plan = dict(validated_plan)
                        plan_data = validated_plan
                    except (json.JSONDecodeError, ValueError) as exc:
                        current_plan = {"raw_response": response.text}
                        plan_data = None
                        last_error = PipelineError(code="INVALID_PLAN_JSON", stage="plan_validate",
                                                   message=f"Planner response is not strict JSON: {exc}", query_id=query.id)
                    except PipelineError as exc:
                        last_error = exc
                        plan_data = None

                    if last_error is None:
                        def save_step_result(step_id: str, result: StepResult) -> None:
                            _log(step_results_path, {"query_id": query.id, "plan_attempt": calls,
                                                     "step_id": step_id, "event": "step_result",
                                                     "result": result.to_dict()}, stamp)
                        try:
                            execution = execute_plan(plan_data, table, limits=runtime_limits,
                                                     step_result_sink=save_step_result)
                        except PipelineError as exc:
                            last_error = exc
                        else:
                            _check_deadline(started_run=started_run, started_query=started_query,
                                            limits=runtime_limits, query_id=query.id, stage="execute")
                            break

                    _check_deadline(started_run=started_run, started_query=started_query,
                                    limits=runtime_limits, query_id=query.id, stage="execute")

                    can_repair = (repair_calls < runtime_limits.max_planner_repair_calls
                                  and last_error is not None and last_error.code in REPAIRABLE_ERRORS)
                    _log(logs_path, {"query_id": query.id, "event": "plan_attempt_failed",
                                     "plan_attempt": calls, "error": last_error.to_dict() if last_error else None,
                                     "repair_eligible": can_repair,
                                     "failed_plan": current_plan}, stamp)
                    if not can_repair:
                        if last_error is None:
                            raise PipelineError(code="EXECUTION_ERROR", stage="execute",
                                                message="Plan attempt failed without a structured error", query_id=query.id)
                        raise last_error
                    repair_pending = True

                execution_artifact = execution.to_dict()
                with executions_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"query_id": query.id, "plan_attempt": calls,
                                             "execution": execution_artifact}, ensure_ascii=False,
                                            allow_nan=False, default=_json_default) + "\n")
                _log(logs_path, {"query_id": query.id, "event": "execution_complete",
                                 "execution_artifact": str(executions_path),
                                 "final_evidence": execution_artifact["evidence"],
                                 "trace": [trace.to_dict() for trace in execution.trace]}, stamp)
                try:
                    answer = format_answer(execution.value, context.answer_spec)
                except PipelineError as exc:
                    exc.query_id = query.id
                    raise
                _check_deadline(started_run=started_run, started_query=started_query,
                                limits=runtime_limits, query_id=query.id, stage="format")
                outcome = {"status": "ok", "answer": answer, "format_valid": True, "exact_match": None,
                           "plan_valid": True, "execution_artifact": str(executions_path),
                           "final_evidence": execution_artifact["evidence"]}

            _check_memory_limit(max_rss_mb, query_id=query.id, stage="collect")

        except PipelineError as exc:
            exc = _deadline_error(started_run=started_run, started_query=started_query,
                                  limits=runtime_limits, query_id=query.id, stage=exc.stage) or exc
            exc.query_id = exc.query_id or query.id
            outcome = {"status": "error", "answer": None, "format_valid": False, "exact_match": None,
                       "error": exc.to_dict(), "plan_valid": False if answer_mode == "plan_tools" else None,
                       "execution_artifact": None, "final_evidence": []}
        except (OSError, ValueError, KeyError) as exc:
            error = _deadline_error(started_run=started_run, started_query=started_query,
                                    limits=runtime_limits, query_id=query.id,
                                    stage="load" if context is None else "execute")
            if error is None:
                error = PipelineError(code="INVALID_TABLE_CONTEXT", stage="load" if context is None else "execute",
                                      message=str(exc), query_id=query.id)
            outcome = {"status": "error", "answer": None, "format_valid": False, "exact_match": None,
                       "error": error.to_dict(), "plan_valid": False if answer_mode == "plan_tools" else None,
                       "execution_artifact": None, "final_evidence": []}

        known_usage = _aggregate_usage(responses, partial=True)
        usage = TokenUsage() if unknown_calls else _aggregate_usage(responses)
        known_actual_cost = _sum_reported([response.actual_cost_usd for response in responses])
        actual_cost = None if unknown_calls else known_actual_cost
        outcome.update({"query_id": query.id, "table_id": query.file_name, "input_mode": input_mode,
                        "answer_mode": answer_mode, "llm_calls": calls, "planner_repair_calls": repair_calls,
                        "usage": usage.to_dict(), "usage_unknown": bool(unknown_calls),
                        "usage_unknown_calls": unknown_calls,
                        "known_usage_partial": known_usage.to_dict(),
                        "actual_cost_usd": actual_cost,
                        "actual_cost_usd_known_partial": known_actual_cost,
                        "estimated_cost_usd": None,
                        "llm_elapsed_ms": _sum_known(call_elapsed_ms),
                        "total_elapsed_ms": (time.perf_counter() - started_query) * 1000,
                        "table_view_hash": context.context_meta.table_view_hash if context else None,
                        "prompt_version": prompt_version, "trace_path": str(logs_path)})
        if labels is not None and query.id in labels:
            # Keep labeled failures in the Exact Match denominator as incorrect.
            outcome["exact_match"] = outcome["status"] == "ok" and outcome["answer"] == labels[query.id]
        if outcome["status"] == "error":
            errors.append({"query_id": query.id, "code": outcome["error"]["code"]})
            _log(logs_path, {"query_id": query.id, "event": "query_error", "error": outcome["error"]}, stamp)
        outcomes.append(outcome)
        with results_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(outcome, ensure_ascii=False, allow_nan=False, default=_json_default) + "\n")

    completed = sum(row["status"] == "ok" for row in outcomes)
    failed = len(outcomes) - completed
    run_error = _deadline_error(started_run=started_run, started_query=None,
                                limits=runtime_limits, stage="collect")
    if run_error is None:
        try:
            _check_memory_limit(max_rss_mb, query_id=None, stage="collect")
        except PipelineError as exc:
            run_error = exc
    if failed == 0 and run_error is None:
        try:
            _write_submission(run_dir / "submission.csv", outcomes,
                              before_commit=lambda: (_check_run_deadline(started_run, runtime_limits),
                                                     _check_memory_limit(max_rss_mb, query_id=None, stage="collect")))
        except PipelineError as exc:
            run_error = exc
    labeled = [row for row in outcomes if row["exact_match"] is not None]
    exact_match = (sum(row["exact_match"] is True for row in labeled) / len(labeled)) if labeled else None
    usage_total = budget.usage
    summary = {
        "run_id": stamp, "execution": "m4_multi_provider_answer_pipeline", "input_mode": input_mode,
        "answer_mode": answer_mode, "run_status": "error" if failed or run_error else "ok",
        "run_error": run_error.to_dict() if run_error else None,
        "provider": (all_responses[0].provider if all_responses and all(response.provider == all_responses[0].provider for response in all_responses) else settings.provider),
        "model": settings.model,
        "num_queries": len(work), "completed": completed, "failed": failed,
        "format_valid_rate": completed / len(work) if work else None,
        "exact_match": exact_match, "num_labeled_queries": len(labeled),
        "total_llm_calls": sum(row["llm_calls"] for row in outcomes),
        "planner_repair_calls": sum(row["planner_repair_calls"] for row in outcomes),
        "api_retries": api_retries, "prompt_tokens_total": usage_total.prompt_tokens,
        "completion_tokens_total": usage_total.completion_tokens, "tokens_total": usage_total.total_tokens,
        "thinking_tokens_total": usage_total.thinking_tokens,
        "cached_prompt_tokens_total": usage_total.cached_prompt_tokens,
        "usage_unknown": bool(budget.unknown_calls),
        "usage_unknown_calls": budget.unknown_calls,
        "known_usage_partial": budget.known_usage.to_dict(),
        "actual_cost_usd": budget.actual_cost_usd, "estimated_cost_usd": None,
        "actual_cost_usd_known_partial": budget.known_actual_cost_usd,
        "llm_elapsed_ms_total": _sum_known(all_call_elapsed_ms),
        "run_wall_time_ms": (time.perf_counter() - started_run) * 1000,
        "peak_rss_mb": peak_rss_mb(),
        "runtime_limits": runtime_limits.to_dict(), "errors": errors,
        "submission_path": "submission.csv" if failed == 0 and run_error is None else None,
        "runtime_config": {"run_profile": run_profile, "provider": settings.provider,
                           "model": settings.model, "json_mode": settings.json_mode,
                           "prompt_variant": prompt_variant,
                           "prompt_version": (("planner_v1" if prompt_variant == "A" else "planner_v1_b")
                                              if answer_mode == "plan_tools" else "direct_v1"),
                           "max_output_tokens": settings.max_output_tokens,
                           "max_api_attempts": runtime_limits.max_api_attempts,
                           "max_planner_repair_calls": runtime_limits.max_planner_repair_calls,
                           "max_rss_mb": max_rss_mb,
                           "expected_query_count": expected_query_count,
                           "expected_workbook_count": expected_workbook_count},
        "note": "Provider usage/cost fields are normalized when supplied; unknown costs remain null. Public labels are evaluation-only and are never included in prompts.",
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    final_peak = peak_rss_mb()
    summary["peak_rss_mb"] = final_peak
    if max_rss_mb is not None and (final_peak is None or final_peak >= max_rss_mb):
        (run_dir / "submission.csv").unlink(missing_ok=True)
        summary["run_status"] = "error"
        summary["submission_path"] = None
        if summary["run_error"] is None:
            summary["run_error"] = PipelineError(
                code="RESOURCE_LIMIT", stage="collect",
                message=("Could not measure process peak RSS" if final_peak is None else
                         f"Process peak RSS reached the {max_rss_mb} MiB limit"),
                details={"peak_rss_mb": final_peak, "memory_limit_mb": max_rss_mb},
            ).to_dict()
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return run_dir
