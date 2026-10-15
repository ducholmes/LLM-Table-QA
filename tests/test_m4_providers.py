from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from table_qa.config import LLMConfig, load_config, validate_config
from table_qa.contract_validation import load_schema
from table_qa.contracts import LLMRequest, PipelineError, RuntimeLimits
from table_qa.llm.factory import LLMBackendFactory
from table_qa.llm.gemini_client import GeminiClient
from table_qa.llm.openrouter_client import OpenRouterClient
from table_qa.runner import run_offline


class FakeOpenRouterSDK:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.options = []
        self.chat = NS(completions=NS(create=self.create))

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeGeminiSDK:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.models = NS(generate_content=self.generate_content)

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def llm_request(*, provider="openrouter", mode="json_object", timeout=4.5):
    schema = load_schema("plan_v1")
    return LLMRequest(system_prompt="system", user_prompt="user", model=(
        "qwen/qwen3.7-flash" if provider == "openrouter" else "gemini-3.8-flash"),
        json_mode=mode, response_schema=schema, max_output_tokens=123,
        timeout_seconds=timeout)


class M4ProviderTests(unittest.TestCase):
    def test_openrouter_normalizes_usage_sets_timeout_and_requests_json_schema(self):
        response = NS(
            id="req-1", model="qwen/qwen3.7-flash", choices=[NS(
                message=NS(content='{"answer":"ok"}'), finish_reason="stop")],
            usage=NS(prompt_tokens=20, completion_tokens=7, total_tokens=27,
                     prompt_tokens_details=NS(cached_tokens=3), cost=0.0012),
        )
        sdk = FakeOpenRouterSDK(response=response)
        client = OpenRouterClient(api_key="never-logged", sdk_client=sdk,
                                  options={"temperature": 0}, default_timeout_seconds=90)
        result = client.generate(llm_request(mode="json_schema"))

        self.assertEqual(len(sdk.calls), 1)
        self.assertEqual(sdk.options, [{"timeout": 4.5, "max_retries": 0}])
        self.assertEqual(sdk.calls[0]["max_tokens"], 123)
        self.assertEqual(sdk.calls[0]["response_format"]["type"], "json_schema")
        schema_request = sdk.calls[0]["response_format"]["json_schema"]
        self.assertFalse(schema_request["strict"])
        self.assertEqual(sdk.calls[0]["extra_body"],
                         {"provider": {"require_parameters": True}})
        step_variants = schema_request["schema"]["properties"]["steps"]["items"]["anyOf"]
        collect_step = next(item for item in step_variants
                            if item["properties"]["op"]["enum"] == ["collect"])
        self.assertEqual(set(collect_step["properties"]["args"]["properties"]), {"items"})
        for keyword in ("$schema", "$defs", "$ref", "pattern", "const", "allOf"):
            self.assertNotIn(keyword, json.dumps(schema_request["schema"]))
        self.assertEqual(result.provider, "openrouter")
        self.assertEqual(result.model_returned, "qwen/qwen3.7-flash")
        self.assertEqual(result.usage.prompt_tokens, 20)
        self.assertEqual(result.usage.completion_tokens, 7)
        self.assertEqual(result.usage.total_tokens, 27)
        self.assertEqual(result.usage.cached_prompt_tokens, 3)
        self.assertEqual(result.actual_cost_usd, 0.0012)
        self.assertGreaterEqual(result.llm_elapsed_ms, 0)

        nonfinite_sdk = FakeOpenRouterSDK(response=NS(
            id="req-nonfinite", model="qwen/qwen3.7-flash",
            choices=[NS(message=NS(content="{}"), finish_reason="stop")],
            usage=NS(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost=float("inf")),
        ))
        nonfinite_result = OpenRouterClient(api_key="unused", sdk_client=nonfinite_sdk).generate(
            llm_request(mode="json_object"))
        self.assertIsNone(nonfinite_result.actual_cost_usd)

    def test_openrouter_rate_limit_is_one_attempt_and_has_no_response_body_leak(self):
        class RateLimitError(Exception):
            status_code = 429

        secret = "do-not-print-this-key"
        sdk = FakeOpenRouterSDK(error=RateLimitError(secret))
        client = OpenRouterClient(api_key=secret, sdk_client=sdk)
        with self.assertRaises(PipelineError) as caught:
            client.generate(llm_request())
        self.assertEqual(caught.exception.code, "API_RATE_LIMIT")
        self.assertEqual(len(sdk.calls), 1)
        self.assertNotIn(secret, str(caught.exception))

    def test_gemini_timeout_retry_continuation_and_thinking_are_explicit(self):
        response = NS(
            text='{"answer":"ok"}', model_version="gemini-3.8-flash-001", response_id="resp-9",
            usage_metadata=NS(prompt_token_count=31, candidates_token_count=8,
                              total_token_count=42, thoughts_token_count=3,
                              cached_content_token_count=5),
            candidates=[NS(finish_reason="STOP")],
        )
        sdk = FakeGeminiSDK(response=response)
        client = GeminiClient(api_key="never-logged", sdk_client=sdk,
                              options={"thinking_level": "low"})
        result = client.generate(llm_request(provider="gemini", mode="json_schema", timeout=2.25))

        self.assertEqual(len(sdk.calls), 1)
        call = sdk.calls[0]
        self.assertEqual(call["model"], "gemini-3.8-flash")
        self.assertEqual(call["config"]["http_options"]["timeout"], 2250)
        self.assertEqual(call["config"]["http_options"]["retry_options"]["attempts"], 1)
        self.assertIs(call["config"]["automatic_continuation"], False)
        self.assertEqual(call["config"]["thinking_config"]["thinking_level"], "low")
        self.assertEqual(call["config"]["response_mime_type"], "application/json")
        gemini_schema = call["config"]["response_json_schema"]
        for keyword in ("$schema", "$defs", "$ref", "pattern", "const", "allOf"):
            self.assertNotIn(keyword, json.dumps(gemini_schema))
        step_variants = gemini_schema["properties"]["steps"]["items"]["anyOf"]
        collect_step = next(item for item in step_variants
                            if item["properties"]["op"]["enum"] == ["collect"])
        self.assertEqual(set(collect_step["properties"]["args"]["properties"]), {"items"})
        self.assertEqual(result.usage.prompt_tokens, 31)
        self.assertEqual(result.usage.completion_tokens, 8)
        self.assertEqual(result.usage.total_tokens, 42)
        self.assertEqual(result.usage.thinking_tokens, 3)
        self.assertEqual(result.usage.cached_prompt_tokens, 5)
        self.assertIsNone(result.actual_cost_usd)

    def test_provider_error_mapping_does_not_retry_timeout(self):
        class FakeTimeoutError(TimeoutError):
            pass

        sdk = FakeGeminiSDK(error=FakeTimeoutError("private transport detail"))
        client = GeminiClient(api_key="secret", sdk_client=sdk)
        with self.assertRaises(PipelineError) as caught:
            client.generate(llm_request(provider="gemini"))
        self.assertEqual(caught.exception.code, "API_TIMEOUT")
        self.assertEqual(len(sdk.calls), 1)
        self.assertNotIn("secret", str(caught.exception))

    def test_factory_fails_fast_for_missing_key_without_reading_workbooks(self):
        config = LLMConfig(provider="gemini", model="gemini-3.8-flash")
        with self.assertRaises(PipelineError) as caught:
            LLMBackendFactory.create(config, env={})
        self.assertEqual(caught.exception.code, "AUTH_ERROR")
        self.assertIn("GEMINI_API_KEY", caught.exception.message)

    def test_factory_routes_to_both_provider_adapters(self):
        openrouter = LLMBackendFactory.create(
            LLMConfig(provider="openrouter", model="qwen/qwen3.7-flash"),
            env={"OPENROUTER_API_KEY": "fake-openrouter-key"},
            sdk_clients={"openrouter": FakeOpenRouterSDK()},
        )
        gemini = LLMBackendFactory.create(
            LLMConfig(provider="gemini", model="gemini-3.8-flash"),
            env={"GEMINI_API_KEY": "fake-gemini-key"},
            sdk_clients={"gemini": FakeGeminiSDK()},
        )
        self.assertIsInstance(openrouter, OpenRouterClient)
        self.assertIsInstance(gemini, GeminiClient)

    def test_configs_load_and_competition_profile_is_locked(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(load_config(root / "config/default.yaml").llm.provider, "mock")
        self.assertEqual(load_config(root / "config/benchmark_gemini.yaml").llm.provider, "gemini")
        self.assertEqual(load_config(root / "config/benchmark_openrouter.yaml").llm.provider, "openrouter")
        submission = load_config(root / "config/submission.yaml")
        self.assertEqual(submission.run_profile, "competition")
        self.assertEqual(submission.llm.model, "qwen/qwen3.7-flash")
        with self.assertRaises(PipelineError):
            validate_config({"run_profile": "competition", "llm": {
                "provider": "gemini", "model": "gemini-3.8-flash"}})

    def test_fake_openrouter_client_completes_the_existing_q006_pipeline(self):
        root = Path(__file__).resolve().parents[1]
        fixture = root / "tests/fixtures/q006"
        query = (fixture / "query.json").read_text(encoding="utf-8")
        plan = (fixture / "plan_raw.json").read_text(encoding="utf-8")
        with self.subTest(flow="provider-to-csv"), tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            queries_path = temp_root / "queries.json"
            queries_path.write_text(f"[{query}]", encoding="utf-8")
            secret = "test-key-must-not-appear-in-artifacts"
            sdk = FakeOpenRouterSDK(response=NS(
                id="fake-request", model="qwen/qwen3.7-flash", choices=[NS(
                    message=NS(content=plan), finish_reason="stop")],
                usage=NS(prompt_tokens=10, completion_tokens=20, total_tokens=30,
                         prompt_tokens_details=NS(cached_tokens=0), cost=None)))
            client = OpenRouterClient(api_key=secret, sdk_client=sdk)
            llm_config = LLMConfig(provider="openrouter", model="qwen/qwen3.7-flash",
                                   provider_options={"openrouter": {"api_key_env": "OPENROUTER_API_KEY"}})
            run_dir = run_offline(
                queries_path=queries_path, data_dir=root / "data/Public data 1/xlsx_files",
                run_root=temp_root / "runs", client=client, llm_config=llm_config,
                limits=RuntimeLimits(request_timeout_seconds=5, query_timeout_seconds=30,
                                     run_timeout_seconds=60))

            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            summary_text = (run_dir / "summary.json").read_text(encoding="utf-8")
            summary = json.loads(summary_text)
            self.assertEqual(result["answer"], "7668")
            self.assertEqual(summary["run_status"], "ok")
            self.assertEqual(summary["provider"], "openrouter")
            self.assertEqual(summary["model"], "qwen/qwen3.7-flash")
            self.assertEqual(summary["tokens_total"], 30)
            self.assertEqual(summary["api_retries"], 0)
            self.assertEqual(len(sdk.calls), 1)
            self.assertTrue((run_dir / "submission.csv").is_file())
            artifacts = "\n".join(path.read_text(encoding="utf-8") for path in run_dir.iterdir()
                                    if path.is_file() and path.suffix in {".json", ".jsonl", ".csv"})
            self.assertNotIn(secret, artifacts)

    def test_fake_gemini_client_completes_the_existing_q006_pipeline(self):
        root = Path(__file__).resolve().parents[1]
        fixture = root / "tests/fixtures/q006"
        query = (fixture / "query.json").read_text(encoding="utf-8")
        plan = (fixture / "plan_raw.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            queries_path = temp_root / "queries.json"
            queries_path.write_text(f"[{query}]", encoding="utf-8")
            sdk = FakeGeminiSDK(response=NS(
                text=plan, model_version="gemini-3.8-flash-001", response_id="fake-gemini-request",
                usage_metadata=NS(prompt_token_count=11, candidates_token_count=22,
                                  total_token_count=36, thoughts_token_count=3,
                                  cached_content_token_count=0), candidates=[NS(finish_reason="STOP")]))
            client = GeminiClient(api_key="fake-gemini-key", sdk_client=sdk)
            llm_config = LLMConfig(provider="gemini", model="gemini-3.8-flash",
                                   provider_options={"gemini": {"api_key_env": "GEMINI_API_KEY",
                                                                 "thinking_level": "low"}})
            run_dir = run_offline(
                queries_path=queries_path, data_dir=root / "data/Public data 1/xlsx_files",
                run_root=temp_root / "runs", client=client, llm_config=llm_config,
                limits=RuntimeLimits(request_timeout_seconds=5, query_timeout_seconds=30,
                                     run_timeout_seconds=60))
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(result["answer"], "7668")
            self.assertEqual(summary["run_status"], "ok")
            self.assertEqual(summary["provider"], "gemini")
            self.assertEqual(summary["tokens_total"], 36)
            self.assertEqual(summary["thinking_tokens_total"], 3)
            self.assertEqual(summary["api_retries"], 0)
            self.assertEqual(len(sdk.calls), 1)
            self.assertTrue((run_dir / "submission.csv").is_file())

    def test_provider_error_is_logged_once_and_never_enters_plan_repair(self):
        root = Path(__file__).resolve().parents[1]
        query = (root / "tests/fixtures/q006/query.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            queries_path = temp_root / "queries.json"
            queries_path.write_text(f"[{query}]", encoding="utf-8")

            class RateLimitError(Exception):
                status_code = 429

            secret = "rate-limit-response-must-not-leak-key"
            sdk = FakeOpenRouterSDK(error=RateLimitError(secret))
            client = OpenRouterClient(api_key=secret, sdk_client=sdk)
            run_dir = run_offline(
                queries_path=queries_path, data_dir=root / "data/Public data 1/xlsx_files",
                run_root=temp_root / "runs", client=client,
                llm_config=LLMConfig(provider="openrouter", model="qwen/qwen3.7-flash"),
                limits=RuntimeLimits(max_api_attempts=1, request_timeout_seconds=5, query_timeout_seconds=30,
                                     run_timeout_seconds=60))

            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            events = [json.loads(line) for line in (run_dir / "logs.jsonl").read_text().splitlines()]
            error_event = next(event for event in events if event["event"] == "llm_call_error")
            self.assertEqual(result["error"]["code"], "API_RATE_LIMIT")
            self.assertEqual(result["llm_calls"], 1)
            self.assertEqual(result["planner_repair_calls"], 0)
            self.assertEqual(summary["api_retries"], 0)
            self.assertEqual(len(sdk.calls), 1)
            self.assertEqual(error_event["http_attempt"], 1)
            self.assertIn("llm_elapsed_ms", error_event)
            self.assertEqual(error_event["provider"], "openrouter")
            self.assertNotIn(secret, (run_dir / "logs.jsonl").read_text(encoding="utf-8"))
            self.assertFalse((run_dir / "submission.csv").exists())

    def test_transient_server_error_retries_with_backoff_then_succeeds(self):
        root = Path(__file__).resolve().parents[1]
        fixture = root / "tests/fixtures/q006"
        query = (fixture / "query.json").read_text(encoding="utf-8")
        plan = (fixture / "plan_raw.json").read_text(encoding="utf-8")

        class ServerErrorThenSuccess(FakeGeminiSDK):
            def generate_content(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) < 3:
                    raise type("ServerError", (Exception,), {"status_code": 503})("unavailable")
                return NS(
                    text=plan, model_version="gemini-3.8-flash-001", response_id="recovered",
                    usage_metadata=NS(prompt_token_count=11, candidates_token_count=22,
                                      total_token_count=33, thoughts_token_count=0,
                                      cached_content_token_count=0),
                    candidates=[NS(finish_reason="STOP")],
                )

        with tempfile.TemporaryDirectory() as temp, patch("table_qa.runner.time.sleep") as sleep:
            temp_root = Path(temp)
            queries_path = temp_root / "queries.json"
            queries_path.write_text(f"[{query}]", encoding="utf-8")
            sdk = ServerErrorThenSuccess()
            run_dir = run_offline(
                queries_path=queries_path, data_dir=root / "data/Public data 1/xlsx_files",
                run_root=temp_root / "runs", client=GeminiClient(api_key="fake", sdk_client=sdk),
                llm_config=LLMConfig(provider="gemini", model="gemini-3.8-flash",
                                     max_api_attempts=3),
                limits=RuntimeLimits(max_api_attempts=3, request_timeout_seconds=5,
                                     query_timeout_seconds=30, run_timeout_seconds=60),
            )
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            events = [json.loads(line) for line in (run_dir / "logs.jsonl").read_text().splitlines()]
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["answer"], "7668")
            self.assertEqual(len(sdk.calls), 3)
            self.assertEqual(summary["api_retries"], 2)
            self.assertEqual(sleep.call_args_list[0].args, (0.5,))
            self.assertEqual(sleep.call_args_list[1].args, (1.0,))
            self.assertEqual([event["http_attempt"] for event in events
                              if event["event"] == "llm_call_started"], [1, 2, 3])
            self.assertEqual(len([event for event in events
                                  if event["event"] == "llm_retry_scheduled"]), 2)

    def test_failed_repair_marks_aggregate_usage_unknown_but_keeps_known_partial(self):
        root = Path(__file__).resolve().parents[1]
        fixture = root / "tests/fixtures/q006"
        query = (fixture / "query.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            queries_path = temp_root / "queries.json"
            queries_path.write_text(f"[{query}]", encoding="utf-8")

            class FirstThenRateLimit(FakeOpenRouterSDK):
                def create(self, **kwargs):
                    self.calls.append(kwargs)
                    if len(self.calls) == 1:
                        return NS(
                            id="first-call", model="qwen/qwen3.7-flash",
                            choices=[NS(message=NS(content="{}"), finish_reason="stop")],
                            usage=NS(prompt_tokens=10, completion_tokens=4, total_tokens=14,
                                     prompt_tokens_details=NS(cached_tokens=0), cost=0.002),
                        )
                    raise type("RateLimitError", (Exception,), {"status_code": 429})("limited")

            sdk = FirstThenRateLimit()
            run_dir = run_offline(
                queries_path=queries_path, data_dir=root / "data/Public data 1/xlsx_files",
                run_root=temp_root / "runs", client=OpenRouterClient(api_key="fake", sdk_client=sdk),
                llm_config=LLMConfig(provider="openrouter", model="qwen/qwen3.7-flash"),
                limits=RuntimeLimits(max_api_attempts=1, request_timeout_seconds=5, query_timeout_seconds=30,
                                     run_timeout_seconds=60))
            result = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(len(sdk.calls), 2)
            self.assertTrue(result["usage_unknown"])
            self.assertIsNone(result["usage"]["total_tokens"])
            self.assertEqual(result["known_usage_partial"]["total_tokens"], 14)
            self.assertIsNone(result["actual_cost_usd"])
            self.assertEqual(result["actual_cost_usd_known_partial"], 0.002)
            self.assertTrue(summary["usage_unknown"])
            self.assertIsNone(summary["tokens_total"])
            self.assertEqual(summary["known_usage_partial"]["total_tokens"], 14)
            self.assertIsNone(summary["actual_cost_usd"])
            self.assertEqual(summary["actual_cost_usd_known_partial"], 0.002)


if __name__ == "__main__":
    unittest.main()
