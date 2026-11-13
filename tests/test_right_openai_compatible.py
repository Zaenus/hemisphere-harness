"""The OpenAI-compatible right hemisphere against a mock transport."""

import json

import httpx
import pytest

from hemisphere_harness import OpenAICompatibleRight, Proposal, RightHemisphereError, Stakes, WorldState

STATE = WorldState(goal="Resolve the ticket", context="Customer is locked out")

PROPOSAL_JSON = json.dumps(
    {
        "options": [
            {"id": "unlock", "description": "Unlock the account", "rationale": "Lock flag is set"},
            {"id": "ask", "description": "Ask for details"},
        ],
        "rationale": "Probably the lock",
        "stakes": "low",
        "open_ended": False,
    }
)


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def right(handler, **kwargs) -> OpenAICompatibleRight:
    return OpenAICompatibleRight(
        "some-model", transport=httpx.MockTransport(handler), backoff_s=0, **kwargs
    )


async def test_sends_a_minimal_chat_completions_request():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return completion(PROPOSAL_JSON)

    await right(handler, api_key="sk-test", base_url="http://localhost:11434/v1").propose(STATE)

    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == "http://localhost:11434/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert set(body) == {"model", "messages"}  # nothing a compatible server might reject
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert "Resolve the ticket" in body["messages"][1]["content"]


async def test_no_api_key_means_no_authorization_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return completion(PROPOSAL_JSON)

    await right(handler).propose(STATE)

    assert "authorization" not in seen[0].headers


async def test_optional_parameters_are_sent_only_when_configured():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return completion(PROPOSAL_JSON)

    await right(handler, temperature=0.2, json_mode=True).propose(STATE)

    assert seen[0]["temperature"] == 0.2
    assert seen[0]["response_format"] == {"type": "json_object"}


async def test_parses_json_wrapped_in_a_code_fence_and_prose():
    reply = f"Here is my proposal:\n```json\n{PROPOSAL_JSON}\n```\nLet me know."

    proposal = await right(lambda request: completion(reply)).propose(STATE)

    assert isinstance(proposal, Proposal)
    assert [option.id for option in proposal.options] == ["unlock", "ask"]
    assert proposal.stakes is Stakes.LOW


async def test_invalid_reply_is_sent_back_once_with_the_reason():
    requests: list[dict] = []
    one_option = json.dumps({"options": [{"id": "only", "description": "x"}], "rationale": "r", "stakes": "low", "open_ended": False})

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return completion(one_option if len(requests) == 1 else PROPOSAL_JSON)

    proposal = await right(handler).propose(STATE)

    assert len(proposal.options) == 2
    retry = requests[1]["messages"]
    assert [message["role"] for message in retry] == ["system", "user", "assistant", "user"]
    assert "at least 2 items" in retry[3]["content"]


async def test_persistently_malformed_reply_raises():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion("I would rather describe my thoughts in prose.")

    with pytest.raises(RightHemisphereError, match="malformed Proposal 2 times"):
        await right(handler).propose(STATE)
    assert calls == 2


async def test_generate_returns_plain_text_and_does_not_ask_for_json():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return completion("All done.")

    text = await right(handler).generate("Summarise.", STATE)

    assert text == "All done."
    assert "JSON" not in seen[0]["messages"][0]["content"]


async def test_api_failure_raises_right_hemisphere_error():
    with pytest.raises(RightHemisphereError, match="HTTP 400"):
        await right(lambda request: httpx.Response(400, json={"error": {"message": "unknown model"}})).propose(STATE)


async def test_response_without_content_raises():
    with pytest.raises(RightHemisphereError, match="no message content"):
        await right(lambda request: httpx.Response(200, json={"choices": []})).propose(STATE)
