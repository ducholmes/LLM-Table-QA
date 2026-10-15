"""Construct full-table query contexts and parse answer formatting rules locally."""

from __future__ import annotations

import re

from .contracts import AnswerSpec, InputMode, PipelineError, Query, QueryContext, TableView
from .contract_validation import validate_answer_spec, validate_query_context

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def parse_answer_spec(question: str) -> AnswerSpec:
    text = " ".join(question.strip().split())
    lower = text.casefold()
    if re.search(r"\b(first|second|third|fourth|last)[ -]name\b", lower) or "only the last name" in lower:
        kind = "last_name"
        values = None
        count = None
        decimals = None
        separator = None
    elif re.search(r"\byes\s+or\s+no\b", lower) or "only yes or no" in lower:
        kind, values, count, decimals, separator = "yes_no", ["Yes", "No"], None, None, None
    else:
        decimal_match = re.search(r"(?:rounded to|exactly)\s+(\d+|one|two|three)\s+decimal\s+places?", lower)
        decimal_match = decimal_match or re.search(r"(\d+|one|two|three)\s+decimal\s+places?", lower)
        decimals = int(decimal_match.group(1)) if decimal_match and decimal_match.group(1).isdigit() else NUMBER_WORDS.get(decimal_match.group(1), 2) if decimal_match else None
        pair_count = None
        comma_pair = re.search(r"\bas\s+`([^`]+,[^`]+)`", lower)
        if comma_pair:
            pair_count = comma_pair.group(1).count(",") + 1
        pair_match = re.search(r"\b(two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(?:integers?|values?|numbers?|sector names|category names|labels?)\b", lower)
        if pair_match:
            token = pair_match.group(1)
            pair_count = int(token) if token.isdigit() else NUMBER_WORDS[token]
        if pair_count is not None and ("separated" in lower or re.search(r"\bas\s+`?[^`\n]*,", lower)):
            count = pair_count
            if decimals is not None:
                kind, separator = "decimal_pair", ", "
            elif "integer" in lower:
                kind, separator = "number_pair", ", "
            else:
                kind, separator = "ordered_labels", ", "
            values = None
            if count > 2 and kind != "ordered_labels":
                raise PipelineError(code="ANSWER_SPEC_UNSUPPORTED", stage="context", message="Only two-number answers are supported by this answer spec")
        elif decimals is not None:
            kind, count, separator, values = "fixed_decimal", None, None, None
        elif "integer" in lower:
            kind, count, separator, values = "integer", None, None, None
        elif any(word in lower for word in ("category", "industry name", "sector name", "hour category")):
            kind, count, separator, values, decimals = "category", None, None, None, None
        else:
            raise PipelineError(code="ANSWER_SPEC_UNSUPPORTED", stage="context", message="No local answer format rule matches this question")
    return validate_answer_spec(AnswerSpec(kind=kind, decimals=decimals, separator=separator,
                                          allowed_values=values, item_count=count))


def build_query_context(query: Query, table_view: TableView, input_mode: InputMode) -> QueryContext:
    if not table_view.context_meta.included_ranges or not table_view.context_meta.sheets_included:
        raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="context", message="No worksheet ranges in table view", query_id=query.id)
    if not table_view.text.startswith(f"TABLE: {query.file_name} |"):
        raise PipelineError(code="INVALID_TABLE_CONTEXT", stage="context", message="Table view belongs to a different workbook", query_id=query.id)
    spec = parse_answer_spec(query.query)
    return validate_query_context(QueryContext(query_id=query.id, table_id=query.file_name, query=query.query,
                       input_mode=input_mode, table_view=table_view.text, answer_spec=spec,
                       context_meta=table_view.context_meta))
