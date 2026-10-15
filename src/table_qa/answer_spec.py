"""Typed answer formatting and strict runtime format checks for answer_spec_v1."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re
from typing import Any

from .contracts import AnswerSpec, PipelineError, RuntimeValue
from .contract_validation import validate_answer_spec
from .json_utils import strict_json_loads


_INTEGER_RE = re.compile(r"^-?\d+$")
def _number(value: Any) -> Decimal:
    if type(value) is int:
        return Decimal(value)
    if isinstance(value, Decimal) and value.is_finite():
        return value
    raise ValueError("Expected a finite typed number (int or Decimal)")


def _quantize(value: Any, places: int, rounding: str) -> str:
    number = _number(value)
    quantum = Decimal(1).scaleb(-places)
    rounding_mode = {"ROUND_HALF_UP": ROUND_HALF_UP}.get(rounding)
    if rounding_mode is None:
        raise ValueError(f"Unsupported rounding mode: {rounding}")
    # Give quantize enough precision for large values and requested decimals.
    precision = max(28, len(number.as_tuple().digits) + abs(number.as_tuple().exponent) + places + 4)
    with localcontext() as context:
        context.prec = precision
        result = number.quantize(quantum, rounding=rounding_mode)
    return f"{result:.{places}f}"


def _fail(message: str) -> None:
    raise PipelineError(code="FORMAT_INVALID", stage="format", message=message)


def _plain_label(value: Any, kind: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{kind} answers must be strings")
    result = value.strip()
    if not result or "\n" in result or "\r" in result:
        raise ValueError(f"{kind} answer must be one non-empty line")
    return result


def format_answer(value: RuntimeValue, spec: AnswerSpec | dict[str, Any]) -> str:
    """Render a tool result according to the validated answer format contract."""
    answer_spec = validate_answer_spec(spec)
    kind = answer_spec.kind
    try:
        if kind == "integer":
            number = _number(value)
            if number != number.to_integral_value():
                raise ValueError("Integer answer cannot contain a fractional value")
            answer = str(int(number))
        elif kind == "fixed_decimal":
            answer = _quantize(value, answer_spec.decimals, answer_spec.rounding)
        elif kind == "yes_no":
            if type(value) is bool:
                answer = "Yes" if value else "No"
            else:
                answer = _plain_label(value, kind)
        elif kind in ("last_name", "category"):
            answer = _plain_label(value, kind)
        elif kind == "ordered_labels":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError("ordered_labels requires a list of strings")
            if len(value) != answer_spec.item_count:
                raise ValueError(f"Expected {answer_spec.item_count} labels, got {len(value)}")
            labels = [_plain_label(item, kind) for item in value]
            answer = answer_spec.separator.join(labels)
        elif kind == "number_pair":
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("number_pair requires exactly two numbers")
            numbers = [_number(item) for item in value]
            if any(item != item.to_integral_value() for item in numbers):
                raise ValueError("number_pair values must be integers")
            answer = answer_spec.separator.join(str(int(item)) for item in numbers)
        elif kind == "decimal_pair":
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("decimal_pair requires exactly two numbers")
            answer = answer_spec.separator.join(
                _quantize(item, answer_spec.decimals, answer_spec.rounding) for item in value
            )
        else:  # guarded by validate_answer_spec
            raise ValueError(f"Unsupported answer kind: {kind}")
    except (ArithmeticError, InvalidOperation, TypeError, ValueError) as exc:
        _fail(str(exc))

    if not validate_answer(answer, answer_spec):
        _fail(f"Formatted answer does not match {kind} format")
    return answer


def validate_answer(answer: str, spec: AnswerSpec | dict[str, Any]) -> bool:
    """Check format only; semantic correctness belongs to offline evaluation."""
    answer_spec = validate_answer_spec(spec)
    if not isinstance(answer, str) or answer != answer.strip() or not answer:
        return False
    kind = answer_spec.kind
    if kind == "integer":
        return _INTEGER_RE.fullmatch(answer) is not None
    if kind == "fixed_decimal":
        decimals = answer_spec.decimals
        pattern = rf"^-?\d+\.\d{{{decimals}}}$" if decimals else r"^-?\d+$"
        return re.fullmatch(pattern, answer) is not None
    if kind == "yes_no":
        return answer in (answer_spec.allowed_values or ["Yes", "No"])
    if kind in ("last_name", "category"):
        if "\n" in answer or "\r" in answer:
            return False
        return answer in answer_spec.allowed_values if answer_spec.allowed_values else True
    if kind in ("number_pair", "decimal_pair", "ordered_labels"):
        parts = answer.split(answer_spec.separator)
        if len(parts) != answer_spec.item_count or any(not part for part in parts):
            return False
        if kind == "number_pair":
            return all(_INTEGER_RE.fullmatch(part) is not None for part in parts)
        if kind == "decimal_pair":
            decimals = answer_spec.decimals
            pattern = rf"^-?\d+\.\d{{{decimals}}}$" if decimals else r"^-?\d+$"
            return all(re.fullmatch(pattern, part) is not None for part in parts)
        return all(part == part.strip() for part in parts) and (
            not answer_spec.allowed_values or all(part in answer_spec.allowed_values for part in parts)
        )
    return False


def validate_direct_envelope(response_text: str, spec: AnswerSpec | dict[str, Any]) -> str:
    """Parse one strict DIRECT JSON envelope and return its trimmed answer."""
    try:
        payload = strict_json_loads(response_text, parse_float=Decimal)
    except ValueError as exc:
        raise PipelineError(code="FORMAT_INVALID", stage="format", message=f"Invalid direct JSON response: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"answer"}:
        raise PipelineError(code="FORMAT_INVALID", stage="format", message="DIRECT response must contain exactly one 'answer' key")
    if not isinstance(payload["answer"], str):
        raise PipelineError(code="FORMAT_INVALID", stage="format", message="DIRECT answer must be a string")
    answer = payload["answer"].strip()  # The only permitted DIRECT normalization.
    if not validate_answer(answer, spec):
        raise PipelineError(code="FORMAT_INVALID", stage="format", message="DIRECT answer does not match the requested answer format")
    return answer
