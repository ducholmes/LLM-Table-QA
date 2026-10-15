"""Provider-independent helpers for LLM adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts import LLMRequest, LLMResponse, PipelineError, TokenUsage


def value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def integer(value_: Any) -> int | None:
    return value_ if type(value_) is int and value_ >= 0 else None


def numeric(value_: Any) -> float | None:
    if type(value_) in (int, float) and value_ >= 0:
        return float(value_)
    return None


def object_dict(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, Mapping):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except Exception:
            return None
    return None


def token_usage(*, prompt: Any = None, completion: Any = None, total: Any = None,
                thinking: Any = None, cached: Any = None) -> TokenUsage:
    return TokenUsage(prompt_tokens=integer(prompt), completion_tokens=integer(completion),
                      total_tokens=integer(total), thinking_tokens=integer(thinking),
                      cached_prompt_tokens=integer(cached))


def requested_timeout(request: LLMRequest, default_seconds: int) -> float:
    timeout = request.timeout_seconds if request.timeout_seconds is not None else default_seconds
    if type(timeout) not in (int, float) or timeout <= 0:
        raise PipelineError(code="API_TIMEOUT", stage="llm", message="LLM request timeout must be positive",
                            details={"usage_unknown": False})
    return float(timeout)


def require_api_key(api_key: str | None, env_name: str) -> str:
    if not isinstance(api_key, str) or not api_key.strip():
        raise PipelineError(code="AUTH_ERROR", stage="llm",
                            message=f"Missing provider credential; set {env_name}")
    return api_key.strip()


def text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            part = value(item, "text")
            if isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""
