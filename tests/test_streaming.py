"""Streaming the answer as it is written: the client, the harness's fallback, and the web stream."""

import asyncio
import json

import httpx
import pytest

from hemisphere_harness import (
    ChoiceAnswer,
    Harness,
    MockLeftHemisphere,
    OpenAICompatibleRight,
    Option,
    Plan,
    Proposal,
    Reintegration,
    RightHemisphereError,
    ScriptedRightHemisphere,
    Stakes,
)


def sse(*pieces, done=True):
    lines = [": keep-alive", "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"thinking\"}}]}"]
    lines += [f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}" for piece in pieces]
    if done:
        lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


async def test_the_client_streams_only_the_answers_text():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=sse("Bitcoin ", "is ", "decentralized."), headers={"content-type": "text/event-stream"})

    right = OpenAICompatibleRight("m", base_url="https://llm.example/v1", transport=httpx.MockTransport(handler))
    pieces = [piece async for piece in right.stream("Answer.")]
    await right.aclose()
    assert pieces == ["Bitcoin ", "is ", "decentralized."]  # keep-alives and reasoning are skipped
    assert sent[0]["stream"] is True


async def test_a_refused_stream_is_an_error_the_harness_can_fall_back_from():
    right = OpenAICompatibleRight("m", base_url="https://llm.example/v1", transport=httpx.MockTransport(lambda r: httpx.Response(400, json={})))
    with pytest.raises(RightHemisphereError, match="refused to stream: HTTP 400"):
        [piece async for piece in right.stream("Answer.")]
    await right.aclose()


def one_step_right(cls=ScriptedRightHemisphere, **extra):
    return cls(
        plan=Plan(situation="s"),
        proposals=[Proposal(options=[Option(id="a", description="a"), Option(id="b", description="b")], rationale="r", stakes=Stakes.LOW, open_ended=False)],
        reintegrations=[Reintegration(situation="r", goal_met=True)],
        report="The whole answer.",
        **extra,
    )


SURE = [{"select": ChoiceAnswer.from_probabilities({"a": 0.95, "b": 0.05})}]


async def test_a_stream_that_breaks_is_withdrawn_and_the_answer_generated_whole():
    class Breaks(ScriptedRightHemisphere):
        async def stream(self, prompt, state=None):
            yield "The begin"
            raise RightHemisphereError("connection dropped")

    seen = []
    result = await Harness(one_step_right(Breaks), MockLeftHemisphere(script=SURE), on_event=seen.append).run("g")
    live = [event.data for event in seen if event.transient]
    assert live == [{"text": "The begin"}, {"reset": True}]
    assert result.report == "The whole answer."


async def test_nobody_listening_means_no_streaming():
    class Counts(ScriptedRightHemisphere):
        streamed = 0

        async def stream(self, prompt, state=None):
            Counts.streamed += 1
            yield "x"

    await Harness(one_step_right(Counts), MockLeftHemisphere(script=SURE)).run("g")
    assert Counts.streamed == 0


async def test_the_web_stream_carries_the_pieces_but_the_run_does_not_keep_them(clean_env, tmp_path, monkeypatch):
    from test_web import AUTH, client, make_app, runs_module, wait_for_end

    class Pieces(ScriptedRightHemisphere):
        async def plan(self, state):
            await asyncio.sleep(0.3)  # long enough for the page to connect before anything happens
            return await super().plan(state)

        async def stream(self, prompt, state=None):
            for piece in ("The ", "whole ", "answer."):
                yield piece

    monkeypatch.setattr(runs_module, "build_right", lambda s: one_step_right(Pieces))
    monkeypatch.setattr(runs_module, "build_left", lambda s: MockLeftHemisphere(script=SURE))
    clean_env.setenv("RIGHT_MODEL", "anything")
    async with client(make_app(tmp_path)) as http:
        run_id = (await http.post("/api/runs", headers=AUTH, json={"goal": "g"})).json()["id"]
        stream = await http.get(f"/api/runs/{run_id}/events", headers=AUTH)  # connected before it starts
        run = await wait_for_end(http, run_id)

    deltas = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: {\"text\"")]
    assert deltas == [{"text": "The "}, {"text": "whole "}, {"text": "answer."}]
    assert "event: delta" in stream.text
    assert all(not event.get("transient") for event in run["events"])
    assert run["report"] == "The whole answer."
