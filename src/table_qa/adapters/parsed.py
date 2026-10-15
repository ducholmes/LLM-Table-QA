"""Adapter for parser-produced table_context_v1 JSON artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from ..contracts import PipelineError, TableContext, TableView
from ..contract_validation import validate_table_context
from .raw import RawWorkbookAdapter


class ParsedTableAdapter:
    def load(self, path: Path, *, source_path: Path | None = None) -> TableContext:
        path = Path(path).resolve(strict=True)
        try:
            wire = json.loads(path.read_text(encoding="utf-8"))
            table = validate_table_context(wire)
        except PipelineError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="load", message=f"Cannot read parsed table: {exc}") from exc
        if table.sheets[0].raw_rows is not None:
            raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="load", message="ParsedTableAdapter requires parsed_cells, not raw_rows")
        if source_path is not None:
            try:
                expected_hash = RawWorkbookAdapter._hash_file(Path(source_path).resolve(strict=True))
            except OSError as exc:
                raise PipelineError(code="TABLE_NOT_FOUND", stage="load", message=f"Cannot verify source workbook: {exc}") from exc
            if expected_hash != table.source_hash:
                raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="load", message="Parsed table source_hash does not match its workbook")
        return table

    def build_view(self, table: TableContext) -> TableView:
        if any(sheet.parsed_cells is None for sheet in table.sheets):
            raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="context", message="Parsed view requires parsed_cells on every sheet")
        return RawWorkbookAdapter().build_view(table)
