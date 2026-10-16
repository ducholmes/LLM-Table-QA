"""Explicit parsing of common human-formatted spreadsheet numbers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re


class MissingNumericValue(ValueError):
    """A cell contains a conventional missing-value marker."""


MISSING_NUMERIC_MARKERS = frozenset({"", "-", "--", "–", "—", "..", "...", "n/a", "na"})
_CURRENCY_SYMBOLS = "$€£¥₹₩₫"
_ISO_CURRENCY = re.compile(r"^[A-Z]{3}(?:\s+|(?=[%s]))" % re.escape(_CURRENCY_SYMBOLS))
_TRAILING_CURRENCY = re.compile(r"(?:\s+[A-Z]{3}|[%s])$" % re.escape(_CURRENCY_SYMBOLS))
_PLAIN_NUMBER = re.compile(r"(?:\d+(?:\.\d+)?|\.\d+)")
_GROUPED_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_SPACE_GROUPED_NUMBER = re.compile(r"\d{1,3}(?: \d{3})+(?:\.\d+)?")


def excel_percent_count(number_format: str | None) -> int:
    """Count active percent tokens, ignoring quoted/escaped format literals."""
    if not number_format:
        return 0
    stripped = re.sub(r'"(?:[^"]|"")*"|\\.|\[[^]]*\]', "", number_format)
    return stripped.count("%")


def parse_numeric_text(value: str) -> Decimal:
    """Parse a finite en-US-style number after an explicit numeric cast.

    Percent text is represented in percentage points: ``18.5%`` becomes
    ``Decimal("18.5")``. Locale-specific decimal commas are intentionally not
    guessed because silently choosing the wrong locale changes magnitudes.
    """
    text = value.replace("\u00a0", " ").replace("\u202f", " ").strip()
    if text.casefold() in MISSING_NUMERIC_MARKERS:
        raise MissingNumericValue("Cell contains a missing-value marker")

    negative_parentheses = text.startswith("(") and text.endswith(")")
    if negative_parentheses:
        text = text[1:-1].strip()
    elif text.startswith("(") or text.endswith(")"):
        raise ValueError("Unbalanced accounting parentheses")

    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    if "%" in text:
        raise ValueError("Percent sign must appear once at the end")

    sign = ""
    if text[:1] in {"+", "-", "−"}:
        sign, text = ("-" if text[0] == "−" else text[0]), text[1:].strip()

    text = _ISO_CURRENCY.sub("", text, count=1).strip()
    if text[:1] in _CURRENCY_SYMBOLS:
        text = text[1:].strip()
    if text[:1] in {"+", "-", "−"} and not sign:
        sign, text = ("-" if text[0] == "−" else text[0]), text[1:].strip()
    text = _TRAILING_CURRENCY.sub("", text).strip()

    if negative_parentheses and sign:
        raise ValueError("Number cannot combine a sign with accounting parentheses")
    if _GROUPED_NUMBER.fullmatch(text):
        normalized = text.replace(",", "")
    elif _SPACE_GROUPED_NUMBER.fullmatch(text):
        normalized = text.replace(" ", "")
    elif _PLAIN_NUMBER.fullmatch(text):
        normalized = text
    else:
        raise ValueError("String is not a supported formatted number")
    try:
        number = Decimal(("-" if negative_parentheses else sign) + normalized)
    except InvalidOperation as exc:
        raise ValueError("String is not a valid decimal number") from exc
    if not number.is_finite():
        raise ValueError("String does not contain a finite number")
    return number


def numeric_unit(value: str | None = None, number_format: str | None = None) -> str | None:
    if (isinstance(value, str) and value.strip().endswith("%")) or excel_percent_count(number_format):
        return "percentage_points"
    return None
