"""Run reproducible Table QA experiment groups and retain evaluation artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any
import uuid

from .adapters.parsed import ParsedTableAdapter
from .adapters.raw import RawWorkbookAdapter
from .config import AppConfig, load_config
from .contracts import PipelineError, RuntimeLimits
from .evaluate import _markdown_table, evaluate_run, write_evaluation
from .llm.factory import LLMBackendFactory
from .query_loader import load_queries
from .runtime_env import load_local_env
from .runner import _load_labels, run_offline


def experiment_specs(*, include_parsed: bool, compare_prompt_b: bool) -> list[dict[str, str]]:
    """Build the controlled E1/E2 suite; parsed runs are opt-in after preflight."""
    specs = [
        {"id": "E1", "input_mode": "raw", "answer_mode": "direct", "prompt_variant": "A"},
        {"id": "E2", "input_mode": "raw", "answer_mode": "plan_tools", "prompt_variant": "A"},
    ]
    if compare_prompt_b:
        specs.append({"id": "E2B", "input_mode": "raw", "answer_mode": "plan_tools", "prompt_variant": "B"})
    if include_parsed:
        specs.extend([
            {"id": "E3", "input_mode": "parsed", "answer_mode": "direct", "prompt_variant": "A"},
            {"id": "E4", "input_mode": "parsed", "answer_mode": "plan_tools", "prompt_variant": "A"},
        ])
        if compare_prompt_b:
            specs.append({"id": "E4B", "input_mode": "parsed", "answer_mode": "plan_tools", "prompt_variant": "B"})
    return specs


def _compare_table_view_hashes(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Verify answer/prompt variants saw the same full table within each input mode."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for report in reports:
        grouped.setdefault(report["input_mode"], []).append(report)
    checks: dict[str, dict[str, Any]] = {}
    for input_mode, mode_reports in grouped.items():
        by_experiment = {
            report.get("experiment_id", report.get("run_id")): {
                row.get("query_id"): row.get("table_view_hash") for row in report["queries"]
            }
            for report in mode_reports
        }
        query_ids = set().union(*(set(values) for values in by_experiment.values()))
        mismatches = []
        missing = []
        for query_id in sorted(query_ids, key=str):
            hashes = {experiment: values.get(query_id) for experiment, values in by_experiment.items()}
            if len(set(hashes.values())) > 1:
                mismatches.append({"query_id": query_id, "hashes": hashes})
            if any(value is None for value in hashes.values()):
                missing.append(query_id)
        checks[input_mode] = {
            "experiments": list(by_experiment),
            "query_count": len(query_ids),
            "verified_queries": len(query_ids) - len(missing),
            "missing_hash_query_ids": missing,
            "mismatches": mismatches,
            "consistent": not mismatches,
            "complete": not missing and not mismatches,
        }
    return checks


def _preflight_parsed(parsed_dir: Path | None, work: list[tuple[Any, Path]]) -> int:
    if parsed_dir is None:
        raise ValueError("E3/E4 require --parsed-dir with parser-produced table_context_v1 artifacts")
    parsed_root = Path(parsed_dir).resolve(strict=True)
    adapter = ParsedTableAdapter()
    seen: set[Path] = set()
    for _, workbook_path in work:
        workbook = workbook_path.resolve(strict=True)
        if workbook in seen:
            continue
        seen.add(workbook)
        try:
            parsed_path = (parsed_root / (workbook.stem + ".json")).resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise ValueError(f"Missing parsed artifact for {workbook.name}: expected {workbook.stem}.json") from exc
        if not parsed_path.is_relative_to(parsed_root):
            raise ValueError(f"Parsed artifact path escapes --parsed-dir for {workbook.name}")
        table = adapter.load(parsed_path, source_path=workbook)
        if table.table_id != workbook.name:
            raise ValueError(f"Parsed artifact table_id does not match source workbook {workbook.name}")
        if table.metadata.get("parsed_integrated") is not True:
            raise ValueError(
                f"Parsed artifact for {workbook.name} is not marked parsed_integrated=true; "
                "manual fixtures cannot enable E3/E4"
            )
        _validate_parsed_source_coverage(workbook, table)
    return len(seen)


def _validate_parsed_source_coverage(workbook: Path, parsed_table: Any) -> None:
    """Require parsed artifacts to retain every physical source cell and value."""
    raw_table = RawWorkbookAdapter().load(workbook)
    if [sheet.name for sheet in raw_table.sheets] != [sheet.name for sheet in parsed_table.sheets]:
        raise ValueError(f"Parsed sheet list/order differs from source workbook {workbook.name}")
    for raw_sheet, parsed_sheet in zip(raw_table.sheets, parsed_table.sheets):
        if (raw_sheet.dimensions != parsed_sheet.dimensions
                or raw_sheet.visibility != parsed_sheet.visibility
                or raw_sheet.merged_ranges != parsed_sheet.merged_ranges):
            raise ValueError(f"Parsed sheet metadata differs from source workbook {workbook.name}: {raw_sheet.name}")
        raw_cells = {cell.cell: cell for row in (raw_sheet.raw_rows or []) for cell in row.cells}
        parsed_cells = {cell.cell: cell for cell in (parsed_sheet.parsed_cells or [])}
        if raw_cells.keys() != parsed_cells.keys():
            missing = sorted(raw_cells.keys() - parsed_cells.keys())
            extra = sorted(parsed_cells.keys() - raw_cells.keys())
            raise ValueError(
                f"Parsed cell coverage differs from {workbook.name}/{raw_sheet.name}; "
                f"missing={missing[:5]}, extra={extra[:5]}"
            )
        for address, raw_cell in raw_cells.items():
            parsed_cell = parsed_cells[address]
            fields = ("value_raw", "value_type", "display_value", "number_format", "formula", "date_iso")
            if any(getattr(raw_cell, field) != getattr(parsed_cell, field) for field in fields):
                raise ValueError(f"Parsed cell data differs from source at {workbook.name}/{raw_sheet.name}!{address}")


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                             encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _default_labels(queries_path: Path) -> Path | None:
    parent = Path(queries_path).parent
    for candidate in (parent / "labels.csv", parent / "sample_submission.csv"):
        if candidate.is_file():
            return candidate
    return None


def run_benchmark(*, config: AppConfig, output_dir: Path, queries_path: Path | None = None,
                  data_dir: Path | None = None, labels_path: Path | None = None,
                  parsed_dir: Path | None = None, include_parsed: bool = False,
                  compare_prompt_b: bool = False, allow_mock: bool = False,
                  dataset_name: str = "public_or_custom") -> Path:
    """Run each experiment once with shared provider/model, labels, and limits."""
    if config.run_profile != "development":
        raise ValueError("Benchmark experiments require run_profile=development")
    if config.llm.provider == "mock" and not allow_mock:
        raise ValueError("Mock benchmark runs are plumbing checks only; pass --allow-mock explicitly")

    queries = Path(queries_path or config.queries)
    data = Path(data_dir or config.data_dir)
    selected_labels = Path(labels_path) if labels_path is not None else _default_labels(queries)
    selected_parsed = Path(parsed_dir) if parsed_dir is not None else config.parsed_dir

    # Resolve credentials and all data contracts before creating artifacts or making calls.
    client = LLMBackendFactory.create(config.llm)
    work = load_queries(queries, data)
    if not work:
        raise ValueError("Benchmark input must contain at least one query")
    synthetic_splits = {
        query.id.split("_", 2)[1]
        for query, _ in work
        if query.id.startswith("syn_") and len(query.id.split("_", 2)) == 3
        and query.id.split("_", 2)[1] in {"dev", "holdout"}
    }
    synthetic_splits.update(
        split for _, workbook in work
        for split in ("dev", "holdout")
        if workbook.name.startswith(f"synthetic_{split}_")
    )
    if len(synthetic_splits) > 1:
        raise ValueError("Synthetic dev and holdout queries cannot be mixed in one benchmark")
    synthetic_split = next(iter(synthetic_splits), None)
    if (synthetic_split == "holdout" or "holdout" in dataset_name.casefold()) and compare_prompt_b:
        raise ValueError("Do not A/B tune prompts on synthetic holdout; compare on dev and evaluate the selected prompt once")
    if compare_prompt_b and any(query.id.startswith("p4_q") for query, _ in work):
        raise ValueError("The public p4 query set is diagnostic holdout data; do not tune prompts on it")
    if (compare_prompt_b and synthetic_split != "dev"
            and dataset_name.casefold() not in {"dev", "synthetic-dev"}):
        raise ValueError("A/B prompt comparison is restricted to dev data; keep public/holdout labels for final diagnostics")
    labels = _load_labels(selected_labels, {query.id for query, _ in work}) if selected_labels else None
    if include_parsed:
        parsed_table_count = _preflight_parsed(selected_parsed, work)
    else:
        parsed_table_count = None

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    stamp = root.name
    specs = experiment_specs(include_parsed=include_parsed, compare_prompt_b=compare_prompt_b)
    manifest: dict[str, Any] = {
        "benchmark_id": stamp,
        "status": "running",
        "dataset_name": dataset_name,
        "synthetic_split": synthetic_split,
        "queries_path": str(queries.resolve()),
        "data_dir": str(data.resolve()),
        "labels_path": str(selected_labels.resolve()) if selected_labels else None,
        "parsed_dir": str(selected_parsed.resolve()) if include_parsed and selected_parsed else None,
        "unique_workbooks": len({workbook.resolve() for _, workbook in work}),
        "parsed_preflight_workbooks": parsed_table_count,
        "provider": config.llm.provider,
        "model": config.llm.model,
        "json_mode": config.llm.json_mode,
        "max_output_tokens": config.llm.max_output_tokens,
        "max_api_attempts": config.llm.max_api_attempts,
        "max_planner_repair_calls": config.llm.max_planner_repair_calls,
        "query_timeout_seconds": config.query_timeout_seconds,
        "run_timeout_seconds": config.run_timeout_seconds,
        "mock_is_accuracy_evidence": False,
        "experiments": [],
        "evaluation_path": "evaluation.json",
        "table_path": "evaluation.csv",
    }
    _json_write(root / "benchmark_manifest.json", manifest)

    limits = RuntimeLimits(
        max_api_attempts=config.llm.max_api_attempts,
        max_planner_repair_calls=config.llm.max_planner_repair_calls,
        request_timeout_seconds=config.llm.request_timeout_seconds,
        query_timeout_seconds=config.query_timeout_seconds,
        run_timeout_seconds=config.run_timeout_seconds,
    )
    reports: list[dict[str, Any]] = []
    for spec in specs:
        run_root = root / "runs" / spec["id"]
        try:
            run_dir = run_offline(
                queries_path=queries, data_dir=data, input_mode=spec["input_mode"],
                answer_mode=spec["answer_mode"], parsed_dir=selected_parsed,
                run_root=run_root, client=client, limits=limits, labels_path=selected_labels,
                llm_config=config.llm, run_profile="development", prompt_variant=spec["prompt_variant"],
            )
            report = evaluate_run(run_dir)
            report["experiment_id"] = spec["id"]
            report["dataset_name"] = dataset_name
            report["evaluation_scope"] = "plumbing_only" if config.llm.provider == "mock" else "model_evaluation"
            report["accuracy_evidence"] = config.llm.provider != "mock" and bool(labels)
            reports.append(report)
            manifest["experiments"].append({**spec, "run_dir": str(run_dir),
                                             "run_status": json.loads((run_dir / "summary.json").read_text(
                                                 encoding="utf-8")).get("run_status")})
            _json_write(root / "benchmark_manifest.json", manifest)
        except (OSError, ValueError, PipelineError) as exc:
            manifest["experiments"].append({**spec, "status": "error", "error": str(exc)})
            manifest["status"] = "error"
            _json_write(root / "benchmark_manifest.json", manifest)
            raise

    comparison_checks = _compare_table_view_hashes(reports)
    comparison_warnings = []
    for report in reports:
        comparison_warnings.extend(
            f"{report['experiment_id']}: {warning}" for warning in report.get("warnings", [])
        )
    for mode, check in comparison_checks.items():
        if check["mismatches"]:
            comparison_warnings.append(
                f"{mode} table_view_hash differs across {', '.join(check['experiments'])}"
            )
        if not check["complete"] and not check["mismatches"]:
            comparison_warnings.append(f"{mode} has query results without a table_view_hash")
    has_accuracy_evidence = (config.llm.provider != "mock" and bool(labels)
                             and not comparison_warnings)
    for report in reports:
        report["accuracy_evidence"] = has_accuracy_evidence
    evaluation = {"benchmark_id": stamp, "dataset_name": dataset_name,
                  "synthetic_split": synthetic_split,
                  "accuracy_evidence": has_accuracy_evidence,
                  "comparison_checks": {"table_view_hash": comparison_checks},
                  "warnings": comparison_warnings,
                  "runs": reports}
    write_evaluation(evaluation, json_path=root / "evaluation.json", csv_path=root / "evaluation.csv")
    manifest["status"] = "complete" if not comparison_warnings else "completed_with_warnings"
    manifest["table_view_hash_checks"] = comparison_checks
    manifest["warnings"] = comparison_warnings
    manifest["evaluation_scope"] = "plumbing_only" if config.llm.provider == "mock" else "model_evaluation"
    manifest["accuracy_evidence"] = evaluation["accuracy_evidence"]
    _json_write(root / "benchmark_manifest.json", manifest)
    return root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run controlled E1/E2 Table QA benchmark experiments")
    parser.add_argument("--config", type=Path, default=Path("config/benchmark_openrouter.yaml"))
    parser.add_argument("--queries", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--labels", type=Path, help="Optional evaluation CSV; defaults to sibling labels.csv/sample_submission.csv")
    parser.add_argument("--parsed-dir", type=Path)
    parser.add_argument("--include-parsed", action="store_true", help="Enable E3/E4 only after all artifact hash checks pass")
    parser.add_argument("--compare-prompt-b", action="store_true", help="Add E2B and, when parsed, E4B")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dataset-name", default="public_or_custom")
    parser.add_argument("--allow-mock", action="store_true", help="Allow an offline plumbing-only benchmark; never accuracy evidence")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
        output_dir = args.output_dir or (config.run_root / f"benchmark_{now_stamp}")
        root = run_benchmark(config=config, output_dir=output_dir, queries_path=args.queries,
                             data_dir=args.data_dir, labels_path=args.labels, parsed_dir=args.parsed_dir,
                             include_parsed=args.include_parsed, compare_prompt_b=args.compare_prompt_b,
                             allow_mock=args.allow_mock, dataset_name=args.dataset_name)
        report = json.loads((root / "evaluation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, PipelineError) as exc:
        print(f"BENCHMARK_ERROR: {exc}", file=sys.stderr)
        return 2
    print(_markdown_table(report["runs"]))
    for warning in report.get("warnings", []):
        print(f"Benchmark warning: {warning}")
    scope = "plumbing only; not accuracy evidence" if not report["accuracy_evidence"] else "labeled model evaluation"
    print(f"Benchmark artifacts: {root} ({scope})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
