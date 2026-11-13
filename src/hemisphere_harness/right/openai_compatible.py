"""Right hemisphere backed by any server that speaks OpenAI's chat-completions protocol.

That covers OpenAI, NVIDIA, xAI, OpenRouter, and local servers such as Ollama, LM Studio,
vLLM and llama.cpp. The named presets for each live in providers.py.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

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
        body = await post_json(
            self._client,
            "/chat/completions",
            payload,
            service=f"the right hemisphere's LLM ({self.name})",
            error=RightHemisphereError,
            max_retries=self._max_retries,
            backoff_s=self._backoff_s,
        )
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
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if not response.is_success:
                    await response.aread()
                    raise RightHemisphereError(f"the LLM ({self.name}) refused to stream: HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        piece = json.loads(data)["choices"][0]["delta"].get("content")
                    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                        continue  # keep-alives, usage reports, reasoning: nothing to show
                    if isinstance(piece, str) and piece:
                        yield piece
        except httpx.HTTPError as exc:
            raise RightHemisphereError(f"the LLM ({self.name}) stream failed: {type(exc).__name__}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
