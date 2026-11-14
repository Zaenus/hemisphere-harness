"""Right hemisphere backed by any server that speaks OpenAI's chat-completions protocol.

That covers OpenAI, NVIDIA, xAI, OpenRouter, and local servers such as Ollama, LM Studio,
vLLM and llama.cpp. The named presets for each live in providers.py.
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any, TypedDict

import httpx

from .. import usage
from .._http import post_json
from ..errors import RightHemisphereError
from .llm import ChatMessage, LLMRightHemisphere

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatibleRight(LLMRightHemisphere):
    """Talks to POST {base_url}/chat/completions.

    Only `model` and `messages` are sent by default, which every compatible server
    accepts. Set `json_mode=True` to also send `response_format` where it is supported.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        name: str = "openai-compatible",
        timeout_s: float = 120.0,
        max_retries: int = 2,
        backoff_s: float = 0.5,
        temperature: float | None = None,
        json_mode: bool = False,
        repair_attempts: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.name = name
        self.base_url = base_url
        self.repair_attempts = repair_attempts
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._temperature = temperature
        self._json_mode = json_mode
        self._stream_usage = True  # ask for token counts in streams, until a server refuses
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout_s,
            transport=transport,
        )

    async def complete(self, messages: list[ChatMessage], *, json_reply: bool = False) -> str:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if json_reply and self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        body = await post_json(
            self._client,
            "/chat/completions",
            payload,
            service=f"the right hemisphere's LLM ({self.name})",
            error=RightHemisphereError,
            max_retries=self._max_retries,
            backoff_s=self._backoff_s,
        )
        counted = body.get("usage") if isinstance(body, dict) else None
        usage.record("right", seconds=time.perf_counter() - started, **_tokens(counted))
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RightHemisphereError("the LLM response has no message content") from exc
        if not isinstance(content, str) or not content.strip():
            raise RightHemisphereError("the LLM returned an empty message")
        return content

    async def complete_stream(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        """Server-sent events, as every compatible server streams them. Not retried: the
        harness falls back to `complete` if a stream fails."""
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        started = time.perf_counter()
        counted: Any = None
        try:
            for attempt in range(2):
                if self._stream_usage:
                    payload["stream_options"] = {"include_usage": True}
                else:
                    payload.pop("stream_options", None)
                async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                    if not response.is_success:
                        await response.aread()
                        if self._stream_usage and attempt == 0 and response.status_code in (400, 422):
                            self._stream_usage = False  # a server that does not know stream_options
                            continue
                        raise RightHemisphereError(f"the LLM ({self.name}) refused to stream: HTTP {response.status_code}")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if isinstance(chunk, dict) and chunk.get("usage"):
                            counted = chunk["usage"]
                        try:
                            piece = chunk["choices"][0]["delta"].get("content")
                        except (KeyError, IndexError, TypeError, AttributeError):
                            continue  # keep-alives, usage reports, reasoning: nothing to show
                        if isinstance(piece, str) and piece:
                            yield piece
                    break
        except httpx.HTTPError as exc:
            raise RightHemisphereError(f"the LLM ({self.name}) stream failed: {type(exc).__name__}") from exc
        finally:
            usage.record("right", seconds=time.perf_counter() - started, **_tokens(counted))

    async def aclose(self) -> None:
        await self._client.aclose()


class _Tokens(TypedDict):
    input_tokens: int | None
    output_tokens: int | None


def _tokens(counted: Any) -> _Tokens:
    """Token counts from an OpenAI-style `usage` object, where the server sent one."""
    if not isinstance(counted, dict):
        return {"input_tokens": None, "output_tokens": None}

    def number(key: str) -> int | None:
        value = counted.get(key)
        return value if isinstance(value, int) else None

    return {"input_tokens": number("prompt_tokens"), "output_tokens": number("completion_tokens")}
