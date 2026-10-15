import csv
import json
from decimal import Decimal
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from table_qa.answer_spec import format_answer, validate_answer, validate_direct_envelope
from table_qa.contracts import AnswerSpec, LLMResponse, PipelineError, RuntimeLimits, TokenUsage
from table_qa.mock_llm import MockLLMClient
from table_qa.runner import _write_submission, run_offline
from tests.fixture_plans import plan_for


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "q006"
DATA = ROOT / "data" / "Public data 1"


def spec(kind, *, decimals=None, separator=None, allowed_values=None, item_count=None):
    return AnswerSpec(kind=kind, decimals=decimals, separator=separator,
                      allowed_values=allowed_values, item_count=item_count)


def response(text):
    return LLMResponse(text=text, provider="mock", model_requested="mock", model_returned="mock",
                       usage=TokenUsage(), llm_elapsed_ms=0.5)


class SequenceClient:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return response(self.texts.pop(0))


class FixturePlanClient:
    """Offline-only client that emits hand-authored plans, never label answers."""

    def generate(self, request):
        query_id = re.search(r"^QUERY_ID: ([A-Za-z0-9_.-]+)$", request.user_prompt, re.MULTILINE).group(1)
        table_id = re.search(r"^TABLE_ID: ([^\r\n]+)$", request.user_prompt, re.MULTILINE).group(1)
        return response(json.dumps(plan_for(query_id, table_id), ensure_ascii=False))


class DelayedFixturePlanClient:
    def __init__(self, clock, elapsed, *, invalid_json=False):
        self.clock = clock
        self.elapsed = elapsed
        self.invalid_json = invalid_json
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        self.clock[0] += self.elapsed
        if self.invalid_json:
            return response("not json")
        query_id = re.search(r"^QUERY_ID: ([A-Za-z0-9_.-]+)$", request.user_prompt, re.MULTILINE).group(1)
        table_id = re.search(r"^TABLE_ID: ([^\r\n]+)$", request.user_prompt, re.MULTILINE).group(1)
        return response(json.dumps(plan_for(query_id, table_id), ensure_ascii=False))


def q006_plan(cell1="E25", cell2="E26"):
    return json.dumps({
        "version": "plan_v1", "query_id": "p4_q006", "table_id": "p4_t04.xlsx",
        "steps": [
            {"id": "s1", "op": "lookup_cell", "args": {"sheet": "cpsaat07", "cell": cell1}},
            {"id": "s2", "op": "lookup_cell", "args": {"sheet": "cpsaat07", "cell": cell2}},
            {"id": "s3", "op": "abs_diff", "args": {"left": "$s1", "right": "$s2"}},
        ], "return": {"ref": "$s3"},
    })


class AnswerFormattingTests(unittest.TestCase):
    def test_typed_formats_and_round_half_up(self):
        self.assertEqual(format_answer(7668, spec("integer")), "7668")
        self.assertEqual(format_answer(Decimal("3.1375"), spec("fixed_decimal", decimals=2)), "3.14")
        self.assertEqual(format_answer(True, spec("yes_no", allowed_values=["Yes", "No"])), "Yes")
        self.assertEqual(format_answer("Ngo", spec("last_name")), "Ngo")
        self.assertEqual(format_answer("Airline fares", spec("category")), "Airline fares")
        self.assertEqual(format_answer(["Manufacturing", "Construction", "Agriculture"],
                                       spec("ordered_labels", separator=", ", item_count=3)),
                         "Manufacturing, Construction, Agriculture")
        self.assertEqual(format_answer([34282, 118705], spec("number_pair", separator=", ", item_count=2)),
                         "34282, 118705")
        self.assertEqual(format_answer([Decimal("40.60"), Decimal("37.60")],
                                       spec("decimal_pair", decimals=2, separator=", ", item_count=2)),
                         "40.60, 37.60")

    def test_rejects_fractional_integer_booleans_and_bad_direct_shape(self):
        for value in (Decimal("1.1"), True, "1"):
            with self.subTest(value=value), self.assertRaises(PipelineError):
                format_answer(value, spec("integer"))
        self.assertFalse(validate_answer("7668 units", spec("integer")))
        self.assertEqual(validate_direct_envelope(' {"answer":" 7668 "} ', spec("integer")), "7668")
        for text in ('{"answer":null}', '{"answer":"7668","extra":1}', 'prefix {"answer":"7668"}',
                     '{"answer":"7668 units"}', '{"answer":"7668","answer":"1"}'):
            with self.subTest(text=text), self.assertRaises(PipelineError):
                validate_direct_envelope(text, spec("integer"))


class M3FlowTests(unittest.TestCase):
    def run_q006(self, client, root, labels_path=None, limits=None):
        query = json.loads((FIXTURES / "query.json").read_text(encoding="utf-8"))
        queries_path = root / "queries.json"
        queries_path.write_text(json.dumps([query]), encoding="utf-8")
        return run_offline(queries_path=queries_path, data_dir=DATA / "xlsx_files",
                           answer_mode="plan_tools", run_root=root, client=client,
                           labels_path=labels_path, limits=limits)

    def test_repairs_one_recoverable_execution_error_then_formats_and_writes_csv(self):
        client = SequenceClient(q006_plan("Z99", "E26"), q006_plan())
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.run_q006(client, Path(tmp))
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            summary = json.loads((run_dir / "summary.json").read_text())
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["answer"], "7668")
            self.assertEqual(result["final_evidence"], [{"sheet": "cpsaat07", "cell": "E25"},
                                                         {"sheet": "cpsaat07", "cell": "E26"}])
            self.assertEqual(result["llm_calls"], 2)
            self.assertEqual(result["planner_repair_calls"], 1)
            self.assertEqual(summary["completed"], 1)
            self.assertEqual(summary["failed"], 0)
            self.assertTrue((run_dir / "submission.csv").exists())
            self.assertEqual(len(client.requests), 2)
            self.assertIn("TABLE_CONTEXT:", client.requests[0].user_prompt)
            self.assertIn("TABLE_CONTEXT:", client.requests[1].user_prompt)
            self.assertIn("FAILED_PLAN_JSON", client.requests[1].user_prompt)
            initial_table = client.requests[0].user_prompt.split("TABLE_CONTEXT:\n", 1)[1]
            repaired_table = client.requests[1].user_prompt.split("TABLE_CONTEXT:\n", 1)[1].split(
                "\n\nYour previous plan failed", 1)[0]
            self.assertEqual(initial_table, repaired_table)

    def test_missing_value_triggers_one_plan_repair(self):
        missing_plan = json.dumps({
            "version": "plan_v1", "query_id": "p4_q006", "table_id": "p4_t04.xlsx",
            "steps": [
                {"id": "s1", "op": "select_records", "args": {
                    "sheet": "cpsaat07", "row_range": "7:7",
                    "fields": {"value": {"column": "E", "as_type": "number"}},
                }},
                {"id": "s2", "op": "derive", "args": {
                    "source": "$s1", "field": "magnitude", "fn": "absolute",
                    "inputs": ["value"],
                }},
            ],
            "return": {"ref": "$s2"},
        })
        client = SequenceClient(missing_plan, q006_plan())
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.run_q006(client, Path(tmp))
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            events = [json.loads(line) for line in (run_dir / "logs.jsonl").read_text().splitlines()]
            failed = next(event for event in events if event["event"] == "plan_attempt_failed")
            self.assertEqual(failed["error"]["code"], "MISSING_VALUE")
            self.assertTrue(failed["repair_eligible"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["answer"], "7668")
            self.assertEqual(result["planner_repair_calls"], 1)

    def test_format_error_does_not_trigger_plan_repair_or_emit_csv(self):
        plan = json.dumps({"version": "plan_v1", "query_id": "p4_q006", "table_id": "p4_t04.xlsx",
                           "steps": [{"id": "s1", "op": "collect", "args": {"items": [{"literal": "wrong type"}]}}],
                           "return": {"ref": "$s1"}})
        client = SequenceClient(plan)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.run_q006(client, Path(tmp))
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "FORMAT_INVALID")
            self.assertEqual(result["llm_calls"], 1)
            self.assertEqual(result["planner_repair_calls"], 0)
            self.assertFalse((run_dir / "submission.csv").exists())

    def test_direct_mode_calls_once_and_never_repairs_format_errors(self):
        client = SequenceClient('{"answer":"bad"}')
        with tempfile.TemporaryDirectory() as tmp:
            query = json.loads((FIXTURES / "query.json").read_text(encoding="utf-8"))
            queries_path = Path(tmp) / "queries.json"
            queries_path.write_text(json.dumps([query]), encoding="utf-8")
            run_dir = run_offline(queries_path=queries_path, data_dir=DATA / "xlsx_files",
                                  answer_mode="direct", run_root=Path(tmp), client=client)
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            self.assertEqual(result["error"]["code"], "FORMAT_INVALID")
            self.assertEqual(result["llm_calls"], 1)
            self.assertEqual(result["planner_repair_calls"], 0)
            self.assertFalse((run_dir / "submission.csv").exists())

    def test_direct_success_trims_only_outer_whitespace_and_has_no_evidence(self):
        client = SequenceClient('{"answer":" 7668 "}')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = json.loads((FIXTURES / "query.json").read_text(encoding="utf-8"))
            queries_path = root / "queries.json"
            queries_path.write_text(json.dumps([query]), encoding="utf-8")
            run_dir = run_offline(queries_path=queries_path, data_dir=DATA / "xlsx_files",
                                  answer_mode="direct", run_root=root, client=client)
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["answer"], "7668")
            self.assertEqual(result["final_evidence"], [])
            self.assertEqual(result["llm_calls"], 1)
            self.assertTrue((run_dir / "submission.csv").exists())

    def test_default_mock_is_parseable_but_cannot_create_a_fake_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = json.loads((FIXTURES / "query.json").read_text(encoding="utf-8"))
            queries_path = root / "queries.json"
            queries_path.write_text(json.dumps([query]), encoding="utf-8")
            run_dir = run_offline(queries_path=queries_path, data_dir=DATA / "xlsx_files",
                                  answer_mode="direct", run_root=root, client=MockLLMClient())
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            self.assertEqual(result["error"]["code"], "FORMAT_INVALID")
            self.assertEqual(result["llm_calls"], 1)
            self.assertFalse((run_dir / "submission.csv").exists())

    def test_labels_are_used_after_inference_and_not_added_to_prompt(self):
        client = SequenceClient(q006_plan("Z99", "E26"), q006_plan())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            labels = root / "labels.csv"
            labels.write_text("id,answer\np4_q006,PRIVATE_LABEL_SENTINEL\n", encoding="utf-8")
            run_dir = self.run_q006(client, root, labels_path=labels)
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            self.assertFalse(result["exact_match"])
            self.assertNotIn("PRIVATE_LABEL_SENTINEL", client.requests[0].user_prompt)
            self.assertNotIn("PRIVATE_LABEL_SENTINEL", client.requests[1].user_prompt)

    def test_bad_json_gets_one_plan_repair_but_api_error_does_not(self):
        client = SequenceClient("not json", q006_plan())
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.run_q006(client, Path(tmp))
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["llm_calls"], 2)
            self.assertEqual(result["planner_repair_calls"], 1)
            self.assertIn('"raw_response": "not json"', client.requests[1].user_prompt)

        class TimeoutClient:
            def __init__(self):
                self.calls = 0

            def generate(self, request):
                self.calls += 1
                raise PipelineError(code="API_TIMEOUT", stage="llm", message="timeout")

        timeout_client = TimeoutClient()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.run_q006(timeout_client, Path(tmp))
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            self.assertEqual(result["error"]["code"], "API_TIMEOUT")
            self.assertEqual(result["llm_calls"], 1)
            self.assertEqual(result["planner_repair_calls"], 0)
            self.assertEqual(timeout_client.calls, 1)
            self.assertFalse((run_dir / "submission.csv").exists())

    def test_query_timeout_is_passed_to_client_and_prevents_repair(self):
        clock = [0.0]
        client = DelayedFixturePlanClient(clock, 1.1, invalid_json=True)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("table_qa.runner.time.perf_counter", side_effect=lambda: clock[0]):
                run_dir = self.run_q006(
                    client, Path(tmp), limits=RuntimeLimits(query_timeout_seconds=1, run_timeout_seconds=10))
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            self.assertEqual(result["error"]["code"], "QUERY_TIMEOUT")
            self.assertEqual(result["llm_calls"], 1)
            self.assertEqual(result["planner_repair_calls"], 0)
            self.assertGreater(client.requests[0].timeout_seconds, 0)
            self.assertLessEqual(client.requests[0].timeout_seconds, 1)
            self.assertFalse((run_dir / "submission.csv").exists())

    def test_run_timeout_stops_later_queries_and_blocks_csv(self):
        clock = [0.0]
        client = DelayedFixturePlanClient(clock, 1.1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = json.loads((FIXTURES / "query.json").read_text(encoding="utf-8"))
            second = dict(first, id="p4_q006_second")
            queries_path = root / "queries.json"
            queries_path.write_text(json.dumps([first, second]), encoding="utf-8")
            with patch("table_qa.runner.time.perf_counter", side_effect=lambda: clock[0]):
                run_dir = run_offline(
                    queries_path=queries_path, data_dir=DATA / "xlsx_files",
                    answer_mode="plan_tools", run_root=root, client=client,
                    limits=RuntimeLimits(query_timeout_seconds=10, run_timeout_seconds=1),
                )
            rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual([row["error"]["code"] for row in rows], ["RUN_TIMEOUT", "RUN_TIMEOUT"])
            self.assertEqual([row["llm_calls"] for row in rows], [1, 0])
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(summary["run_status"], "error")
            self.assertEqual(summary["run_error"]["code"], "RUN_TIMEOUT")
            self.assertIsNone(summary["submission_path"])
            self.assertFalse((run_dir / "submission.csv").exists())

    def test_submission_csv_round_trip_preserves_comma_separated_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "submission.csv"
            _write_submission(target, [{"query_id": "p4_q013", "answer": "Manufacturing, Construction, Agriculture"}])
            with target.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows, [{"id": "p4_q013", "answer": "Manufacturing, Construction, Agriculture"}])

    def test_twenty_fixture_plans_run_end_to_end_and_match_offline_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = run_offline(
                queries_path=DATA / "queries.json", data_dir=DATA / "xlsx_files",
                answer_mode="plan_tools", run_root=root, client=FixturePlanClient(),
                labels_path=DATA / "sample_submission.csv",
                max_rss_mb=1024, expected_query_count=20, expected_workbook_count=10,
            )
            rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(len(rows), 20)
            self.assertTrue(all(row["status"] == "ok" for row in rows))
            self.assertTrue(all(row["format_valid"] for row in rows))
            self.assertTrue(all(row["exact_match"] is True for row in rows))
            self.assertEqual(summary["completed"], 20)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["exact_match"], 1.0)
            self.assertEqual(summary["total_llm_calls"], 20)
            self.assertEqual(summary["planner_repair_calls"], 0)
            self.assertEqual(summary["num_queries"], 20)
            self.assertIsNotNone(summary["peak_rss_mb"])
            self.assertLess(summary["peak_rss_mb"], 1024)
            self.assertTrue((run_dir / "submission.csv").exists())
            with (run_dir / "submission.csv").open("r", encoding="utf-8-sig", newline="") as stream:
                submission = list(csv.DictReader(stream))
            self.assertEqual(len(submission), 20)
            self.assertEqual(list(submission[0]), ["id", "answer"])


if __name__ == "__main__":
    unittest.main()
