"""In-memory request quota gates for provider RPM and input TPM limits."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import ceil
import threading
import time
import uuid
from typing import Callable, Protocol

from .contracts import LLMRequest, PipelineError


@dataclass(frozen=True)
class RateLimitSettings:
    enabled: bool = False
    requests_per_minute: int | None = None
    input_tokens_per_minute: int | None = None
    window_seconds: float = 60.0
    token_estimate_safety_factor: float = 1.25
    max_wait_seconds: float = 120.0


@dataclass(frozen=True)
class Admission:
    waited_seconds: float
    wait_reason: str | None
    estimated_input_tokens: int
    reserved_input_tokens: int
    record_id: str | None


@dataclass
class _QuotaRecord:
    record_id: str
    started_at: float
    reserved_input_tokens: int


class RequestQuotaGate(Protocol):
    def acquire(self, estimated_input_tokens: int, *, wait_hook: Callable[[str, float], None] | None = None) -> Admission: ...
    def commit(self, admission: Admission, actual_input_tokens: int | None) -> None: ...


class NoopQuotaGate:
    def acquire(self, estimated_input_tokens: int, *, wait_hook=None) -> Admission:
        return Admission(waited_seconds=0.0, wait_reason=None,
                         estimated_input_tokens=estimated_input_tokens,
                         reserved_input_tokens=estimated_input_tokens, record_id=None)

    def commit(self, admission: Admission, actual_input_tokens: int | None) -> None:
        return None


class ActiveTimer:
    """A monotonic timer whose elapsed value excludes controlled quota waits."""

    def __init__(self, *, clock=time.perf_counter) -> None:
        self._clock = clock
        self.started_at = clock()
        self.excluded_seconds = 0.0

    def exclude(self, seconds: float) -> None:
        if seconds > 0:
            self.excluded_seconds += seconds

    def elapsed(self) -> float:
        return max(0.0, self._clock() - self.started_at - self.excluded_seconds)

    def wall_elapsed(self) -> float:
        return max(0.0, self._clock() - self.started_at)


class GeminiSlidingWindowGate:
    """Reserve RPM and input-token capacity in a rolling monotonic-time window."""

    def __init__(self, settings: RateLimitSettings, *, clock=time.monotonic,
                 sleeper=time.sleep) -> None:
        if not settings.enabled:
            raise ValueError("GeminiSlidingWindowGate requires enabled settings")
        if settings.requests_per_minute is None and settings.input_tokens_per_minute is None:
            raise ValueError("At least one RPM or TPM limit is required")
        self.settings = settings
        self._clock = clock
        self._sleeper = sleeper
        self._records: deque[_QuotaRecord] = deque()
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        cutoff = now - self.settings.window_seconds
        while self._records and self._records[0].started_at <= cutoff:
            self._records.popleft()

    def _required_wait(self, now: float, reserved: int) -> tuple[float, str | None]:
        waits: list[tuple[str, float]] = []
        rpm = self.settings.requests_per_minute
        if rpm is not None and len(self._records) >= rpm:
            index = len(self._records) - rpm
            waits.append(("rpm", self._records[index].started_at + self.settings.window_seconds - now))

        tpm = self.settings.input_tokens_per_minute
        if tpm is not None:
            current = sum(record.reserved_input_tokens for record in self._records)
            if current + reserved > tpm:
                released = 0
                expiry = now
                for record in self._records:
                    released += record.reserved_input_tokens
                    expiry = record.started_at + self.settings.window_seconds
                    if current - released + reserved <= tpm:
                        break
                waits.append(("tpm", expiry - now))

        if not waits:
            return 0.0, None
        wait_seconds = max(0.0, max(wait for _, wait in waits))
        reasons = {reason for reason, wait in waits if wait > 0}
        reason = "+".join(sorted(reasons)) if reasons else None
        return wait_seconds, reason

    def acquire(self, estimated_input_tokens: int, *, wait_hook=None) -> Admission:
        if type(estimated_input_tokens) is not int or estimated_input_tokens < 0:
            raise ValueError("estimated_input_tokens must be a non-negative integer")
        reserved = ceil(estimated_input_tokens * self.settings.token_estimate_safety_factor)
        tpm = self.settings.input_tokens_per_minute
        if tpm is not None and reserved > tpm:
            raise PipelineError(
                code="CONTEXT_TOO_LARGE", stage="llm",
                message=(f"Estimated input requires {reserved} reserved tokens, exceeding "
                         f"the configured {tpm} TPM limit"),
                details={"estimated_input_tokens": estimated_input_tokens,
                         "reserved_input_tokens": reserved,
                         "input_tokens_per_minute": tpm},
            )

        total_waited = 0.0
        combined_reasons: set[str] = set()
        while True:
            with self._lock:
                now = self._clock()
                self._evict(now)
                wait_seconds, reason = self._required_wait(now, reserved)
                if wait_seconds <= 0:
                    record_id = uuid.uuid4().hex
                    self._records.append(_QuotaRecord(record_id, now, reserved))
                    return Admission(
                        waited_seconds=total_waited,
                        wait_reason="+".join(sorted(combined_reasons)) or None,
                        estimated_input_tokens=estimated_input_tokens,
                        reserved_input_tokens=reserved,
                        record_id=record_id,
                    )

            if total_waited + wait_seconds > self.settings.max_wait_seconds:
                raise PipelineError(
                    code="API_RATE_LIMIT", stage="llm",
                    message=(f"Local Gemini quota wait would exceed "
                             f"{self.settings.max_wait_seconds:g} seconds"),
                    details={"required_wait_seconds": wait_seconds,
                             "waited_seconds": total_waited,
                             "quota_scope": reason or "unknown"},
                )
            if reason:
                combined_reasons.update(reason.split("+"))
                if wait_hook is not None:
                    wait_hook(reason, wait_seconds)
            before = self._clock()
            # A tiny margin avoids re-entering the same boundary due to clock precision.
            self._sleeper(wait_seconds + 1e-6)
            total_waited += max(0.0, self._clock() - before)

    def commit(self, admission: Admission, actual_input_tokens: int | None) -> None:
        if admission.record_id is None or actual_input_tokens is None:
            return
        if type(actual_input_tokens) is not int or actual_input_tokens < 0:
            return
        with self._lock:
            for record in self._records:
                if record.record_id == admission.record_id:
                    record.reserved_input_tokens = actual_input_tokens
                    return


def estimate_input_tokens(request: LLMRequest) -> int:
    """Conservative provider-independent approximation, reconciled after response."""
    payload = (request.system_prompt + "\n" + request.user_prompt).encode("utf-8")
    return max(1, ceil(len(payload) / 4))


def rate_limit_settings(provider: str, provider_options: dict) -> RateLimitSettings:
    if provider != "gemini":
        return RateLimitSettings()
    raw = provider_options.get("rate_limit") or {}
    return RateLimitSettings(
        enabled=raw.get("enabled", False),
        requests_per_minute=raw.get("requests_per_minute"),
        input_tokens_per_minute=raw.get("input_tokens_per_minute"),
        window_seconds=float(raw.get("window_seconds", 60)),
        token_estimate_safety_factor=float(raw.get("token_estimate_safety_factor", 1.25)),
        max_wait_seconds=float(raw.get("max_wait_seconds", 120)),
    )


def build_quota_gate(provider: str, provider_options: dict, *, clock=time.monotonic,
                     sleeper=time.sleep) -> RequestQuotaGate:
    settings = rate_limit_settings(provider, provider_options)
    if not settings.enabled:
        return NoopQuotaGate()
    return GeminiSlidingWindowGate(settings, clock=clock, sleeper=sleeper)
