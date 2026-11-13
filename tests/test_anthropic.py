"""The Claude right hemisphere, against Anthropic's SDK with a mock HTTP transport. No network."""

import json

import httpx2
import pytest

from hemisphere_harness import ConfigError, RightHemisphereError, Settings, build_right
from hemisphere_harness.right.anthropic import FALLBACK_BETA, AnthropicRight


def events(*texts, stop_reason="end_turn", model="claude-opus-5-5"):
    def event(name, data):
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    body = event("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}})  # fmt: skip
    body += event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    for text in texts:
        body += event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
    body += event("content_block_stop", {"type": "content_block_stop", "index": 0})
    body += event("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 5}})
    body += event("message_stop", {"type": "message_stop"})
    return body


def claude(model, handler, **kwargs):
    import anthropic

    client = anthropic.AsyncAnthropic(api_key="test-key", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return AnthropicRight(model, client=client, **kwargs)


def sse(body):
    return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})


MESSAGES = [{"role": "system", "content": "You are the right hemisphere."}, {"role": "user", "content": "Answer."}]


async def test_claude_streams_with_its_system_prompt_and_the_refusal_fallback_on():
    sent = []

    def handler(request):
        sent.append(request)
        return sse(events("Bitcoin ", "is ", "decentralized."))

    right = claude("claude-opus-5-5", handler, effort="high")
    pieces = [piece async for piece in right.complete_stream(MESSAGES)]
    await right.aclose()

    assert pieces == ["Bitcoin ", "is ", "decentralized."]
    request = sent[0]
    body = json.loads(request.content)
    assert request.url.path == "/v1/messages"
    assert body["system"] == "You are the right hemisphere."
    assert body["messages"] == [{"role": "user", "content": "Answer."}]
    assert body["stream"] is True
    assert body["output_config"] == {"effort": "high"}
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in request.headers["anthropic-beta"]
    assert "temperature" not in body and "thinking" not in body  # the model's own defaults


async def test_older_models_get_no_fallback_and_complete_joins_the_stream():
    sent = []

    def handler(request):
        sent.append(request)
        return sse(events("one ", "two", model="claude-sonnet-4-6"))

    right = claude("claude-sonnet-4-6", handler)
    assert await right.complete(MESSAGES) == "one two"
    await right.aclose()
    body = json.loads(sent[0].content)
    assert "fallbacks" not in body and "anthropic-beta" not in sent[0].headers


async def test_a_refusal_and_an_api_error_become_right_hemisphere_errors():
    right = claude("claude-opus-5-5", lambda request: sse(events(stop_reason="refusal")))
    with pytest.raises(RightHemisphereError, match="declined to answer"):
        await right.complete(MESSAGES)
    await right.aclose()

    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad model"}}
    right = claude("claude-opus-5-5", lambda request: httpx2.Response(400, json=error))
    with pytest.raises(RightHemisphereError, match="HTTP 400"):
        await right.complete(MESSAGES)
    await right.aclose()


async def test_the_structured_calls_work_through_claude():
    reply = json.dumps({"situation": "s", "steps": ["one"]})
    right = claude("claude-opus-5-5", lambda request: sse(events(reply)))
    from hemisphere_harness import WorldState

    plan = await right.plan(WorldState(goal="g"))
    await right.aclose()
    assert plan.steps == ["one"]


def test_the_provider_needs_a_model_and_takes_the_key_from_the_settings_file(clean_env, tmp_path):
    with pytest.raises(ConfigError, match="needs RIGHT_MODEL"):
        build_right(Settings(_env_file=None, right_provider="claude"))
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=from-file\n")
    right = build_right(Settings(_env_file=env, right_provider="anthropic", right_model="claude-opus-5-5"))
    assert isinstance(right, AnthropicRight)
    assert right._client.api_key == "from-file"
