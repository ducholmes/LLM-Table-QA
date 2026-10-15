"""OpenRouter adapter using the OpenAI-compatible Chat Completions API."""

from __future__ import annotations

import math
from time import perf_counter
from typing import Any

from ..contracts import LLMRequest, LLMResponse, PipelineError
from .base import object_dict, requested_timeout, text_content, token_usage, value
from .errors import map_provider_error
from .response_schema import provider_response_schema


class OpenRouterClient:
    provider = "openrouter"

    def __init__(self, *, api_key: str, options: dict[str, Any] | None = None,
                 default_timeout_seconds: int = 90, sdk_client: Any = None) -> None:
        self.options = dict(options or {})
        self.default_timeout_seconds = default_timeout_seconds
        if sdk_client is not None:
            self._client = sdk_client
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise PipelineError(code="API_SERVER_ERROR", stage="llm",
                                    message="Install the 'openrouter' extra to use OpenRouter") from exc
            client_options: dict[str, Any] = {
                "api_key": api_key,
                "base_url": self.options.get("base_url", "https://openrouter.ai/api/v1"),
                "max_retries": 0,
                "timeout": default_timeout_seconds,
            }
            headers = {}
            if self.options.get("site_url"):
                headers["HTTP-Referer"] = self.options["site_url"]
            if self.options.get("app_name"):
                headers["X-Title"] = self.options["app_name"]
            if headers:
                client_options["default_headers"] = headers
            self._client = OpenAI(**client_options)

    def generate(self, request: LLMRequest) -> LLMResponse:
        started = perf_counter()
        timeout = requested_timeout(request, self.default_timeout_seconds)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_tokens": request.max_output_tokens,
        }
        request_options = request.provider_options.get(self.provider, request.provider_options)
        temperature = request_options.get("temperature", self.options.get("temperature"))
        if temperature is not None:
            kwargs["temperature"] = temperature
        if request.json_mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        elif request.json_mode == "json_schema":
            if not isinstance(request.response_schema, dict):
                raise PipelineError(code="STRUCTURED_OUTPUT_UNSUPPORTED", stage="llm",
                                    message="json_schema mode requires an application response schema",
                                    details={"usage_unknown": False})
            try:
                schema = provider_response_schema(request.response_schema)
            except ValueError as exc:
                raise PipelineError(code="STRUCTURED_OUTPUT_UNSUPPORTED", stage="llm",
                                    message="No OpenRouter-compatible response envelope is defined for this schema",
                                    details={"usage_unknown": False}) from exc
            kwargs["response_format"] = {
                "type": "json_schema",
                # Provider constraints cover each operator's args; the local
                # Draft 2020-12 validator remains the authoritative boundary.
                "json_schema": {"name": "table_qa_response", "strict": False,
                                 "schema": schema},
            }
            # OpenRouter-specific routing options are not top-level parameters
            # in the OpenAI SDK. Pass them through the compatible API body.
            kwargs["extra_body"] = {"provider": {"require_parameters": True}}

        try:
            client = self._client
            if callable(getattr(client, "with_options", None)):
                client = client.with_options(timeout=timeout, max_retries=0)
            response = client.chat.completions.create(**kwargs)
        except TypeError as exc:
            raise PipelineError(
                code="STRUCTURED_OUTPUT_UNSUPPORTED", stage="llm",
                message="OpenRouter request parameters are incompatible with the installed OpenAI SDK",
                details={"provider": self.provider, "usage_unknown": False,
                         "local_error": type(exc).__name__},
            ) from exc
        except Exception as exc:
            raise map_provider_error(exc, provider=self.provider) from exc

        choices = value(response, "choices", []) or []
        choice = choices[0] if choices else None
        message = value(choice, "message") if choice is not None else None
        text = text_content(value(message, "content"))
        if not text.strip():
            raise PipelineError(code="EMPTY_RESPONSE", stage="llm",
                                message="OpenRouter returned no text content",
                                details={"provider": self.provider, "usage_unknown": True,
                                         "finish_reason": value(choice, "finish_reason")})
        usage_obj = value(response, "usage")
        usage_map = object_dict(usage_obj) or {}
        prompt_details = value(usage_obj, "prompt_tokens_details", {}) or {}
        cost = value(usage_obj, "cost")
        if cost is None:
            cost = value(value(usage_obj, "cost_details", {}), "upstream_inference_cost")
        return LLMResponse(
            text=text, provider=self.provider, model_requested=request.model,
            model_returned=value(response, "model"),
            usage=token_usage(prompt=value(usage_obj, "prompt_tokens"),
                              completion=value(usage_obj, "completion_tokens"),
                              total=value(usage_obj, "total_tokens"),
                              cached=value(prompt_details, "cached_tokens")),
            llm_elapsed_ms=(perf_counter() - started) * 1000,
            actual_cost_usd=(float(cost) if type(cost) in (int, float)
                             and math.isfinite(cost) and cost >= 0 else None),
            provider_request_id=value(response, "id"),
            finish_reason=value(choice, "finish_reason") if choice is not None else None,
            raw_usage=usage_map or None,
        )
