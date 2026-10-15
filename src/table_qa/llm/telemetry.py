"""Shared call timing so provider adapters expose comparable latency."""

from __future__ import annotations

from dataclasses import replace
from threading import Lock
from time import perf_counter

from ..contracts import LLMRequest, LLMResponse, PipelineError
from ..interfaces import LLMClient


class MeasuredClient:
    """Measure every call, including errors, without retrying or changing routing."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.calls = 0
        self._lock = Lock()

    def generate(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            self.calls += 1
        started = perf_counter()
        try:
            response = self.client.generate(request)
        except PipelineError as exc:
            elapsed_ms = (perf_counter() - started) * 1000
            exc.details.setdefault("llm_elapsed_ms", elapsed_ms)
            raise
        except Exception as exc:
            elapsed_ms = (perf_counter() - started) * 1000
            raise PipelineError(
                code="API_SERVER_ERROR", stage="llm",
                message=f"LLM client raised {type(exc).__name__}",
                details={"llm_elapsed_ms": elapsed_ms},
            ) from exc
        return replace(response, llm_elapsed_ms=(perf_counter() - started) * 1000)
