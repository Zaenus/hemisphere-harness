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


# --- Local reasoning models that leave their thinking in the reply -----------------------

THINKING = '<think>The shape wants {"options": [...]}; two options, then.</think>\n'


@pytest.mark.parametrize(
    "reply",
    [
        THINKING + PROPOSAL_JSON,
        THINKING.replace("think>", "thinking>") + PROPOSAL_JSON,
        # The template opened the block itself, so only its end is in the reply.
        'two options {"a": then}.</think>\n' + PROPOSAL_JSON,
        # Braces in prose before the object.
        "Using {braces} loosely, here it is: " + PROPOSAL_JSON,
    ],
)
async def test_parses_json_after_thinking_or_stray_braces(reply):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion(reply)

    proposal = await right(handler).propose(STATE)

    assert [option.id for option in proposal.options] == ["unlock", "ask"]
    assert calls == 1  # no repair round needed


async def test_generate_leaves_the_thinking_out_of_the_answer():
    text = await right(lambda request: completion("<think>Keep it short.</think>\n\nAll done.")).generate("Summarise.")

    assert text == "All done."


async def test_a_reply_that_is_all_thinking_is_an_error():
    with pytest.raises(RightHemisphereError, match="all thinking"):
        await right(lambda request: completion("<think>Still deciding")).generate("Summarise.")


def sse(*pieces: str) -> str:
    events = [f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n" for p in pieces]
    return "".join(events) + "data: [DONE]\n\n"


def streaming(*pieces: str) -> OpenAICompatibleRight:
    return right(lambda request: httpx.Response(200, text=sse(*pieces), headers={"content-type": "text/event-stream"}))


async def test_the_stream_drops_thinking_even_when_its_tags_are_split():
    pieces = [p async for p in streaming("<thi", "nk>plan {it}</th", "ink>", "Bitcoin is ", "decentralized <", "3").stream("Answer.")]

    assert "".join(pieces) == "Bitcoin is decentralized <3"


async def test_the_stream_keeps_a_trailing_angle_bracket():
    pieces = [p async for p in streaming("x is ", "<").stream("Answer.")]

    assert "".join(pieces) == "x is <"


async def test_thinking_that_leaks_into_the_stream_is_withdrawn():
    # The harness withdraws a failed stream and generates the answer whole.
    with pytest.raises(RightHemisphereError, match="thinking reached the answer stream"):
        [p async for p in streaming("weighing it ", "up</think>", "The answer.").stream("Answer.")]
