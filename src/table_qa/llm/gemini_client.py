"""Google Gemini adapter using the generate_content API surface."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from ..contracts import LLMRequest, LLMResponse, PipelineError
from .base import object_dict, requested_timeout, token_usage, value
from .errors import map_provider_error
from .response_schema import provider_response_schema


class GeminiClient:
    provider = "gemini"

    def __init__(self, *, api_key: str, options: dict[str, Any] | None = None,
                 default_timeout_seconds: int = 90, sdk_client: Any = None) -> None:
        self.options = dict(options or {})
        self.default_timeout_seconds = default_timeout_seconds
        if sdk_client is not None:
            self._client = sdk_client
        else:
            try:
                from google import genai
            except ImportError as exc:
                raise PipelineError(code="API_SERVER_ERROR", stage="llm",
                                    message="Install the 'gemini' extra to use Gemini") from exc
            self._client = genai.Client(api_key=api_key, http_options={"retry_options": {"attempts": 1}})

    def generate(self, request: LLMRequest) -> LLMResponse:
        started = perf_counter()
        timeout = requested_timeout(request, self.default_timeout_seconds)
        config: dict[str, Any] = {
            "system_instruction": request.system_prompt,
            "max_output_tokens": request.max_output_tokens,
            "automatic_continuation": False,
            "http_options": {"timeout": max(1, int(timeout * 1000)),
                             "retry_options": {"attempts": 1}},
        }
        if request.json_mode in {"json_object", "json_schema"}:
            config["response_mime_type"] = "application/json"
        if request.json_mode == "json_schema":
            if not isinstance(request.response_schema, dict):
                raise PipelineError(code="STRUCTURED_OUTPUT_UNSUPPORTED", stage="llm",
                                    message="json_schema mode requires an application response schema",
                                    details={"usage_unknown": False})
            try:
                config["response_json_schema"] = provider_response_schema(request.response_schema)
            except ValueError as exc:
                raise PipelineError(code="STRUCTURED_OUTPUT_UNSUPPORTED", stage="llm",
                                    message="No Gemini-compatible response envelope is defined for this schema",
                                    details={"usage_unknown": False}) from exc
        if request.model.startswith("gemini-3"):
            request_options = request.provider_options.get(self.provider, request.provider_options)
            thinking_level = request_options.get("thinking_level", self.options.get("thinking_level", "low"))
            config["thinking_config"] = {"thinking_level": thinking_level}

        try:
            response = self._client.models.generate_content(
                model=request.model, contents=request.user_prompt, config=config)
        except Exception as exc:
            raise map_provider_error(exc, provider=self.provider) from exc

        try:
            text = value(response, "text")
        except Exception:
            text = ""
        if not isinstance(text, str) or not text.strip():
            raise PipelineError(code="EMPTY_RESPONSE", stage="llm",
                                message="Gemini returned no text content")
        metadata = value(response, "usage_metadata")
        usage_map = object_dict(metadata) or {}
        candidates = value(response, "candidates", []) or []
        candidate = candidates[0] if candidates else None
        return LLMResponse(
            text=text, provider=self.provider, model_requested=request.model,
            model_returned=value(response, "model_version") or value(response, "model"),
            usage=token_usage(prompt=value(metadata, "prompt_token_count"),
                              completion=value(metadata, "candidates_token_count"),
                              total=value(metadata, "total_token_count"),
                              thinking=value(metadata, "thoughts_token_count"),
                              cached=value(metadata, "cached_content_token_count")),
            llm_elapsed_ms=(perf_counter() - started) * 1000,
            actual_cost_usd=None,
            provider_request_id=value(response, "response_id"),
            finish_reason=value(candidate, "finish_reason") if candidate is not None else None,
            raw_usage=usage_map or None,
        )
