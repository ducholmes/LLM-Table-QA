from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from table_qa.contracts import Operator
from table_qa.contract_validation import (
    SCHEMA_NAMES, load_schema, validate_plan, validate_query_context, validate_schema,
)
from table_qa.contracts import PipelineError
from typing import get_args

FIXTURES = Path(__file__).parent / "fixtures" / "q006"


class PlanSchemaTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads((FIXTURES / "plan_raw.json").read_text())
        self.context = validate_query_context(json.loads((FIXTURES / "query_context_raw.json").read_text()))

    def assert_bad(self, plan, code="INVALID_PLAN_SCHEMA"):
        with self.assertRaises(PipelineError) as caught:
            validate_plan(plan, self.context)
        self.assertEqual(caught.exception.code, code)

    def test_all_schemas_are_valid_draft_2020_12(self):
        for name in SCHEMA_NAMES:
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(load_schema(name))

    def test_valid_plan_is_not_mutated_or_shared(self):
        original = deepcopy(self.plan)
        checked = validate_plan(self.plan, self.context)
        checked["steps"][0]["args"]["cell"] = "E26"
        self.assertEqual(self.plan, original)

    def test_return_format_is_small_closed_and_optional(self):
        self.plan["return"]["format"] = {"separator": ", ", "decimal_places": 2}
        validate_plan(self.plan, self.context)
        for bad_format in (
            {"unknown": True}, {"separator": ""}, {"separator": "\n"},
            {"decimal_places": -1}, {"decimal_places": 11}, {"decimal_places": True},
        ):
            with self.subTest(format=bad_format):
                plan = deepcopy(self.plan)
                plan["return"]["format"] = bad_format
                self.assert_bad(plan)

    def test_unknown_operator(self):
        self.plan["steps"][0]["op"] = "exec"
        self.assert_bad(self.plan, "UNKNOWN_OPERATOR")

    def test_extra_keys_and_wrong_args(self):
        mutations = [
            lambda p: p.update(code="print(1)"),
            lambda p: p["steps"][0]["args"].update(row_header="x"),
            lambda p: p["steps"][0]["args"].pop("sheet"),
            lambda p: p["steps"][2]["args"].update(expression="s1-s2"),
            lambda p: p["steps"][0]["args"].update(cell="XFE1"),
            lambda p: p["steps"][0]["args"].update(cell="A1048577"),
            lambda p: p["steps"][2]["args"].update(left="$s1.value"),
            lambda p: p.update(query_id="different_query"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                plan = deepcopy(self.plan)
                mutate(plan)
                self.assert_bad(plan)

    def test_duplicate_self_forward_missing_and_return_refs(self):
        for mutate in (
            lambda p: p["steps"][1].update(id="s1"),
            lambda p: p["steps"][2]["args"].update(left="$s3"),
            lambda p: p["steps"][2]["args"].update(left="$s4"),
            lambda p: p["return"].update(ref="$s99"),
        ):
            with self.subTest(mutation=mutate):
                plan = deepcopy(self.plan)
                mutate(plan)
                self.assert_bad(plan)

    def test_limit_steps(self):
        self.plan["steps"] = [{"id": f"s{i}", "op": "lookup_cell", "args": {"sheet": "s", "cell": "A1"}} for i in range(1, 22)]
        self.assert_bad(self.plan)

    def test_raw_rejects_semantic_selectors(self):
        semantic = json.loads((FIXTURES / "plan_parsed.json").read_text())
        self.assert_bad(semantic, "UNSUPPORTED_SELECTOR")

    def test_nested_refs_and_literal_escape(self):
        self.plan["steps"].append({"id": "s4", "op": "collect", "args": {"items": [{"label": {"literal": "$s99"}, "value": "$s3"}]}})
        self.plan["return"]["ref"] = "$s4"
        validate_plan(self.plan, self.context)
        self.plan["steps"][-1]["args"]["items"][0]["value"] = "$s99"
        self.assert_bad(self.plan)

    def test_derive_absolute_has_one_input_and_arithmetic_derives_keep_two(self):
        self.plan = {
            "version": "plan_v1", "query_id": self.context.query_id, "table_id": self.context.table_id,
            "steps": [
                {"id": "s1", "op": "collect", "args": {"items": [
                    {"label": "negative", "change": -6}, {"label": "positive", "change": 4}]}},
                {"id": "s2", "op": "derive", "args": {"source": "$s1", "field": "magnitude",
                                                              "fn": "absolute", "inputs": ["change"]}},
                {"id": "s3", "op": "argmax", "args": {"source": "$s2", "by": "magnitude"}},
                {"id": "s4", "op": "project", "args": {"source": "$s3", "field": "label", "single": True}},
            ], "return": {"ref": "$s4"},
        }
        validate_plan(self.plan, self.context)
        self.plan["steps"][1]["args"]["inputs"] = ["change", "change"]
        self.assert_bad(self.plan)
        self.plan["steps"][1]["args"].update(fn="add", inputs=["change"])
        self.assert_bad(self.plan)

    def test_each_operator_has_a_closed_args_schema(self):
        schema = load_schema("plan_v1")
        samples = {
            "lookup_cell": {"sheet": "s", "cell": "A1"},
            "select_records": {"sheet": "s", "row_range": "3:22", "fields": {"last": {"column": "D"}}},
            "collect": {"items": [{"label": "A", "value": "$s1"}]},
            "derive": {"source": "$s1", "field": "age", "fn": "date_diff_days", "inputs": ["end", "start"]},
            "aggregate": {"source": "$s1", "fn": "mean", "field": "value"},
            "sort": {"source": "$s1", "by": "value", "order": "desc"},
            "take": {"source": "$s1", "offset": 1, "count": 1},
            "project": {"source": "$s1", "field": "label"},
            "compare": {"left": "$s1", "right": "$s2", "predicate": "gte"},
            "filter": {"source": "$s1", "predicates": [{"field": "label", "predicate": "in", "value": ["A", "B"]}]},
            "argmax": {"source": "$s1", "by": "value"},
            "argmin": {"source": "$s1", "by": "value"},
        }
        for name in ("add", "subtract", "abs_diff", "divide"):
            samples[name] = {"left": "$s1", "right": "$s2"}
        self.assertEqual(set(samples), set(get_args(Operator)))
        for op, args in samples.items():
            with self.subTest(op=op):
                step = {"id": "s3", "op": op, "args": args}
                probe = {**schema, "$ref": "#/$defs/step"}
                # The probe validates a single step, retaining shared $defs.
                probe.pop("type")
                probe.pop("required")
                probe.pop("properties")
                probe.pop("additionalProperties")
                validator = Draft202012Validator(probe)
                validator.validate(step)
                bad = deepcopy(step)
                bad["args"]["unexpected"] = True
                self.assertFalse(validator.is_valid(bad))

    def test_nonfinite_operands_rejected(self):
        self.plan["steps"][2]["args"]["left"] = float("nan")
        self.assert_bad(self.plan)


if __name__ == "__main__":
    unittest.main()
