"""One POST-with-retries helper, shared by the Jev client and the OpenAI-compatible client."""

import asyncio
import random
from typing import Any

import httpx

from .errors import HarnessError

MAX_RETRY_AFTER_S = 30.0


async def post_json(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
    *,
    service: str,
    error: type[HarnessError],
    max_retries: int = 2,
    backoff_s: float = 0.5,
) -> dict[str, Any]:
    """POST `payload` and return the JSON object in the response.

    Timeouts, connection failures, 408, 429 and 5xx are retried with exponential backoff,
    honouring a numeric Retry-After. Any other 4xx fails at once: retrying a request the
    server has rejected will not change its mind. Every failure is raised as `error`.
    """
    problem = "no attempt made"
    for attempt in range(max_retries + 1):
        retry_after: float | None = None
        limited = False
        try:
            response = await client.post(path, json=payload)
        except httpx.TransportError as exc:
            problem = f"{type(exc).__name__}: {exc}".rstrip(": ")
        else:
            if response.is_success:
                return _json_object(response, service, error)
            problem = f"HTTP {response.status_code}: {_detail(response)}"
            if not _retryable(response.status_code):
                hint = " (check the API key)" if response.status_code in (401, 403) else ""
                raise error(f"{service} rejected the request{hint}: {problem}")
            retry_after = _retry_after(response)
            limited = response.status_code == 429

        if attempt < max_retries:
            # A rate limit lifts in seconds, not milliseconds: back off from RATE_LIMIT_BACKOFF_S.
            base = max(backoff_s, RATE_LIMIT_BACKOFF_S) if limited else backoff_s
            delay = base * 2**attempt * (1 + random.random() / 4)
            await asyncio.sleep(delay if retry_after is None else retry_after)

    raise error(f"{service} unavailable after {max_retries + 1} attempts: {problem}")


RATE_LIMIT_BACKOFF_S = 5.0
"""The first wait after a 429 without a Retry-After header; it doubles on each retry."""


def _retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500


def _retry_after(response: httpx.Response) -> float | None:
    try:
        seconds = float(response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)


def _json_object(
    response: httpx.Response, service: str, error: type[HarnessError]
) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise error(f"{service} returned a body that is not JSON") from exc
    if not isinstance(body, dict):
        raise error(f"{service} returned JSON that is not an object")
    return body


def _detail(response: httpx.Response) -> str:
    """Pull a readable message out of an error body, whatever shape the server used."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300].strip() or response.reason_phrase
    if isinstance(body, dict):
        detail = body.get("detail", body.get("error", body))
        if isinstance(detail, dict) and "message" in detail:
            detail = detail["message"]
        return str(detail)[:300]
    return str(body)[:300]
