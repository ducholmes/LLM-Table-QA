from __future__ import annotations

import json
from contextlib import redirect_stderr
from dataclasses import asdict
from io import StringIO
import os
import stat
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from table_qa.config import load_config, validate_config
from table_qa.contracts import PipelineError
from table_qa.main import _apply_overrides, build_parser, main
from table_qa.runner import run_offline
from tools.package_submission import build_submission_zip


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "q006"
DATA = ROOT / "data" / "Public data 1"


class M6CompetitionConfigTests(unittest.TestCase):
    def test_submission_profile_locks_paths_modes_and_expected_count(self):
        config = load_config(ROOT / "config" / "submission.yaml")
        self.assertEqual(config.expected_query_count, 20)
        self.assertEqual(config.expected_workbook_count, 10)
        self.assertEqual(config.max_rss_mb, 1024)
        self.assertEqual(config.queries, Path("data/Public data 1/queries.json"))
        self.assertEqual(config.data_dir, Path("data/Public data 1/xlsx_files"))

        over_budget = asdict(config)
        over_budget["run_timeout_seconds"] = 2401
        with self.assertRaisesRegex(PipelineError, "timeout exceeds"):
            validate_config(over_budget)

        data = {
            "run_profile": "competition",
            "input_mode": "raw",
            "answer_mode": "plan_tools",
            "prompt_version": "planner_v1",
            "queries": "elsewhere/queries.json",
            "expected_query_count": 20,
            "expected_workbook_count": 10,
            "llm": {"provider": "openrouter", "model": "qwen/qwen3.7-flash",
                    "max_planner_repair_calls": 1},
        }
        with self.assertRaisesRegex(PipelineError, "fixed submission path"):
            validate_config(data)

    def test_competition_rejects_cli_overrides_before_backend_creation(self):
        args = build_parser().parse_args([
            "--config", str(ROOT / "config" / "submission.yaml"), "--input-mode", "parsed",
        ])
        with self.assertRaisesRegex(PipelineError, "does not accept CLI overrides"):
            _apply_overrides(load_config(args.config), args)

        args = build_parser().parse_args([
            "--config", str(ROOT / "config" / "submission.yaml"), "--prompt-variant", "B",
        ])
        with self.assertRaisesRegex(PipelineError, "prompt_variant"):
            _apply_overrides(load_config(args.config), args)
        args = build_parser().parse_args([
            "--config", str(ROOT / "config" / "submission.yaml"), "--prompt-variant", "A",
        ])
        with self.assertRaisesRegex(PipelineError, "prompt_variant"):
            _apply_overrides(load_config(args.config), args)

    def test_run_rejects_wrong_query_count_before_creating_artifacts(self):
        query = json.loads((FIXTURE / "query.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queries_path = root / "queries.json"
            queries_path.write_text(json.dumps([query]), encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "Expected 20 queries"):
                run_offline(queries_path=queries_path, data_dir=DATA / "xlsx_files",
                            run_root=root / "runs", expected_query_count=20)
            self.assertFalse((root / "runs").exists())

    def test_cli_fails_fast_when_competition_input_paths_are_missing(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            try:
                os.chdir(temp)
                errors = StringIO()
                with patch("table_qa.main.LLMBackendFactory.create", return_value=object()), \
                        redirect_stderr(errors):
                    code = main(["--config", str(ROOT / "config" / "submission.yaml")])
            finally:
                os.chdir(original_cwd)
        self.assertEqual(code, 2)
        self.assertIn("Input file not found", errors.getvalue())

    def test_memory_limit_is_logged_and_prevents_submission(self):
        query = json.loads((FIXTURE / "query.json").read_text(encoding="utf-8"))

        class NeverCalled:
            calls = 0

            def generate(self, _request):
                self.calls += 1
                raise AssertionError("memory guard should run before the model")

        with tempfile.TemporaryDirectory() as temp, patch("table_qa.runner.peak_rss_mb", return_value=1024):
            root = Path(temp)
            queries_path = root / "queries.json"
            queries_path.write_text(json.dumps([query]), encoding="utf-8")
            client = NeverCalled()
            run_dir = run_offline(queries_path=queries_path, data_dir=DATA / "xlsx_files",
                                  run_root=root / "runs", client=client, max_rss_mb=1024)
            result = json.loads((run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(client.calls, 0)
            self.assertEqual(result["error"]["code"], "RESOURCE_LIMIT")
            self.assertEqual(summary["run_status"], "error")
            self.assertFalse((run_dir / "submission.csv").exists())
            self.assertEqual(summary["peak_rss_mb"], 1024)


class M6PackagingTests(unittest.TestCase):
    def test_submission_zip_is_small_and_excludes_data_labels_and_run_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "submission.zip"
            size = build_submission_zip(output)
            self.assertLess(size, 100_000_000)
            self.assertTrue(output.stat().st_mode & stat.S_IROTH)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                run_mode = archive.getinfo("run_submission.sh").external_attr >> 16
                self.assertTrue(run_mode & stat.S_IXUSR)
            self.assertIn("config/submission.yaml", names)
            self.assertIn("requirements-submission.txt", names)
            self.assertIn("src/table_qa/schemas/plan_v1.json", names)
            self.assertIn("src/table_qa/prompts/planner_v1.txt", names)
            self.assertIn("run_submission.sh", names)
            for source_schema in (ROOT / "schemas").glob("*.json"):
                packaged_schema = ROOT / "src" / "table_qa" / "schemas" / source_schema.name
                self.assertEqual(source_schema.read_bytes(), packaged_schema.read_bytes())
            self.assertFalse(any(name.startswith(("data/", "runs/", "tests/")) for name in names))
            self.assertFalse(any(name.endswith("sample_submission.csv") or name.endswith(".env")
                                 for name in names))
            with tempfile.TemporaryDirectory() as extracted:
                with zipfile.ZipFile(output) as archive:
                    archive.extractall(extracted)
                package_root = Path(extracted)
                package_env = {**os.environ, "PYTHONPATH": str(package_root / "src")}
                imported = subprocess.run(
                    [sys.executable, "-c",
                     "from table_qa.contract_validation import load_schema; "
                     "assert load_schema('plan_v1')['$id'].endswith('/plan_v1.json')"],
                    cwd=package_root, env=package_env, capture_output=True, text=True, check=False,
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)
                package_env.pop("OPENROUTER_API_KEY", None)
                entrypoint = subprocess.run(
                    ["sh", "run_submission.sh"], cwd=package_root, env=package_env,
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(entrypoint.returncode, 2)
                self.assertIn("AUTH_ERROR: Missing provider credential", entrypoint.stderr)


if __name__ == "__main__":
    unittest.main()
