"""Prompt construction and strict response handling for DIRECT mode."""

import json
from pathlib import Path

from .contracts import LLMRequest, PipelineError, QueryContext
from .planner import DIRECT_SCHEMA
from .validator import validate_direct_envelope


PROMPT_PATH = Path(__file__).with_name("prompts") / "direct_v1.txt"
PROMPT_VERSION = "direct_v1"


def build_direct_request(context: QueryContext, *, model: str = "mock", json_mode: str = "json_object",
                         max_output_tokens: int = 2400,
                         provider_options: dict | None = None) -> LLMRequest:
    user_prompt = "\n".join((
        f"QUERY_ID: {context.query_id}", f"TABLE_ID: {context.table_id}", "ANSWER_MODE=direct",
        "QUESTION_JSON: " + json.dumps(context.query, ensure_ascii=False),
        "ANSWER_SPEC: " + context.answer_spec.to_json(),
        "TABLE_CONTEXT:", context.table_view,
        'Return only {"answer":"..."}.',
    ))
    return LLMRequest(system_prompt=PROMPT_PATH.read_text(encoding="utf-8"), user_prompt=user_prompt,
                      model=model, json_mode=json_mode, response_schema=DIRECT_SCHEMA,
                      max_output_tokens=max_output_tokens, provider_options=provider_options or {})


def parse_direct_response(response_text: str, context: QueryContext) -> str:
    try:
        return validate_direct_envelope(response_text, context.answer_spec)
    except PipelineError as exc:
        exc.query_id = context.query_id
        raise
