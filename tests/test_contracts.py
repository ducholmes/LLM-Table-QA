import json
from datetime import date
from decimal import Decimal
from pathlib import Path
import unittest

from table_qa.contracts import (
    AnswerSpec, CellRef, LLMRequest, LLMResponse, PipelineError, QueryResult,
    RuntimeLimits, StepResult, TableContext, TokenUsage,
)
from table_qa.contract_validation import validate_runtime_limits

FIXTURES = Path(__file__).parent / "fixtures" / "q006"


class DTOTests(unittest.TestCase):
    def test_nested_table_round_trip(self):
        for mode in ("raw", "parsed"):
            with self.subTest(mode=mode):
                wire = json.loads((FIXTURES / f"table_{mode}.json").read_text())
                table = TableContext.from_dict(wire)
                self.assertEqual(table, TableContext.from_dict(json.loads(table.to_json())))
                self.assertEqual(table.sheets[0].name, "cpsaat07")

    def test_defaults_do_not_share_mutable_state(self):
        a = LLMRequest(system_prompt="s", user_prompt="u", model="mock", json_mode="json_object")
        b = LLMRequest(system_prompt="s", user_prompt="u", model="mock", json_mode="json_object")
        a.provider_options["test"] = 1
        self.assertEqual(b.provider_options, {})

    def test_unknown_and_missing_fields_rejected_on_decode(self):
        for bad in ({"sheet": "s", "cell": "E25", "extra": 1}, {"cell": "E25"}):
            with self.subTest(data=bad), self.assertRaises((ValueError, TypeError)):
                CellRef.from_dict(bad)

    def test_boolean_is_not_an_integer(self):
        with self.assertRaises(ValueError):
            TokenUsage.from_dict({"prompt_tokens": True})

    def test_typed_artifacts_preserve_precision_date_and_label(self):
        values = [Decimal("40.600000000000000000001"), date(2024, 2, 29),
                  [Decimal("3.14"), Decimal("2.00")],
                  [{"label": "$s1", "value": Decimal("3.14")}]]
        for value in values:
            with self.subTest(value=value):
                result = StepResult(value=value, evidence=[CellRef(sheet="s", cell="E25")])
                self.assertEqual(result, StepResult.from_dict(json.loads(result.to_json())))

    def test_nonfinite_artifact_is_rejected(self):
        for value in (Decimal("NaN"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                StepResult(value=value, evidence=[]).to_json()

    def test_runtime_collections_and_reserved_tags(self):
        for value in ([1, "2"], [True, 1], [[1]], [{"a": 1}, {"b": 2}],
                      {"$type": "decimal", "value": "40.60"}, {"nested": [1]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                StepResult(value=value, evidence=[]).to_dict()
        for value in ({"$type": "unknown", "value": "x"},
                      {"$type": "decimal", "value": "invalid"},
                      {"$type": "decimal", "value": "3.14", "extra": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                StepResult.from_dict({"value": value, "evidence": []})

    def test_error_has_json_contract_and_exception_behavior(self):
        error = PipelineError(code="CELL_NOT_FOUND", stage="resolve", message="No matching cell", query_id="q")
        with self.assertRaises(PipelineError) as caught:
            raise error
        self.assertIn("CELL_NOT_FOUND", str(caught.exception))
        restored = PipelineError.from_dict(json.loads(error.to_json()))
        self.assertEqual(restored.to_dict(), error.to_dict())

    def test_mock_usage_is_unknown(self):
        response = LLMResponse(text='{"answer":"7668"}', provider="mock", model_requested="mock", model_returned=None, usage=TokenUsage(), llm_elapsed_ms=0.0)
        restored = LLMResponse.from_dict(json.loads(response.to_json()))
        self.assertIsNone(restored.usage.total_tokens)
        self.assertIsNone(restored.actual_cost_usd)

    def test_error_query_result_round_trip(self):
        result = QueryResult(query_id="q", table_id="t.xlsx", status="error", answer=None,
                             format_valid=False, exact_match=None,
                             error=PipelineError(code="API_TIMEOUT", stage="llm", message="Timeout"),
                             trace_path=None, llm_calls=1, planner_repair_calls=0)
        self.assertEqual(result, QueryResult.from_dict(json.loads(result.to_json())))

    def test_default_limits_match_decisions(self):
        limits = validate_runtime_limits(RuntimeLimits())
        self.assertEqual(limits.max_api_attempts, 3)
        self.assertEqual(limits.max_planner_repair_calls, 1)
        self.assertEqual(limits.request_timeout_seconds, 90)

    def test_api_attempt_bounds_and_repeated_plan_repair_are_enforced(self):
        for attempts in (0, 6):
            with self.subTest(max_api_attempts=attempts):
                bad = RuntimeLimits().to_dict()
                bad["max_api_attempts"] = attempts
                with self.assertRaises(PipelineError):
                    validate_runtime_limits(bad)
        bad = RuntimeLimits().to_dict()
        bad["max_planner_repair_calls"] = 2
        with self.assertRaises(PipelineError):
            validate_runtime_limits(bad)


if __name__ == "__main__":
    unittest.main()
