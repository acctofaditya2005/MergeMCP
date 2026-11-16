"""LLM providers behind one small interface.

The agent loop keeps a provider-neutral transcript (`Turn` objects). Each provider
converts that transcript to its own wire format on every call, so adding a provider
means writing one `complete` method and nothing else changes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Turn:
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int = 0
    output_tokens: int = 0


class Provider(Protocol):
    name: str

    async def complete(
        self, system: str, turns: list[Turn], tools: list[ToolSpec]
    ) -> LLMResponse: ...


class ProviderError(Exception):
    pass


class AnthropicProvider:
    """Anthropic Messages API. Needs `pip install merge-mcp[anthropic]`."""

    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float | None = None,
        client: Any | None = None,
    ):
        if client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise ProviderError("Install the 'anthropic' extra to use this provider.") from exc
            client = AsyncAnthropic()
        self._client = client
        self.model = model
        self.name = f"anthropic:{model}"
        self.max_tokens = max_tokens
        self.temperature = temperature

    @staticmethod
    def to_messages(turns: list[Turn]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for t in turns:
            if t.role == "user":
                messages.append({"role": "user", "content": t.text})
            elif t.role == "assistant":
                blocks: list[dict[str, Any]] = [{"type": "text", "text": t.text}] if t.text else []
                blocks += [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in t.tool_calls
                ]
                messages.append({"role": "assistant", "content": blocks})
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": r.call_id,
                                "content": r.content,
                                "is_error": r.is_error,
                            }
                            for r in t.results
                        ],
                    }
                )
        return messages

    async def complete(self, system: str, turns: list[Turn], tools: list[ToolSpec]) -> LLMResponse:
        kwargs: dict[str, Any] = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=self.to_messages(turns),
            tools=[
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ],
            **kwargs,
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [
            ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"
        ]
        return LLMResponse(text, calls, resp.usage.input_tokens, resp.usage.output_tokens)


class OpenAICompatProvider:
    """Any server that speaks the OpenAI chat-completions format, including local models
    (Ollama, vLLM, LM Studio). Set the base URL to point at it."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.name = f"openai-compat:{model}"
        self.temperature = temperature
        base = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._http = httpx.AsyncClient(
            base_url=base.rstrip("/"),
            timeout=300.0,
            transport=transport,
            headers={"Authorization": f"Bearer {key}"} if key else {},
        )

    @staticmethod
    def to_messages(system: str, turns: list[Turn]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for t in turns:
            if t.role == "user":
                messages.append({"role": "user", "content": t.text})
            elif t.role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": t.text or None}
                if t.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in t.tool_calls
                    ]
                messages.append(msg)
            else:
                messages += [
                    {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
                    for r in t.results
                ]
        return messages

    async def complete(self, system: str, turns: list[Turn], tools: list[ToolSpec]) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self.to_messages(system, turns),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        resp = await self._http.post("/chat/completions", json=payload)
        if resp.status_code >= 400:
            raise ProviderError(f"LLM API returned {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        message = data["choices"][0]["message"]
        calls = []
        for c in message.get("tool_calls") or []:
            raw = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"_unparsed_arguments": raw}
            calls.append(ToolCall(c["id"], c["function"]["name"], args))
        usage = data.get("usage") or {}
        return LLMResponse(
            message.get("content") or "",
            calls,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )


class ScriptedProvider:
    """Replays fixed responses. Makes the agent loop testable with no network and no cost."""

    name = "scripted"

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.seen: list[list[Turn]] = []

    async def complete(self, system: str, turns: list[Turn], tools: list[ToolSpec]) -> LLMResponse:
        self.seen.append(list(turns))
        if not self._responses:
            return LLMResponse("Nothing left to do.", [])
        return self._responses.pop(0)
