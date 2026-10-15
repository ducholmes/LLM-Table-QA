"""Deterministic, offline-only mock backend for exercising pipeline routing."""

from __future__ import annotations

from hashlib import sha256
import json
import re
from time import perf_counter

from .contracts import LLMRequest, LLMResponse, PipelineError, TokenUsage


class MockLLMClient:
    """Return shape-correct placeholder JSON, never synthetic accuracy/usage."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        started = perf_counter()
        query_id_match = re.search(r"^QUERY_ID[:=] ?([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$", request.user_prompt, re.MULTILINE)
        table_match = re.search(r"^TABLE_ID[:=] ?([^\r\n]+)$", request.user_prompt, re.MULTILINE)
        mode_match = re.search(r"^ANSWER_MODE=(direct|plan_tools)$", request.user_prompt, re.MULTILINE)
        if not query_id_match or not table_match or not mode_match:
            raise PipelineError(code="INVALID_QUERY", stage="llm", message="Mock request is missing routing metadata")
        query_id, table_id, mode = query_id_match.group(1), table_match.group(1), mode_match.group(1)
        if mode == "direct":
            # Null exercises envelope parsing without looking like a usable
            # category/name answer that could pass the shape-only validator.
            payload = {"answer": None}
        else:
            payload = {"version": "plan_v1", "query_id": query_id, "table_id": table_id,
                       "steps": [{"id": "s1", "op": "collect", "args": {"items": [f"MOCK:{query_id}"]}}],
                       "return": {"ref": "$s1"}}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return LLMResponse(text=text, provider="mock", model_requested=request.model, model_returned="mock",
                           usage=TokenUsage(), llm_elapsed_ms=(perf_counter() - started) * 1000,
                           actual_cost_usd=None, provider_request_id="mock-" + sha256(text.encode()).hexdigest()[:12],
                           finish_reason="stop", raw_usage=None)
