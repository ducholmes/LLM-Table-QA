"""CLI for offline mock, provider-backed benchmark, and locked submission runs."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

from .config import AppConfig, load_config, validate_config
from .contracts import PipelineError, RuntimeLimits
from .input_layout import resolve_input_layout
from .llm.factory import LLMBackendFactory
from .runtime_env import load_local_env
from .runner import run_offline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the LLM Table QA pipeline")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    parser.add_argument("--input-dir", nargs="+", metavar="PATH_PART",
                        help="Directory containing queries.json and, by default, the XLSX files")
    parser.add_argument("--workbook-dir", type=Path,
                        help="Optional workbook directory (relative paths are resolved under --input-dir)")
    parser.add_argument("--queries", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path, help="Exact submission.csv destination")
    parser.add_argument("--artifacts-dir", type=Path, help="Directory for run logs and diagnostics")
    parser.add_argument("--input-mode", choices=("raw", "parsed"))
    parser.add_argument("--parsed-dir", type=Path)
    parser.add_argument("--answer-mode", choices=("plan_tools", "direct"))
    parser.add_argument("--prompt-variant", choices=("A", "B"),
                        help="Planner prompt variant for plan_tools (direct supports A only)")
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--labels", type=Path, help="Optional CSV labels for offline evaluation only")
    parser.add_argument("--llm-provider", "--provider", dest="llm_provider",
                        choices=("mock", "gemini", "openrouter"),
                        help="Development-only provider override")
    parser.add_argument("--model", help="Development-only model override")
    parser.add_argument("--request-timeout-seconds", type=int)
    parser.add_argument("--query-timeout-seconds", type=int)
    parser.add_argument("--run-timeout-seconds", type=int)
    return parser


def _validate_final_config(config: AppConfig) -> AppConfig:
    data = asdict(config)
    for key in ("queries", "data_dir", "parsed_dir", "run_root"):
        if data[key] is not None:
            data[key] = str(data[key])
    return validate_config(data)


def _joined_path(parts: list[str] | None) -> Path | None:
    """Accept quoted paths or unquoted shell tokens for judge-friendly CLI use."""
    return Path(" ".join(parts)) if parts else None


def _apply_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    if config.run_profile in {"competition", "submission"}:
        override_flags = {
            "input_mode": args.input_mode, "parsed_dir": args.parsed_dir,
            "answer_mode": args.answer_mode,
            "provider": args.llm_provider, "model": args.model,
            "request_timeout_seconds": args.request_timeout_seconds,
            "query_timeout_seconds": args.query_timeout_seconds,
            "run_timeout_seconds": args.run_timeout_seconds,
        }
        supplied = [name for name, value in override_flags.items() if value is not None]
        if args.prompt_variant is not None:
            supplied.append("prompt_variant")
        if supplied:
            raise PipelineError(code="INVALID_QUERY", stage="load",
                                message=("Competition profile does not accept CLI overrides: "
                                         + ", ".join(sorted(supplied))))
    llm = config.llm
    if args.llm_provider is not None:
        default_models = {"mock": "mock", "gemini": "gemini-3.8-flash",
                          "openrouter": "dots-studio/dots-3-note-preview:free"}
        llm = replace(llm, provider=args.llm_provider,
                      model=(args.model or default_models[args.llm_provider]))
    elif args.model is not None:
        llm = replace(llm, model=args.model)
    if args.request_timeout_seconds is not None:
        llm = replace(llm, request_timeout_seconds=args.request_timeout_seconds)
    config = replace(config, llm=llm)
    overrides = {
        "queries": args.queries, "data_dir": args.data_dir,
        "input_mode": args.input_mode, "parsed_dir": args.parsed_dir,
        "answer_mode": args.answer_mode, "run_root": args.run_root,
    }
    config = replace(config, **{key: value for key, value in overrides.items() if value is not None})
    if args.answer_mode is not None:
        config = replace(config, prompt_version="planner_v1" if args.answer_mode == "plan_tools" else "direct_v1")
    return _validate_final_config(config)


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    args = build_parser().parse_args(argv)
    try:
        config = _apply_overrides(load_config(args.config), args)
        input_dir = _joined_path(args.input_dir)
        if config.run_profile == "submission" and (args.input_dir is None or args.output is None):
            raise PipelineError(code="INVALID_QUERY", stage="load",
                                message="Submission profile requires --input-dir and --output")
        if config.run_profile in {"competition", "submission"} and args.labels is not None:
            raise PipelineError(code="INVALID_QUERY", stage="load",
                                message="Submission profile does not accept evaluation labels")
        if input_dir is not None:
            if args.queries is not None or args.data_dir is not None:
                raise PipelineError(code="INVALID_QUERY", stage="load",
                                    message="--input-dir cannot be combined with --queries or --data-dir")
            layout = resolve_input_layout(input_dir=input_dir,
                                          workbook_dir=args.workbook_dir,
                                          output_path=args.output,
                                          artifacts_dir=args.artifacts_dir or args.run_root)
            config = replace(config, queries=layout.queries_path, data_dir=layout.workbook_root,
                             run_root=layout.artifacts_dir)
            output_path = layout.output_path
        else:
            if args.workbook_dir is not None:
                raise PipelineError(code="INVALID_QUERY", stage="load",
                                    message="--workbook-dir requires --input-dir")
            output_path = args.output.resolve() if args.output is not None else None
            if args.artifacts_dir is not None:
                config = replace(config, run_root=args.artifacts_dir)
        if config.input_mode == "parsed" and config.parsed_dir is None:
            raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="load",
                                message="parsed input requires parsed_dir in config or --parsed-dir")
        limits = RuntimeLimits(
            max_api_attempts=config.llm.max_api_attempts,
            max_planner_repair_calls=config.llm.max_planner_repair_calls,
            request_timeout_seconds=config.llm.request_timeout_seconds,
            query_timeout_seconds=(args.query_timeout_seconds if args.query_timeout_seconds is not None
                                   else config.query_timeout_seconds),
            run_timeout_seconds=(args.run_timeout_seconds if args.run_timeout_seconds is not None
                                 else config.run_timeout_seconds),
        )
        # Resolve missing credentials before query/workbook loading. Never log credential values.
        client = LLMBackendFactory.create(config.llm)
        run_dir = run_offline(
            queries_path=config.queries, data_dir=config.data_dir,
            input_mode=config.input_mode, answer_mode=config.answer_mode,
            parsed_dir=config.parsed_dir, run_root=config.run_root, client=client,
            limits=limits, labels_path=args.labels, llm_config=config.llm,
            run_profile=config.run_profile, prompt_variant=args.prompt_variant or "A",
            max_rss_mb=config.max_rss_mb,
            expected_query_count=config.expected_query_count,
            expected_workbook_count=config.expected_workbook_count,
            output_path=output_path, submission_policy=config.submission_policy,
        )
    except PipelineError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"RUN_IO_ERROR: {exc}", file=sys.stderr)
        return 2
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    failed = summary.get("failed", 0)
    print(f"Run written to {run_dir}; failed queries: {failed}")
    if failed:
        try:
            for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
                result = json.loads(line)
                error = result.get("error")
                if result.get("status") == "error" and isinstance(error, dict):
                    print(f"Query {result.get('query_id')}: {error.get('code')}: "
                          f"{error.get('message')}", file=sys.stderr)
        except (OSError, json.JSONDecodeError):
            pass
    if summary.get("run_error"):
        run_error = summary["run_error"]
        print(f"Run error: {run_error.get('code')}: {run_error.get('message')}", file=sys.stderr)
    if summary.get("submission_path"):
        print(f"Submission written to {summary['submission_path']}")
    print("Artifacts include provider/model, usage and latency telemetry.")
    return 1 if failed or summary.get("run_status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
