import csv
import json
from pathlib import Path
import tempfile
import unittest

from table_qa.config import load_config
from table_qa.adapters.raw import RawWorkbookAdapter
from table_qa.contracts import LLMResponse, TokenUsage
from table_qa.evaluate import evaluate_run
from table_qa.planner import planner_system_prompt
from table_qa.runner import run_offline
from table_qa.synthetic import _answers_from_rows, generate_synthetic_corpus
from table_qa.benchmark import _preflight_parsed, experiment_specs, run_benchmark
from tests.test_m3_pipeline import DATA, FIXTURES, response


ROOT = Path(__file__).resolve().parents[1]


class InvalidDirectClient:
    def generate(self, request):
        return LLMResponse(text='{"answer":null}', provider="mock", model_requested="mock",
                           model_returned="mock", usage=TokenUsage(), llm_elapsed_ms=0.5)


class M5BenchmarkTests(unittest.TestCase):
    def test_synthetic_dev_and_holdout_split_by_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            splits = generate_synthetic_corpus(Path(tmp), dev_tables=3, holdout_tables=2, seed=44)
            dev, holdout = splits["dev"], splits["holdout"]
            self.assertEqual(dev.query_count, 12)
            self.assertEqual(holdout.query_count, 8)
            dev_queries = json.loads(dev.queries_path.read_text(encoding="utf-8"))
            holdout_queries = json.loads(holdout.queries_path.read_text(encoding="utf-8"))
            self.assertTrue({row["file_name"] for row in dev_queries}.isdisjoint(
                row["file_name"] for row in holdout_queries))
            self.assertTrue({row["id"] for row in dev_queries}.isdisjoint(
                row["id"] for row in holdout_queries))
            for split in (dev, holdout):
                with split.labels_path.open(encoding="utf-8-sig", newline="") as stream:
                    self.assertEqual(len(list(csv.DictReader(stream))), split.query_count)
                workbook = next(split.data_dir.glob("*.xlsx"))
                table = RawWorkbookAdapter().load(workbook)
                self.assertEqual(table.sheets[0].dimensions, "A1:C5")
                self.assertEqual(table.sheets[0].raw_rows[0].cells[0].value_raw, "Sector")

    def test_synthetic_label_oracle_tracks_metamorphic_table_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            split = generate_synthetic_corpus(Path(tmp), dev_tables=1, holdout_tables=1)["dev"]
            table = RawWorkbookAdapter().load(next(split.data_dir.glob("*.xlsx")))
            rows = [[cell.value_raw for cell in row.cells] for row in table.sheets[0].raw_rows]
            base = _answers_from_rows(rows)

            reordered = [rows[0], *reversed(rows[1:])]
            self.assertEqual(_answers_from_rows(reordered), base)

            renamed = [[row[0].replace("Sector ", "Division "), *row[1:]] for row in rows]
            renamed_answers = _answers_from_rows(renamed)
            self.assertEqual(renamed_answers["q01"], base["q01"].replace("Sector ", "Division "))
            self.assertEqual(renamed_answers["q02"], base["q02"].replace("Sector ", "Division "))

            changed = [list(row) for row in rows]
            changed[1][2] = changed[1][1] + 1000
            self.assertEqual(_answers_from_rows(changed)["q01"], changed[1][0])

            total_row = ["TOTAL", sum(row[1] for row in rows[1:]), sum(row[2] for row in rows[1:])]
            with_total = [*rows, total_row]
            self.assertEqual(_answers_from_rows(with_total), base)

            scaled = [rows[0], *[[row[0], row[1] * 1000, row[2] * 1000] for row in rows[1:]]]
            scaled_answers = _answers_from_rows(scaled)
            self.assertEqual(scaled_answers["q01"], base["q01"])
            self.assertEqual(scaled_answers["q02"], base["q02"])
            self.assertEqual(scaled_answers["q03"], str(int(base["q03"]) * 1000))
            self.assertEqual(scaled_answers["q04"], base["q04"])

    def test_holdout_benchmark_rejects_prompt_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            holdout = generate_synthetic_corpus(root, dev_tables=1, holdout_tables=1)["holdout"]
            config = load_config(ROOT / "config" / "default.yaml")
            with self.assertRaisesRegex(ValueError, "Do not A/B tune prompts on synthetic holdout"):
                run_benchmark(config=config, output_dir=root / "blocked", queries_path=holdout.queries_path,
                              data_dir=holdout.data_dir, allow_mock=True, compare_prompt_b=True)
            self.assertFalse((root / "blocked").exists())

    def test_benchmark_rejects_empty_query_set_before_creating_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queries = root / "queries.json"
            queries.write_text("[]", encoding="utf-8")
            config = load_config(ROOT / "config" / "default.yaml")
            with self.assertRaisesRegex(ValueError, "at least one query"):
                run_benchmark(config=config, output_dir=root / "empty", queries_path=queries,
                              data_dir=DATA / "xlsx_files", allow_mock=True)
            self.assertFalse((root / "empty").exists())

    def test_parsed_preflight_requires_integrated_marker_and_full_source_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = DATA / "xlsx_files" / "p4_t04.xlsx"
            fixture = json.loads((FIXTURES / "table_parsed.json").read_text(encoding="utf-8"))
            parsed_dir = root / "parsed"
            parsed_dir.mkdir()
            (parsed_dir / "p4_t04.json").write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "parsed_integrated=true"):
                _preflight_parsed(parsed_dir, [(None, source)])

            raw_table = RawWorkbookAdapter().load(source)
            complete = raw_table.to_dict()
            complete["metadata"]["parsed_integrated"] = True
            for sheet in complete["sheets"]:
                cells = []
                for row in sheet["raw_rows"]:
                    for cell in row["cells"]:
                        cells.append({**cell, "section_path": [], "row_header_path": [], "column_header_path": []})
                sheet["raw_rows"] = None
                sheet["parsed_cells"] = cells
            path = parsed_dir / "p4_t04.json"
            path.write_text(json.dumps(complete), encoding="utf-8")
            self.assertEqual(_preflight_parsed(parsed_dir, [(None, source)]), 1)

            complete["sheets"][0]["parsed_cells"].pop()
            path.write_text(json.dumps(complete), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cell coverage differs"):
                _preflight_parsed(parsed_dir, [(None, source)])

    def test_experiment_matrix_gates_parsed_and_ab_prompt(self):
        self.assertEqual([row["id"] for row in experiment_specs(include_parsed=False, compare_prompt_b=False)],
                         ["E1", "E2"])
        specs = experiment_specs(include_parsed=True, compare_prompt_b=True)
        self.assertEqual([row["id"] for row in specs], ["E1", "E2", "E2B", "E3", "E4", "E4B"])
        self.assertEqual(planner_system_prompt("B").replace(
            "When a question ranks changes or magnitudes, derive the requested change or\n"
            "percentage for every candidate row first, then rank that derived field; do not\n"
            "rank the original values when the question asks for a derived quantity.\n\n", ""),
            planner_system_prompt("A"))

    def test_failed_labeled_query_counts_in_exact_match_denominator_and_trace_is_embedded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = json.loads((FIXTURES / "query.json").read_text(encoding="utf-8"))
            queries = root / "queries.json"
            queries.write_text(json.dumps([query]), encoding="utf-8")
            labels = root / "labels.csv"
            labels.write_text("id,answer\np4_q006,7668\n", encoding="utf-8")
            run_dir = run_offline(queries_path=queries, data_dir=DATA / "xlsx_files",
                                  answer_mode="direct", run_root=root,
                                  client=InvalidDirectClient(), labels_path=labels)
            report = evaluate_run(run_dir)
            self.assertEqual(report["num_labeled_queries"], 1)
            self.assertEqual(report["exact_matches"], 0)
            self.assertEqual(report["exact_match"], 0.0)
            self.assertEqual(report["failed"], 1)
            trace = report["queries"][0]["trace_events"]
            self.assertTrue(any(event.get("event") == "llm_response" for event in trace))
            self.assertTrue(any(event.get("event") == "query_error" for event in trace))

    def test_mock_benchmark_writes_auditable_e1_e2_ab_artifacts_and_marks_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query = json.loads((FIXTURES / "query.json").read_text(encoding="utf-8"))
            queries = root / "queries.json"
            queries.write_text(json.dumps([query]), encoding="utf-8")
            labels = root / "labels.csv"
            labels.write_text("id,answer\np4_q006,7668\n", encoding="utf-8")
            config = load_config(ROOT / "config" / "default.yaml")
            with self.assertRaisesRegex(ValueError, "--allow-mock"):
                run_benchmark(config=config, output_dir=root / "blocked", queries_path=queries,
                              data_dir=DATA / "xlsx_files", labels_path=labels)
            with self.assertRaisesRegex(ValueError, "public p4 query set"):
                run_benchmark(config=config, output_dir=root / "public_ab", queries_path=queries,
                              data_dir=DATA / "xlsx_files", labels_path=labels,
                              compare_prompt_b=True, allow_mock=True)

            dev = generate_synthetic_corpus(root / "synthetic", dev_tables=1, holdout_tables=1)["dev"]

            benchmark_dir = run_benchmark(
                config=config, output_dir=root / "benchmark", queries_path=dev.queries_path,
                data_dir=dev.data_dir, labels_path=dev.labels_path, compare_prompt_b=True,
                allow_mock=True, dataset_name="synthetic-dev",
            )
            manifest = json.loads((benchmark_dir / "benchmark_manifest.json").read_text(encoding="utf-8"))
            evaluation = json.loads((benchmark_dir / "evaluation.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete")
            self.assertFalse(manifest["accuracy_evidence"])
            self.assertEqual([run["experiment_id"] for run in evaluation["runs"]], ["E1", "E2", "E2B"])
            self.assertTrue(all(run["evaluation_scope"] == "plumbing_only" for run in evaluation["runs"]))
            self.assertTrue(all(run["queries"][0]["trace_events"] for run in evaluation["runs"]))
            self.assertTrue(evaluation["comparison_checks"]["table_view_hash"]["raw"]["complete"])
            self.assertFalse(evaluation["comparison_checks"]["table_view_hash"]["raw"]["mismatches"])
            self.assertTrue((benchmark_dir / "evaluation.csv").is_file())
            summaries = [json.loads((Path(run["run_dir"]) / "summary.json").read_text(encoding="utf-8"))
                         for run in evaluation["runs"]]
            self.assertTrue(all(summary["runtime_config"]["max_planner_repair_calls"] == 1
                                for summary in summaries))
            self.assertEqual(summaries[-1]["runtime_config"]["prompt_version"], "planner_v1_b")


if __name__ == "__main__":
    unittest.main()
