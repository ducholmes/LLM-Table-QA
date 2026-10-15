"""Aggregate provider-reported usage and actual cost without inventing estimates."""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import LLMResponse, TokenUsage


def _sum_known(values: list[int | float | None]) -> int | float | None:
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _sum_reported(values: list[int | float | None]) -> int | float | None:
    reported = [value for value in values if value is not None]
    return sum(reported) if reported else None


@dataclass
class RunBudget:
    """Run-local ledger; unknown provider costs stay unknown rather than zero."""

    responses: list[LLMResponse] = field(default_factory=list)
    unknown_calls: int = 0

    def record(self, response: LLMResponse) -> None:
        self.responses.append(response)

    def record_unknown_call(self) -> None:
        self.unknown_calls += 1

    @property
    def known_usage(self) -> TokenUsage:
        """Sum values actually reported by successful calls, even if partial."""
        names = ("prompt_tokens", "completion_tokens", "total_tokens", "thinking_tokens",
                 "cached_prompt_tokens")
        return TokenUsage(**{
            name: _sum_reported([getattr(response.usage, name) for response in self.responses])
            for name in names
        })

    @property
    def usage(self) -> TokenUsage:
        if self.unknown_calls:
            return TokenUsage()
        names = ("prompt_tokens", "completion_tokens", "total_tokens", "thinking_tokens",
                 "cached_prompt_tokens")
        return TokenUsage(**{
            name: _sum_known([getattr(response.usage, name) for response in self.responses])
            for name in names
        })

    @property
    def known_actual_cost_usd(self) -> float | None:
        return _sum_reported([response.actual_cost_usd for response in self.responses])

    @property
    def actual_cost_usd(self) -> float | None:
        if self.unknown_calls:
            return None
        return self.known_actual_cost_usd
