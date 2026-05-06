"""LiteLLM provider — OpenRouter (cloud gateway) or vLLM (local OpenAI-compatible).

Adding a third backend is just another entry in ``_BACKENDS``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import litellm
from litellm import acompletion
from loguru import logger

from teachclaw.config import ProviderConfig

from .base import LLMProvider, LLMResponse, ToolCallRequest


@dataclass(frozen=True)
class _Backend:
    env_key: str
    litellm_provider: str
    default_api_base: str = ""


_BACKENDS: dict[str, _Backend] = {
    "openrouter": _Backend(
        env_key="OPENROUTER_API_KEY",
        litellm_provider="openrouter",
        default_api_base="https://openrouter.ai/api/v1",
    ),
    "vllm": _Backend(
        env_key="HOSTED_VLLM_API_KEY",
        litellm_provider="hosted_vllm",
    ),
}


class LiteLLMProvider(LLMProvider):
    """Backed by litellm; routes to one of ``_BACKENDS`` based on ``ProviderConfig.name``."""

    def __init__(
        self,
        p: ProviderConfig,
        default_model: str = "openrouter/anthropic/claude-opus-4.5",
    ):
        super().__init__()
        self.default_model = default_model
        self._config = p
        if p.name not in _BACKENDS:
            raise RuntimeError(
                f"Unknown provider {p.name!r}. Supported: {', '.join(sorted(_BACKENDS))}."
            )
        self._backend = _BACKENDS[p.name]

        if not p.api_key:
            raise RuntimeError("No API key configured (set provider.api_key in config.yaml).")

        self._effective_base = self._config.api_base or self._backend.default_api_base
        os.environ[self._backend.env_key] = self._config.api_key

        litellm.api_base = self._effective_base
        litellm.suppress_debug_info = True
        litellm.drop_params = True

        logger.info(f"Configured LiteLLMProvider with {p.name}.")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        top_p: float | None = None,
        top_k: int | None = None,
        enable_thinking: bool | None = None,
    ) -> LLMResponse:
        assert max_tokens >= 1
        assert temperature >= 0
        model = model or self.default_model

        kwargs: dict[str, Any] = {}
        if top_p is not None:
            kwargs["top_p"] = top_p
        # `top_k` and chat-template flags aren't in the OpenAI schema; vLLM
        # accepts them via `extra_body`. OpenRouter ignores unknown extras.
        extra_body: dict[str, Any] = {}
        if top_k is not None:
            extra_body["top_k"] = top_k
        if enable_thinking is not None:
            extra_body["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
        if extra_body:
            kwargs["extra_body"] = extra_body

        try:
            response = await acompletion(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                api_key=self._config.api_key,
                api_base=self._effective_base or None,
                extra_headers=self._config.extra_headers,
                tools=tools,
                custom_llm_provider=self._backend.litellm_provider,
                **kwargs,
            )
            assert isinstance(response, litellm.ModelResponse)
            return self._parse_response(response)
        except Exception as e:
            return LLMResponse(content=f"Error calling LLM: {e}", finish_reason="error")

    @staticmethod
    def _parse_response(response: litellm.ModelResponse) -> LLMResponse:
        choice = response.choices[0]
        assert isinstance(choice, litellm.Choices)
        message = choice.message

        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                tool_calls.append(
                    ToolCallRequest(
                        id=tc.id,
                        name=tc.function.name or "(no name)",
                        arguments=args,
                    )
                )

        usage: dict[str, int] = {}
        response_usage = getattr(response, "usage", None)
        if response_usage:
            usage = {
                "prompt_tokens": response_usage.prompt_tokens,
                "completion_tokens": response_usage.completion_tokens,
                "total_tokens": response_usage.total_tokens,
            }

        content = message.content if message.content is not None else ""
        if content.startswith("\n\n"):
            content = content.lstrip("\n")  # Qwen workaround

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=getattr(message, "reasoning_content", None),
        )
