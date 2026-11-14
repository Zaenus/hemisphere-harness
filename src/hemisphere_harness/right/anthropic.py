"""Right hemisphere backed by Claude, through Anthropic's official SDK.

Needs the "anthropic" extra: pip install "hemisphere-harness[anthropic]".

    RIGHT_PROVIDER=anthropic
    RIGHT_MODEL=claude-opus-5-5
    ANTHROPIC_API_KEY=...          (or an `ant auth login` profile)

Every request is streamed, which keeps long replies clear of HTTP timeouts and lets the
answer appear as it is written. Thinking is left to the model, which runs adaptive
thinking by default; RIGHT_EFFORT sets how hard it works (low, medium, high, xhigh, max).
"""

import time
from collections.abc import AsyncIterator
from typing import Any

from .. import usage
from ..errors import ConfigError, RightHemisphereError
from .llm import ChatMessage, LLMRightHemisphere

MAX_TOKENS = 64_000

# On these models a request the safety classifiers decline is re-run, inside the same
# call, on a fallback model the API chooses by the refusal's category.
FALLBACK_MODELS = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicRight(LLMRightHemisphere):
    """Talks to Claude through `anthropic.AsyncAnthropic`. One method, `complete`, as for any LLM."""

    name = "anthropic"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        timeout_s: float = 600.0,
        repair_attempts: int = 1,
        client: Any | None = None,
    ) -> None:
        try:
            import anthropic
        except ImportError:
            raise ConfigError('Claude needs the "anthropic" extra: pip install "hemisphere-harness[anthropic]"') from None
        self._anthropic = anthropic
        self.model = model
        self.base_url = base_url or "https://api.anthropic.com"
        self.repair_attempts = repair_attempts
        self._effort = effort
        # Without an explicit key the SDK finds credentials itself: ANTHROPIC_API_KEY,
        # ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile.
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=timeout_s)

    async def complete(self, messages: list[ChatMessage], *, json_reply: bool = False) -> str:
        pieces = [piece async for piece in self.complete_stream(messages)]
        text = "".join(pieces)
        if not text.strip():
            raise RightHemisphereError(f"Claude ({self.model}) returned no text")
        return text

    async def complete_stream(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        anthropic = self._anthropic
        started = time.perf_counter()
        try:
            async with self._stream(messages) as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
            used = getattr(final, "usage", None)
            usage.record(
                "right",
                seconds=time.perf_counter() - started,
                input_tokens=sum(getattr(used, k, None) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
                output_tokens=getattr(used, "output_tokens", None),
            )
        except anthropic.APIConnectionError as exc:
            raise RightHemisphereError(f"Claude ({self.model}) could not be reached: {type(exc).__name__}") from exc
        except anthropic.APIStatusError as exc:
            raise RightHemisphereError(f"Claude ({self.model}) rejected the request: HTTP {exc.status_code}: {exc.message}") from exc
        if final.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            category = f" ({details.category})" if details is not None and getattr(details, "category", None) else ""
            raise RightHemisphereError(f"Claude ({self.model}) declined to answer{category}")

    def _stream(self, messages: list[ChatMessage]) -> Any:
        # The harness's messages are OpenAI-shaped: system turns become Claude's `system`.
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"],
        }
        if system:
            params["system"] = system
        if self._effort:
            params["output_config"] = {"effort": self._effort}
        if self.model in FALLBACK_MODELS:
            return self._client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **params)
        return self._client.messages.stream(**params)

    async def aclose(self) -> None:
        await self._client.close()
