"""Signatures for future milestones. Protocols contain no reader/tool/API logic."""

from pathlib import Path
from typing import Any, Protocol

from .contracts import (
    AnswerSpec, ExecutionResult, InputMode, LLMRequest, LLMResponse, Query,
    QueryContext, RuntimeValue, StepResult, TableContext, TableView, ValidatedPlan,
)


class TableAdapter(Protocol):
    def load(self, path: Path) -> TableContext: ...
    def build_view(self, table: TableContext) -> TableView: ...


class ContextBuilder(Protocol):
    def __call__(self, query: Query, table_view: TableView, input_mode: InputMode) -> QueryContext: ...


class LLMClient(Protocol):
    """Synchronous client; implementations must honor request.timeout_seconds.

    Provider adapters must set a transport-level timeout from this value so a
    blocking network request returns control to the runner by its deadline.
    """

    def generate(self, request: LLMRequest) -> LLMResponse: ...


class PlanValidator(Protocol):
    def __call__(self, plan: dict[str, Any], query_context: QueryContext) -> ValidatedPlan: ...


class Tool(Protocol):
    def __call__(self, table: TableContext, **resolved_args: Any) -> StepResult: ...


class PlanExecutor(Protocol):
    def __call__(self, plan: ValidatedPlan, table: TableContext) -> ExecutionResult: ...


class AnswerFormatter(Protocol):
    def __call__(self, value: RuntimeValue, spec: AnswerSpec) -> str: ...


class AnswerValidator(Protocol):
    def __call__(self, answer: str, spec: AnswerSpec) -> bool: ...


class Evaluator(Protocol):
    def __call__(self, answer: str, label: str) -> bool: ...
