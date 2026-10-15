"""Create a provider adapter while resolving credentials before the run starts."""

from __future__ import annotations

import os
from typing import Any

from ..config import LLMConfig
from ..contracts import PipelineError
from ..mock_llm import MockLLMClient
from .base import require_api_key
from .gemini_client import GeminiClient
from .openrouter_client import OpenRouterClient


class LLMBackendFactory:
    @staticmethod
    def create(config: LLMConfig, *, env: dict[str, str] | None = None,
               sdk_clients: dict[str, Any] | None = None):
        if config.provider == "mock":
            return MockLLMClient()
        options = dict(config.provider_options.get(config.provider, {}))
        env_name = options.get("api_key_env", "GEMINI_API_KEY" if config.provider == "gemini"
                               else "OPENROUTER_API_KEY")
        environment = os.environ if env is None else env
        api_key = require_api_key(environment.get(env_name), env_name)
        clients = sdk_clients or {}
        if config.provider == "openrouter":
            return OpenRouterClient(api_key=api_key, options=options,
                                    default_timeout_seconds=config.request_timeout_seconds,
                                    sdk_client=clients.get("openrouter"))
        if config.provider == "gemini":
            return GeminiClient(api_key=api_key, options=options,
                                 default_timeout_seconds=config.request_timeout_seconds,
                                 sdk_client=clients.get("gemini"))
        raise PipelineError(code="INVALID_QUERY", stage="load", message="Unknown LLM provider")
