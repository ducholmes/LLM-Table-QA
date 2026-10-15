"""Map SDK/transport failures to the shared pipeline error taxonomy."""

from __future__ import annotations

import math
from typing import Any

from ..contracts import PipelineError


def _status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    candidates = (getattr(exc, "status_code", None), getattr(exc, "code", None),
                 getattr(response, "status_code", None))
    for candidate in candidates:
        if type(candidate) is int:
            return candidate
    return None


def _retry_after_seconds(exc: Exception) -> float | None:
    """Return a numeric Retry-After header without exposing response contents."""
    response = getattr(exc, "response", None)
    for owner in (response, exc):
        headers = getattr(owner, "headers", None)
        if headers is None or not callable(getattr(headers, "get", None)):
            continue
        raw = headers.get("retry-after") or headers.get("Retry-After")
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def map_provider_error(exc: Exception, *, provider: str) -> PipelineError:
    status = _status(exc)
    name = type(exc).__name__.lower()
    detail_text = str(exc).lower()
    if "timeout" in name or "timeout" in detail_text or status == 408:
        code, message = "API_TIMEOUT", f"{provider} request timed out"
    elif status == 401 or "authentication" in name:
        code, message = "AUTH_ERROR", f"{provider} rejected the configured credential"
    elif status == 403 or "permission" in name:
        code, message = "PROVIDER_FORBIDDEN", f"{provider} denied this request"
    elif status == 404 and "requested parameters" in detail_text:
        code, message = ("STRUCTURED_OUTPUT_UNSUPPORTED",
                         f"{provider} has no endpoint supporting the requested parameters")
    elif status == 404 or "notfound" in name or "not_found" in name:
        code, message = "MODEL_NOT_FOUND", f"{provider} model or endpoint was not found"
    elif status == 429 or "ratelimit" in name or "resourceexhausted" in name:
        code, message = "API_RATE_LIMIT", f"{provider} rate limit or quota was reached"
    elif status in (400, 422) and any(token in detail_text for token in
                                     ("json_schema", "response_format", "structured output")):
        code, message = "STRUCTURED_OUTPUT_UNSUPPORTED", f"{provider} rejected the structured-output format"
    elif status is not None and status >= 500:
        code, message = "API_SERVER_ERROR", f"{provider} returned a server error"
    else:
        code, message = "API_SERVER_ERROR", f"{provider} request failed ({type(exc).__name__})"
    details: dict[str, Any] = {"provider": provider, "usage_unknown": True}
    if status is not None:
        details["http_status"] = status
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        details["retry_after_seconds"] = retry_after
    return PipelineError(code=code, stage="llm", message=message, details=details)
