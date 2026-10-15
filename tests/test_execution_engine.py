import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from table_qa.contracts import PipelineError
from table_qa.contract_validation import (
    validate_plan,
    validate_query_context,
    validate_table_context,
)
from table_qa.execution import execute_plan
from table_qa.main import main


FIXTURES = Path(__file__).parent / "fixtures" / "q006"


def read_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ExecutionEngineTests(unittest.TestCase):
    def setUp(self):
        self.table = validate_table_context(read_fixture("table_raw.json"))
        self.context = validate_query_context(read_fixture("query_context_raw.json"))

    def execute(self, steps, return_ref):
        plan = {
            "version": "plan_v1",
            "query_id": self.context.query_id,
            "table_id": self.context.table_id,
            "steps": steps,
            "return": {"ref": return_ref},
        }
        return execute_plan(validate_plan(plan, self.context), self.table)

    def test_q006_executes_raw_and_parsed_plans_with_matching_evidence(self):
        expected = read_fixture("expected.json")
        executions = {}
        for mode in ("raw", "parsed"):
            with self.subTest(mode=mode):
                table = validate_table_context(read_fixture(f"table_{mode}.json"))
                context = validate_query_context(read_fixture(f"query_context_{mode}.json"))
                plan = validate_plan(read_fixture(f"plan_{mode}.json"), context)

                execution = execute_plan(plan, table)
                executions[mode] = execution

                self.assertEqual(str(execution.value), expected["answer"])
                self.assertEqual([ref.cell for ref in execution.evidence], ["E25", "E26"])
                self.assertEqual([step.status for step in execution.trace], ["ok", "ok", "ok"])

        self.assertEqual(executions["raw"].value, executions["parsed"].value)
        self.assertEqual(executions["raw"].evidence, executions["parsed"].evidence)

    def test_compare_rejects_text_ordering_but_supports_text_equality(self):
        steps = [
            {"id": "s1", "op": "lookup_cell", "args": {"sheet": "cpsaat07", "cell": "A24"}},
            {"id": "s2", "op": "lookup_cell", "args": {"sheet": "cpsaat07", "cell": "A25"}},
            {"id": "s3", "op": "compare", "args": {"left": "$s1", "right": "$s2", "predicate": "gt"}},
        ]
        with self.assertRaises(PipelineError) as caught:
            self.execute(steps, "$s3")
        self.assertEqual(caught.exception.code, "TYPE_ERROR")
        self.assertEqual(caught.exception.details["trace"][-1]["status"], "error")

        steps[-1]["args"]["predicate"] = "eq"
        self.assertFalse(self.execute(steps, "$s3").value)

    def test_argmax_and_argmin_reject_text_fields(self):
        for op in ("argmax", "argmin"):
            with self.subTest(op=op):
                steps = [
                    {"id": "s1", "op": "collect", "args": {"items": [{"label": "A"}, {"label": "B"}]}},
                    {"id": "s2", "op": op, "args": {"source": "$s1", "by": "label"}},
                ]
                with self.assertRaises(PipelineError) as caught:
                    self.execute(steps, "$s2")
                self.assertEqual(caught.exception.code, "TYPE_ERROR")

    def test_derive_absolute_ranks_negative_and_positive_changes_by_magnitude(self):
        steps = [
            {"id": "s1", "op": "collect", "args": {"items": [
                {"label": "negative", "change": -6}, {"label": "positive", "change": 4}]}},
            {"id": "s2", "op": "derive", "args": {"source": "$s1", "field": "magnitude",
                                                          "fn": "absolute", "inputs": ["change"]}},
            {"id": "s3", "op": "argmax", "args": {"source": "$s2", "by": "magnitude"}},
            {"id": "s4", "op": "project", "args": {"source": "$s3", "field": "label", "single": True}},
        ]
        self.assertEqual(self.execute(steps, "$s4").value, "negative")

    def test_filter_excludes_missing_fields_without_raising_missing_value(self):
        steps = [
            {"id": "s1", "op": "collect", "args": {"items": [
                {"label": "A", "value": 1},
                {"label": None, "value": None},
                {"label": "B", "value": 2},
            ]}},
            {"id": "s2", "op": "filter", "args": {
                "source": "$s1", "predicates": [
                    {"field": "label", "predicate": "ne", "value": ""},
                ],
            }},
            {"id": "s3", "op": "project", "args": {
                "source": "$s2", "field": "label",
            }},
        ]
        self.assertEqual(self.execute(steps, "$s3").value, ["A", "B"])

        steps[1]["args"]["predicates"][0] = {
            "field": "label", "predicate": "eq", "value": None,
        }
        self.assertEqual(self.execute(steps[:2], "$s2").value,
                         [{"label": None, "value": None}])

    def test_cli_exits_nonzero_after_recording_query_failures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "summary.json").write_text('{"failed": 1}\n', encoding="utf-8")
            with patch("table_qa.main.run_offline", return_value=run_dir):
                self.assertEqual(main([]), 1)


if __name__ == "__main__":
    unittest.main()
