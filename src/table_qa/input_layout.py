"""Resolve portable submission input/output paths without relying on the CWD."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contracts import PipelineError


@dataclass(frozen=True)
class InputLayout:
    input_dir: Path
    queries_path: Path
    workbook_root: Path
    output_path: Path | None
    artifacts_dir: Path


def resolve_input_layout(*, input_dir: Path, workbook_dir: Path | None = None,
                         output_path: Path | None = None,
                         artifacts_dir: Path | None = None) -> InputLayout:
    """Resolve the judge-facing directory contract and reject unsafe outputs."""
    try:
        root = input_dir.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message=f"Input directory does not exist: {input_dir}") from exc
    if not root.is_dir():
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message=f"Input path is not a directory: {input_dir}")

    queries = root / "queries.json"
    if not queries.is_file():
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message=f"Input file not found: {queries}")

    workbook_candidate = workbook_dir if workbook_dir is not None else root
    if not workbook_candidate.is_absolute():
        workbook_candidate = root / workbook_candidate
    try:
        workbooks = workbook_candidate.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message=f"Workbook directory does not exist: {workbook_candidate}") from exc
    if not workbooks.is_dir():
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message=f"Workbook path is not a directory: {workbooks}")

    output = output_path.resolve() if output_path is not None else None
    if output is not None:
        if output == queries or output == root or output == workbooks:
            raise PipelineError(code="INVALID_QUERY", stage="load",
                                message="Output path would overwrite an input")
        if output.suffix.lower() != ".csv":
            raise PipelineError(code="INVALID_QUERY", stage="load",
                                message="--output must name a .csv file")

    artifact_root = (artifacts_dir.resolve() if artifacts_dir is not None else
                     ((output.parent / "artifacts") if output is not None else Path("runs").resolve()))
    return InputLayout(input_dir=root, queries_path=queries, workbook_root=workbooks,
                       output_path=output, artifacts_dir=artifact_root)
