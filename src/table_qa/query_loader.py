"""Load queries in source order and resolve each workbook without path escapes."""

from __future__ import annotations

from pathlib import Path

from .contracts import PipelineError, Query
from .contract_validation import validate_query
from .json_utils import strict_json_loads


def load_queries(path: Path, data_dir: Path) -> list[tuple[Query, Path]]:
    """Return validated queries paired with safe workbook paths, preserving order.

    Both the data root and each workbook are resolved before containment checks,
    so `..` and symlinks that leave the selected data directory are rejected.
    """
    try:
        root = data_dir.resolve(strict=True)
        query_path = path.resolve(strict=True)
        payload = strict_json_loads(query_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, RuntimeError) as exc:
        raise PipelineError(code="INVALID_QUERY", stage="load", message=f"Input file not found: {exc.filename}") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise PipelineError(code="INVALID_QUERY", stage="load", message=f"Cannot read queries: {exc}") from exc
    if not isinstance(payload, list):
        raise PipelineError(code="INVALID_QUERY", stage="load", message="queries.json must contain an array")
    result: list[tuple[Query, Path]] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload):
        try:
            # The judge may attach metadata fields. Only the documented input
            # contract is trusted or propagated to prompts.
            core = ({key: raw[key] for key in ("id", "file_name", "query") if key in raw}
                    if isinstance(raw, dict) else raw)
            if isinstance(core, dict) and type(core.get("id")) is int:
                core["id"] = str(core["id"])
            query = validate_query(core)
            if query.id in seen:
                raise PipelineError(code="INVALID_QUERY", stage="load", message=f"Duplicate query id: {query.id}", query_id=query.id)
            try:
                workbook = (root / query.file_name).resolve(strict=True)
            except (FileNotFoundError, RuntimeError) as exc:
                raise PipelineError(code="TABLE_NOT_FOUND", stage="load", message=f"Workbook does not exist: {query.file_name}", query_id=query.id) from exc
            if not workbook.is_relative_to(root) or not workbook.is_file() or workbook.suffix.lower() != ".xlsx":
                raise PipelineError(code="INVALID_QUERY", stage="load", message=f"Workbook is outside DATA_DIR or is not an XLSX file: {query.file_name}", query_id=query.id)
            seen.add(query.id)
            result.append((query, workbook))
        except PipelineError as exc:
            if exc.query_id is None:
                exc.query_id = str(raw.get("id", "")) if isinstance(raw, dict) else None
            exc.details = {**exc.details, "query_index": index}
            raise
    return result
