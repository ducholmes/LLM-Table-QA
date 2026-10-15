"""Aggregate per-run benchmark metrics and produce query-level error audits."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from typing import Any


def _read_json(path: Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON number {value}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"Non-finite JSON number {value}")))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Expected JSON object at {path}:{line_number}")
        rows.append(row)
    return rows


def evaluate_run(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve(strict=True)
    summary_path = run_dir / "summary.json"
    results_path = run_dir / "results.jsonl"
    if not summary_path.is_file() or not results_path.is_file():
        raise ValueError(f"Run directory lacks summary.json or results.jsonl: {run_dir}")
    stored_summary = _read_json(summary_path)
    if not isinstance(stored_summary, dict):
        raise ValueError(f"Expected a JSON object in {summary_path}")
    results = _read_jsonl(results_path)
    events = _read_jsonl(run_dir / "logs.jsonl")
    events_by_query: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        query_id = event.get("query_id")
        if isinstance(query_id, str):
            events_by_query.setdefault(query_id, []).append(event)

    total = len(results)
    format_valid = sum(row.get("format_valid") is True for row in results)
    labeled = [row for row in results if type(row.get("exact_match")) is bool]
    exact_matches = sum(row["exact_match"] is True for row in labeled)
    error_queries: dict[str, list[str]] = {}
    query_audit = []
    for row in results:
        error = row.get("error") if isinstance(row.get("error"), dict) else {}
        error_code = error.get("code")
        query_id = row.get("query_id")
        if isinstance(error_code, str):
            error_queries.setdefault(error_code, []).append(str(query_id))
        query_events = events_by_query.get(str(query_id), [])
        query_audit.append({
            "query_id": query_id,
            "table_id": row.get("table_id"),
            "status": row.get("status"),
            "format_valid": row.get("format_valid"),
            "exact_match": row.get("exact_match"),
            "answer": row.get("answer"),
            "table_view_hash": row.get("table_view_hash"),
            "error_code": error_code,
            "error": error or None,
            "llm_calls": row.get("llm_calls"),
            "planner_repair_calls": row.get("planner_repair_calls"),
            "trace_path": row.get("trace_path", str(run_dir / "logs.jsonl")),
            "trace_event_count": len(query_events),
            # Keep the per-query call/repair/execution trail alongside the path
            # to the canonical JSONL file so the benchmark artifact is auditable.
            "trace_events": query_events,
            "execution_artifact": row.get("execution_artifact"),
        })

    runtime = stored_summary.get("runtime_config", {})
    if not isinstance(runtime, dict):
        runtime = {}
    report = {
        "run_dir": str(run_dir),
        "run_id": stored_summary.get("run_id"),
        "provider": stored_summary.get("provider"),
        "model": stored_summary.get("model"),
        "input_mode": stored_summary.get("input_mode"),
        "answer_mode": stored_summary.get("answer_mode"),
        "prompt_version": runtime.get("prompt_version"),
        "prompt_variant": runtime.get("prompt_variant", "A"),
        "max_planner_repair_calls": runtime.get("max_planner_repair_calls"),
        "num_queries": total,
        "completed": sum(row.get("status") == "ok" for row in results),
        "failed": sum(row.get("status") != "ok" for row in results),
        "format_valid": format_valid,
        "format_valid_rate": format_valid / total if total else None,
        "num_labeled_queries": len(labeled),
        "exact_matches": exact_matches,
        "exact_match": exact_matches / len(labeled) if labeled else None,
        "total_llm_calls": sum(row.get("llm_calls", 0) for row in results),
        "planner_repair_calls": sum(row.get("planner_repair_calls", 0) for row in results),
        "prompt_tokens_total": stored_summary.get("prompt_tokens_total"),
        "completion_tokens_total": stored_summary.get("completion_tokens_total"),
        "tokens_total": stored_summary.get("tokens_total"),
        "actual_cost_usd": stored_summary.get("actual_cost_usd"),
        "estimated_cost_usd": stored_summary.get("estimated_cost_usd"),
        "llm_elapsed_ms_total": stored_summary.get("llm_elapsed_ms_total"),
        "run_wall_time_ms": stored_summary.get("run_wall_time_ms"),
        "usage_unknown": stored_summary.get("usage_unknown", False),
        "error_queries": error_queries,
        "warnings": [],
        "queries": query_audit,
    }
    if total != stored_summary.get("num_queries"):
        report["warnings"].append(
            f"results count {total} differs from summary num_queries {stored_summary.get('num_queries')}"
        )
    if len(labeled) != stored_summary.get("num_labeled_queries"):
        report["warnings"].append(
            f"labeled results count {len(labeled)} differs from summary num_labeled_queries "
            f"{stored_summary.get('num_labeled_queries')}"
        )
    stored_em = stored_summary.get("exact_match")
    if stored_em != report["exact_match"]:
        report["warnings"].append(
            f"computed Exact Match {report['exact_match']} differs from summary exact_match {stored_em}"
        )
    stored_format = stored_summary.get("format_valid_rate")
    if stored_format != report["format_valid_rate"]:
        report["warnings"].append(
            f"computed Format Valid Rate {report['format_valid_rate']} differs from summary format_valid_rate "
            f"{stored_format}"
        )
    trace_gaps = [row.get("query_id") for row in query_audit if not row["trace_events"]]
    if trace_gaps:
        report["missing_trace_query_ids"] = trace_gaps
        report["warnings"].append(f"no trace events found for query IDs: {', '.join(map(str, trace_gaps))}")
    else:
        report["missing_trace_query_ids"] = []
    return report


def evaluate_runs(run_dirs: list[Path]) -> dict[str, Any]:
    if not run_dirs:
        raise ValueError("At least one run directory is required")
    reports = [evaluate_run(path) for path in run_dirs]
    return {"runs": reports}


def write_evaluation(report: dict[str, Any], *, json_path: Path, csv_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_temp = json_path.with_name(json_path.name + ".tmp")
    csv_temp = csv_path.with_name(csv_path.name + ".tmp")
    fields = [
        "experiment_id", "dataset_name", "evaluation_scope", "accuracy_evidence", "run_id", "provider", "model",
        "input_mode", "answer_mode", "prompt_version", "prompt_variant", "max_planner_repair_calls",
        "num_queries", "failed", "format_valid_rate", "num_labeled_queries", "exact_match",
        "total_llm_calls", "tokens_total", "actual_cost_usd", "estimated_cost_usd",
        "llm_elapsed_ms_total", "run_wall_time_ms",
    ]
    try:
        json_temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                             encoding="utf-8")
        with csv_temp.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for run in report["runs"]:
                writer.writerow(run)
        json_temp.replace(json_path)
        csv_temp.replace(csv_path)
    finally:
        json_temp.unlink(missing_ok=True)
        csv_temp.unlink(missing_ok=True)


def _markdown_table(reports: list[dict[str, Any]]) -> str:
    headers = ("Experiment", "Provider/model", "Input", "Answer", "Prompt", "Format",
               "Exact Match", "Tokens", "Cost USD", "LLM ms")
    rows = [headers]
    for report in reports:
        rows.append((
            report.get("experiment_id") or report.get("run_id") or Path(report["run_dir"]).name,
            f"{report.get('provider')}/{report.get('model')}",
            str(report.get("input_mode")), str(report.get("answer_mode")),
            str(report.get("prompt_variant", "A")),
            _percent(report.get("format_valid_rate")),
            (f"{report.get('exact_matches')}/{report.get('num_labeled_queries')} = "
             f"{_percent(report.get('exact_match'))}"
             if report.get("exact_match") is not None else "n/a"),
            str(report.get("tokens_total")), str(report.get("actual_cost_usd")),
            str(report.get("llm_elapsed_ms_total")),
        ))
    widths = [max(len(str(row[col])) for row in rows) for col in range(len(headers))]
    lines = ["| " + " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(rows[0])) + " |",
             "| " + " | ".join("-" * widths[i] for i in range(len(headers))) + " |"]
    lines.extend("| " + " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)) + " |"
                 for row in rows[1:])
    return "\n".join(lines)


def _percent(value: Any) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate existing Table QA runs and audit query traces")
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--output-json", type=Path, default=Path("evaluation.json"))
    parser.add_argument("--output-csv", type=Path, default=Path("evaluation.csv"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = evaluate_runs(args.run_dirs)
        write_evaluation(report, json_path=args.output_json, csv_path=args.output_csv)
    except (OSError, ValueError, KeyError) as exc:
        print(f"EVALUATION_ERROR: {exc}", file=sys.stderr)
        return 2
    print(_markdown_table(report["runs"]))
    print(f"Query audit and metrics: {args.output_json}; table: {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
