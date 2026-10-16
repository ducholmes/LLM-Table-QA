"""Map SDK/transport failures to the shared pipeline error taxonomy."""

from __future__ import annotations

import math
import re
from typing import Any

from ..contracts import PipelineError


_API_KEY_PATTERN = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[0-9A-Za-z._~+/-]{12,}")


def _safe_provider_text(value: Any, *, limit: int = 1200) -> str | None:
    """Keep provider diagnostics while redacting common credential formats."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.strip().split())
    text = _API_KEY_PATTERN.sub("[REDACTED_API_KEY]", text)
    text = _BEARER_PATTERN.sub("Bearer [REDACTED_TOKEN]", text)
    return text[:limit] + ("..." if len(text) > limit else "")


def _provider_diagnostics(exc: Exception) -> dict[str, Any]:
    """Extract SDK-normalized fields, never the raw response or headers."""
    diagnostics: dict[str, Any] = {"provider_exception_type": type(exc).__name__}
    provider_status = _safe_provider_text(getattr(exc, "status", None), limit=160)
    provider_message = _safe_provider_text(getattr(exc, "message", None))
    if provider_status is not None:
        diagnostics["provider_status"] = provider_status
    if provider_message is not None:
        diagnostics["provider_message"] = provider_message
    return diagnostics


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


def _quota_classification(exc: Exception) -> tuple[str, str, bool]:
    """Classify a 429 without copying provider response text into artifacts."""
    parts = [type(exc).__name__, str(exc)]
    for owner in (exc, getattr(exc, "response", None)):
        if owner is None:
            continue
        for attribute in ("reason", "status", "details", "errors"):
            value = getattr(owner, attribute, None)
            if value is not None:
                parts.append(str(value))
    text = " ".join(parts).lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", text)

    daily_markers = (
        "per_day", "requests_per_day", "tokens_per_day", "daily_quota",
        "quota_day", "requestsperday", "tokensperday", "perday", "rpd",
        "daily_limit",
    )
    minute_markers = (
        "per_minute", "requests_per_minute", "tokens_per_minute",
        "requestsperminute", "tokensperminute", "perminute",
        "rate_limit_exceeded", "rpm", "tpm",
    )
    if any(marker in normalized for marker in daily_markers):
        return "day", "daily_quota_exhausted", False
    if any(marker in normalized for marker in minute_markers):
        return "minute", "minute_rate_limit_exceeded", True
    return "unknown", "rate_limit_or_quota_exceeded", True


def map_provider_error(exc: Exception, *, provider: str) -> PipelineError:
    status = _status(exc)
    name = type(exc).__name__.lower()
    detail_text = str(exc).lower()
    diagnostics = _provider_diagnostics(exc)
    provider_message = diagnostics.get("provider_message")
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
    elif status in (400, 422):
        code, message = "API_BAD_REQUEST", f"{provider} rejected the request"
    elif status is not None and status >= 500:
        code, message = "API_SERVER_ERROR", f"{provider} returned a server error"
    else:
        code, message = "API_SERVER_ERROR", f"{provider} request failed ({type(exc).__name__})"
    if provider_message is not None:
        message = f"{message}: {provider_message}"
    details: dict[str, Any] = {"provider": provider, "usage_unknown": True,
                               **diagnostics}
    if status is not None:
        details["http_status"] = status
    if code in {"API_BAD_REQUEST", "STRUCTURED_OUTPUT_UNSUPPORTED",
                "AUTH_ERROR", "PROVIDER_FORBIDDEN", "MODEL_NOT_FOUND"}:
        details["retryable"] = False
    if code == "API_RATE_LIMIT":
        quota_scope, provider_reason, retryable = _quota_classification(exc)
        details.update({"quota_scope": quota_scope, "provider_reason": provider_reason,
                        "retryable": retryable})
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        details["retry_after_seconds"] = retry_after
    return PipelineError(code=code, stage="llm", message=message, details=details)
