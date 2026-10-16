"""Construct full-table query contexts and parse answer formatting rules locally."""

from __future__ import annotations

from .contracts import AnswerSpec, InputMode, PipelineError, Query, QueryContext, TableView
from .contract_validation import validate_answer_spec, validate_query_context


def parse_answer_spec(question: str) -> AnswerSpec:
    # The planner sees the original multilingual question and declares the
    # small return.format contract itself. Regex classification here used to
    # turn labels into numeric pairs and reject otherwise correct executions.
    # Keep AnswerSpec only as a backwards-compatible direct-mode envelope.
    del question
    return validate_answer_spec(AnswerSpec(kind="generic_string", decimals=None, separator=None,
                                           allowed_values=None, item_count=None))


def build_query_context(query: Query, table_view: TableView, input_mode: InputMode) -> QueryContext:
    if not table_view.context_meta.included_ranges or not table_view.context_meta.sheets_included:
        raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="context", message="No worksheet ranges in table view", query_id=query.id)
    if not table_view.text.startswith(f"TABLE: {query.file_name} |"):
        raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="context", message="Table view belongs to a different workbook", query_id=query.id)
    spec = parse_answer_spec(query.query)
    return validate_query_context(QueryContext(query_id=query.id, table_id=query.file_name, query=query.query,
                       input_mode=input_mode, table_view=table_view.text, answer_spec=spec,
                       context_meta=table_view.context_meta))
