"""What a run cost: tokens, calls and time per hemisphere, counted per run."""

import json

import httpx

from hemisphere_harness import Harness, HarnessConfig, MockLeftHemisphere
from hemisphere_harness.right.openai_compatible import OpenAICompatibleRight
from hemisphere_harness.usage import Usage, record, tracking

from test_memory import SURE, one_step_right


def chat_reply(content: str, usage: dict | None = None) -> httpx.Response:
    body: dict = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


async def test_the_openai_client_counts_tokens_into_the_current_run_only():
    transport = httpx.MockTransport(lambda request: chat_reply("hi", {"prompt_tokens": 120, "completion_tokens": 30}))
    right = OpenAICompatibleRight("m", transport=transport)
    await right.complete([{"role": "user", "content": "x"}])  # outside a run: counted nowhere
    with tracking() as used:
        await right.complete([{"role": "user", "content": "x"}])
        await right.complete([{"role": "user", "content": "x"}])
    assert (used.right.calls, used.right.input_tokens, used.right.output_tokens) == (2, 240, 60)
    assert used.left.calls == 0
    await right.aclose()


async def test_a_server_without_a_usage_report_still_counts_the_call():
    right = OpenAICompatibleRight("m", transport=httpx.MockTransport(lambda request: chat_reply("hi")))
    with tracking() as used:
        await right.complete([{"role": "user", "content": "x"}])
    assert (used.right.calls, used.right.tokens) == (1, 0)
    await right.aclose()


def sse(*chunks: dict) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


async def test_streams_ask_for_usage_and_stop_asking_a_server_that_refuses():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append("stream_options" in body)
        if "stream_options" in body and len(seen) == 1:
            return httpx.Response(400, json={"error": "unknown field stream_options"})
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo"}}]}))

    right = OpenAICompatibleRight("m", transport=httpx.MockTransport(handler))
    with tracking() as used:
        assert "".join([p async for p in right.complete_stream([{"role": "user", "content": "x"}])]) == "Hello"
        assert "".join([p async for p in right.complete_stream([{"role": "user", "content": "x"}])]) == "Hello"
    assert seen == [True, False, False]
    assert used.right.calls == 2
    await right.aclose()


async def test_a_stream_reports_its_tokens_from_the_final_chunk():
    content = sse({"choices": [{"delta": {"content": "Hi"}}]}, {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 2}})
    right = OpenAICompatibleRight("m", transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)))
    with tracking() as used:
        assert [p async for p in right.complete_stream([{"role": "user", "content": "x"}])] == ["Hi"]
    assert (used.right.input_tokens, used.right.output_tokens) == (50, 2)
    await right.aclose()


async def test_a_run_reports_its_usage_and_cost():
    class Counting(type(one_step_right())):
        async def plan(self, state):
            record("right", seconds=0.5, input_tokens=1_000_000, output_tokens=100_000)
            return await super().plan(state)

    left = MockLeftHemisphere(script=[SURE])
    config = HarnessConfig(right_price_input=3, right_price_output=15, check_answers=False)
    result = await Harness(one_step_right(Counting), left, config=config).run("Do it.")

    assert result.usage.left.calls == 1
    assert result.usage.right.tokens == 1_100_000
    assert result.usage.cost == 3 + 1.5
    end = result.trace[-1]
    assert end.data["usage"]["cost"] == 4.5
    assert "1100.0k tokens · $4.50" in end.details[0]


def test_usages_add_up():
    a, b = Usage(), Usage()
    a.right.input_tokens, b.right.input_tokens, b.cost = 10, 5, 0.25
    a.add(b)
    assert (a.right.input_tokens, a.cost) == (15, 0.25)
