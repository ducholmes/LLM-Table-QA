"""Strict YAML configuration and profile validation for CLI runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import PipelineError


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "mock"
    model: str = "mock"
    json_mode: str = "json_object"
    max_output_tokens: int = 2400
    request_timeout_seconds: int = 90
    max_api_attempts: int = 3
    max_planner_repair_calls: int = 1
    provider_options: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    run_profile: str = "development"
    input_mode: str = "raw"
    answer_mode: str = "plan_tools"
    prompt_version: str = "planner_v1"
    schema_version: str = "plan_v1"
    context_policy: str = "full_table"
    max_workers: int = 1
    query_timeout_seconds: int = 240
    run_timeout_seconds: int = 2400
    max_rss_mb: int = 1024
    expected_query_count: int | None = None
    expected_workbook_count: int | None = None
    submission_policy: str = "fail_run"
    queries: Path = Path("queries.json")
    data_dir: Path = Path(".")
    parsed_dir: Path | None = None
    run_root: Path = Path("runs")
    llm: LLMConfig = field(default_factory=LLMConfig)


_TOP_KEYS = {
    "run_profile", "input_mode", "answer_mode", "prompt_version", "schema_version",
    "context_policy", "max_workers", "query_timeout_seconds", "run_timeout_seconds",
    "max_rss_mb", "expected_query_count", "expected_workbook_count",
    "submission_policy", "queries", "data_dir", "parsed_dir", "run_root", "llm",
}
_LLM_KEYS = {
    "provider", "model", "json_mode", "max_output_tokens", "request_timeout_seconds",
    "max_api_attempts", "max_planner_repair_calls", "provider_options",
}
_PROVIDER_OPTION_KEYS = {
    "mock": set(),
    "gemini": {"api_key_env", "api_surface", "thinking_level", "rate_limit"},
    "openrouter": {"api_key_env", "base_url", "temperature", "site_url", "app_name"},
}


def _invalid(message: str) -> PipelineError:
    return PipelineError(code="INVALID_QUERY", stage="load", message=f"Invalid configuration: {message}")


def validate_config(data: Any) -> AppConfig:
    if not isinstance(data, dict):
        raise _invalid("top-level YAML value must be a mapping")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise _invalid(f"unknown top-level keys: {', '.join(sorted(unknown))}")

    llm_data = data.get("llm", {})
    if not isinstance(llm_data, dict):
        raise _invalid("llm must be a mapping")
    unknown_llm = set(llm_data) - _LLM_KEYS
    if unknown_llm:
        raise _invalid(f"unknown llm keys: {', '.join(sorted(unknown_llm))}")
    try:
        llm = LLMConfig(**llm_data)
    except TypeError as exc:
        raise _invalid("llm has an invalid field") from exc

    if not isinstance(llm.provider, str) or llm.provider not in _PROVIDER_OPTION_KEYS:
        raise _invalid("llm.provider must be mock, gemini, or openrouter")
    if not isinstance(llm.json_mode, str) or llm.json_mode not in {"json_object", "json_schema", "prompt_only"}:
        raise _invalid("llm.json_mode must be json_object, json_schema, or prompt_only")
    if not isinstance(llm.model, str) or not llm.model.strip():
        raise _invalid("llm.model must be a non-empty string")
    if type(llm.max_output_tokens) is not int or llm.max_output_tokens < 1:
        raise _invalid("llm.max_output_tokens must be a positive integer")
    if type(llm.request_timeout_seconds) is not int or llm.request_timeout_seconds < 1:
        raise _invalid("llm.request_timeout_seconds must be a positive integer")
    if type(llm.max_api_attempts) is not int or not 1 <= llm.max_api_attempts <= 5:
        raise _invalid("llm.max_api_attempts must be an integer between 1 and 5")
    if type(llm.max_planner_repair_calls) is not int or llm.max_planner_repair_calls not in (0, 1):
        raise _invalid("llm.max_planner_repair_calls must be 0 or 1")

    options = llm.provider_options
    if not isinstance(options, dict):
        raise _invalid("llm.provider_options must be a mapping")
    if set(options) - set(_PROVIDER_OPTION_KEYS):
        raise _invalid("llm.provider_options contains an unknown provider")
    for provider, provider_options in options.items():
        if not isinstance(provider_options, dict):
            raise _invalid(f"llm.provider_options.{provider} must be a mapping")
        extra = set(provider_options) - _PROVIDER_OPTION_KEYS[provider]
        if extra:
            raise _invalid(f"unknown {provider} options: {', '.join(sorted(extra))}")
        env_name = provider_options.get("api_key_env")
        if env_name is not None and (not isinstance(env_name, str) or not env_name.strip()):
            raise _invalid(f"{provider}.api_key_env must be a non-empty environment variable name")
        if provider == "openrouter":
            for key in ("base_url", "site_url", "app_name"):
                option_value = provider_options.get(key)
                if option_value is not None and (not isinstance(option_value, str) or not option_value.strip()):
                    raise _invalid(f"openrouter.{key} must be a non-empty string")
            temperature = provider_options.get("temperature")
            if temperature is not None and (type(temperature) not in (int, float) or not 0 <= temperature <= 2):
                raise _invalid("openrouter.temperature must be between 0 and 2")
        if provider == "gemini":
            if provider_options.get("api_surface", "generate_content") != "generate_content":
                raise _invalid("gemini.api_surface currently supports only generate_content")
            thinking_level = provider_options.get("thinking_level", "low")
            if not isinstance(thinking_level, str) or thinking_level not in {"low", "medium", "high"}:
                raise _invalid("gemini.thinking_level must be low, medium, or high")
            rate_limit = provider_options.get("rate_limit", {})
            if not isinstance(rate_limit, dict):
                raise _invalid("gemini.rate_limit must be a mapping")
            allowed_rate_keys = {
                "enabled", "requests_per_minute", "input_tokens_per_minute",
                "window_seconds", "token_estimate_safety_factor", "max_wait_seconds",
            }
            if extra_rate := set(rate_limit) - allowed_rate_keys:
                raise _invalid(f"unknown gemini.rate_limit options: {', '.join(sorted(extra_rate))}")
            enabled = rate_limit.get("enabled", False)
            if type(enabled) is not bool:
                raise _invalid("gemini.rate_limit.enabled must be boolean")
            for key in ("requests_per_minute", "input_tokens_per_minute"):
                value = rate_limit.get(key)
                if value is not None and (type(value) is not int or value < 1):
                    raise _invalid(f"gemini.rate_limit.{key} must be a positive integer or null")
            for key in ("window_seconds", "token_estimate_safety_factor", "max_wait_seconds"):
                value = rate_limit.get(key)
                if value is not None and (type(value) not in (int, float) or value <= 0):
                    raise _invalid(f"gemini.rate_limit.{key} must be a positive number")
            if enabled and (rate_limit.get("requests_per_minute") is None
                            and rate_limit.get("input_tokens_per_minute") is None):
                raise _invalid("enabled gemini.rate_limit requires RPM or TPM")
    selected_options = options.get(llm.provider, {})

    for key in ("queries", "data_dir", "run_root"):
        if key in data and not isinstance(data[key], (str, Path)):
            raise _invalid(f"{key} must be a path string")
    if data.get("parsed_dir") is not None and not isinstance(data["parsed_dir"], (str, Path)):
        raise _invalid("parsed_dir must be a path string")

    config = AppConfig(
        run_profile=data.get("run_profile", "development"),
        input_mode=data.get("input_mode", "raw"),
        answer_mode=data.get("answer_mode", "plan_tools"),
        prompt_version=data.get("prompt_version", "planner_v1"),
        schema_version=data.get("schema_version", "plan_v1"),
        context_policy=data.get("context_policy", "full_table"),
        max_workers=data.get("max_workers", 1),
        query_timeout_seconds=data.get("query_timeout_seconds", 240),
        run_timeout_seconds=data.get("run_timeout_seconds", 2400),
        max_rss_mb=data.get("max_rss_mb", 1024),
        expected_query_count=data.get("expected_query_count"),
        expected_workbook_count=data.get("expected_workbook_count"),
        submission_policy=data.get("submission_policy", "fail_run"),
        queries=Path(data.get("queries", "queries.json")),
        data_dir=Path(data.get("data_dir", ".")),
        parsed_dir=Path(data["parsed_dir"]) if data.get("parsed_dir") is not None else None,
        run_root=Path(data.get("run_root", "runs")),
        llm=llm,
    )
    if not isinstance(config.run_profile, str) or config.run_profile not in {"development", "competition", "submission"}:
        raise _invalid("run_profile must be development, competition, or submission")
    if (not isinstance(config.input_mode, str) or config.input_mode not in {"raw", "parsed"}
            or not isinstance(config.answer_mode, str) or config.answer_mode not in {"plan_tools", "direct"}):
        raise _invalid("input_mode or answer_mode is unsupported")
    expected_prompt = "planner_v1" if config.answer_mode == "plan_tools" else "direct_v1"
    if config.prompt_version != expected_prompt or config.schema_version != "plan_v1":
        raise _invalid("unsupported prompt_version or schema_version")
    if config.context_policy != "full_table":
        raise _invalid("context_policy must be full_table")
    if type(config.max_workers) is not int or config.max_workers != 1:
        raise _invalid("max_workers must equal 1; concurrent execution is not implemented yet")
    for field_name in ("query_timeout_seconds", "run_timeout_seconds"):
        value = getattr(config, field_name)
        if type(value) is not int or value < 1:
            raise _invalid(f"{field_name} must be a positive integer")
    if type(config.max_rss_mb) is not int or config.max_rss_mb < 1:
        raise _invalid("max_rss_mb must be a positive integer")
    if (config.expected_query_count is not None
            and (type(config.expected_query_count) is not int or config.expected_query_count < 1)):
        raise _invalid("expected_query_count must be a positive integer when set")
    if (config.expected_workbook_count is not None
            and (type(config.expected_workbook_count) is not int or config.expected_workbook_count < 1)):
        raise _invalid("expected_workbook_count must be a positive integer when set")
    if config.submission_policy not in {"fail_run", "emit_all_rows"}:
        raise _invalid("submission_policy must be fail_run or emit_all_rows")
    if config.run_profile in {"competition", "submission"} and (
        llm.provider != "openrouter" or llm.model != "qwen/qwen3.7-flash"
    ):
        raise _invalid("submission profile requires openrouter/qwen/qwen3.7-flash")
    if config.run_profile in {"competition", "submission"} and llm.max_planner_repair_calls != 1:
        raise _invalid("submission profile requires max_planner_repair_calls=1")
    if config.run_profile in {"competition", "submission"}:
        if (config.input_mode != "raw" or config.answer_mode != "plan_tools"
                or config.prompt_version != "planner_v1" or config.parsed_dir is not None):
            raise _invalid("submission profile requires raw/plan_tools/planner_v1 and no parsed_dir")
        if config.max_rss_mb > 1024:
            raise _invalid("submission profile max_rss_mb cannot exceed 1024")
        if llm.request_timeout_seconds > 90:
            raise _invalid("submission profile request_timeout_seconds cannot exceed 90")
        if config.query_timeout_seconds > 240 or config.run_timeout_seconds > 2400:
            raise _invalid("submission profile timeout exceeds the configured query/run budget")
    return config


def load_config(path: Path) -> AppConfig:
    try:
        import yaml
    except ImportError as exc:
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message="PyYAML is required to load run configuration") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PipelineError(code="INVALID_QUERY", stage="load",
                            message=f"Could not read configuration file: {path}") from exc
    except yaml.YAMLError as exc:
        raise _invalid(f"YAML parse failed ({type(exc).__name__})") from exc
    return validate_config(data)
