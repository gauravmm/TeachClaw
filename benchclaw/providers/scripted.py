"""Deterministic fake LLM provider for tests and lecture rehearsals.

The fixture is a YAML or JSON file with a ``responses`` list. Each entry is
one ``LLMResponse`` returned in order; subsequent calls past the end of the
list reuse the last entry. Each response can declare:

```yaml
responses:
  - content: "Hello, world."
    usage_total: 1234         # → response.usage["total_tokens"]
  - content: ""
    tool_calls:
      - name: "search"
        arguments: {q: "value chains"}
        id: "tc1"
  - balloon: 25000             # emit ~25k tokens of filler to force overflow
```

A request to use this provider sets ``provider.name = "scripted"`` and
points ``provider.api_base`` (yes, reused) at the fixture path.
``ScriptedProvider.from_config`` reads the file and constructs the provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from benchclaw.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@dataclass
class ScriptedResponse:
    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage_total: int = 0
    balloon: int = 0  # if > 0, generate that many characters of filler text

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScriptedResponse":
        raw_calls = data.get("tool_calls") or []
        calls = [
            ToolCallRequest(
                id=str(tc.get("id") or f"tc{i}"),
                name=str(tc["name"]),
                arguments=dict(tc.get("arguments") or {}),
            )
            for i, tc in enumerate(raw_calls)
        ]
        return cls(
            content=str(data.get("content") or ""),
            tool_calls=calls,
            finish_reason=str(data.get("finish_reason") or "stop"),
            usage_total=int(data.get("usage_total") or 0),
            balloon=int(data.get("balloon") or 0),
        )

    def to_response(self) -> LLMResponse:
        content = self.content
        if self.balloon > 0:
            filler = ("balloon " * (self.balloon // 8 + 1))[: self.balloon]
            content = (content + "\n" + filler).strip()
        usage: dict[str, int] = {}
        if self.usage_total:
            usage = {
                "prompt_tokens": self.usage_total // 2,
                "completion_tokens": self.usage_total - self.usage_total // 2,
                "total_tokens": self.usage_total,
            }
        return LLMResponse(
            content=content,
            tool_calls=list(self.tool_calls),
            finish_reason=self.finish_reason,
            usage=usage,
        )


class ScriptedProvider(LLMProvider):
    """Replays scripted responses in order. Records each call for assertions."""

    def __init__(self, responses: list[ScriptedResponse]) -> None:
        if not responses:
            raise ValueError("ScriptedProvider needs at least one response in the script")
        self._script = list(responses)
        self._cursor = 0
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def from_fixture(cls, path: Path) -> "ScriptedProvider":
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
        if not isinstance(data, dict) or "responses" not in data:
            raise ValueError(f"{path} must be a mapping with a 'responses' list")
        responses = [ScriptedResponse.from_dict(item) for item in data["responses"]]
        logger.info(f"Loaded {len(responses)} scripted responses from {path}")
        return cls(responses)

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
        self.calls.append({"messages": messages, "tools": tools, "model": model})
        idx = min(self._cursor, len(self._script) - 1)
        self._cursor += 1
        return self._script[idx].to_response()
