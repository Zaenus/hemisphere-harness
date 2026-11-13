"""The experimental chat-model left hemisphere, against a mock transport.

The fake servers below answer the way real chat models were observed to: with a
probability for each candidate first token.
"""

import json
import math
from collections.abc import Callable

import httpx
import pytest

from hemisphere_harness import (
    ChoiceAnswer,
    ChoiceQuestion,
    Evaluation,
    ExperimentalLLMLeft,
    ExperimentalWarning,
    Harness,
    Hemisphere,
    LeftHemisphereError,
    NoulAnswer,
    NoulQuestion,
    Option,
    Plan,
    Proposal,
    Reintegration,
    ScoreAnswer,
    ScoreQuestion,
    ScriptedRightHemisphere,
    Stakes,
    Verdict,
)

TokenProbabilities = dict[str, float]

TWO = ChoiceQuestion(instructions="Which?", options={"close": "Close the ticket", "wait": "Keep it open"})
THREE = ChoiceQuestion(instructions="Which?", options={"unlock": "Unlock the account", "reset": "Send a reset link", "ask": None})


def reply(tokens: TokenProbabilities) -> httpx.Response:
    top = [{"token": token, "logprob": math.log(p)} for token, p in tokens.items() if p > 0]
    first = {"token": max(tokens, key=tokens.__getitem__), "top_logprobs": top}
    return httpx.Response(200, json={"choices": [{"message": {"content": first["token"]}, "logprobs": {"content": [first]}}]})


def user_prompt(request: httpx.Request) -> str:
    return json.loads(request.content)["messages"][1]["content"]


def labelled_options(request: httpx.Request) -> dict[str, str]:
    """{"A": "close", "B": "wait"} as the model was shown them."""
    block = user_prompt(request).split("OPTIONS\n")[1].split("\n\n")[0]
    return {line[0]: line[3:].split(" - ")[0] for line in block.splitlines()}


def left(answer: Callable[[httpx.Request], TokenProbabilities], **kwargs) -> tuple[ExperimentalLLMLeft, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(answer(request))

    with pytest.warns(ExperimentalWarning):
        model = ExperimentalLLMLeft("some-chat-model", transport=httpx.MockTransport(handler), backoff_s=0, **kwargs)
    return model, seen


def always_b(request: httpx.Request) -> TokenProbabilities:
    """A model that likes the letter B, whatever B stands for. Observed with a real one."""
    return {"B": 0.99, "A": 0.01}


def prefers(option: str, p: float = 0.9) -> Callable[[httpx.Request], TokenProbabilities]:
    """A model that reads the options and wants `option`, wherever it is listed."""

    def answer(request: httpx.Request) -> TokenProbabilities:
        labels = labelled_options(request)
        others = (1 - p) / (len(labels) - 1)
        return {label: p if key == option else others for label, key in labels.items()}

    return answer


# --- Choice ------------------------------------------------------------------------------


async def test_a_model_that_only_likes_a_letter_is_averaged_into_a_tie():
    model, seen = left(always_b)

    answer = (await model.decide("state", {"select": TWO})).answers["select"]

    assert isinstance(answer, ChoiceAnswer)
    assert answer.probabilities == pytest.approx({"close": 0.5, "wait": 0.5})
    assert answer.confidence == pytest.approx(0.0)
    assert [labelled_options(request) for request in seen] == [
        {"A": "close", "B": "wait"},
        {"A": "wait", "B": "close"},
    ]


async def test_a_model_that_reads_the_options_keeps_its_answer_under_every_rotation():
    model, seen = left(prefers("unlock"))

    answer = (await model.decide("state", {"select": THREE})).answers["select"]

    assert isinstance(answer, ChoiceAnswer)
    assert answer.choice == "unlock"
    assert answer.probability == pytest.approx(0.9)
    assert len(seen) == 3  # every option took a turn under every letter
    assert {labels["A"] for labels in map(labelled_options, seen)} == {"unlock", "reset", "ask"}


async def test_rotations_are_capped_for_long_option_lists():
    many = ChoiceQuestion(instructions="Which?", options={f"option_{i}": None for i in range(6)})
    model, seen = left(prefers("option_3"), max_rotations=4)

    answer = (await model.decide("state", {"select": many})).answers["select"]

    assert len(seen) == 4
    assert answer.choice == "option_3"  # type: ignore[union-attr]


async def test_more_options_than_letters_is_refused():
    huge = ChoiceQuestion(instructions="Which?", options={f"o{i}": None for i in range(27)})
    model, seen = left(always_b)

    with pytest.raises(LeftHemisphereError, match="A to Z"):
        await model.decide("state", {"select": huge})
    assert seen == []


async def test_spellings_of_a_label_all_count_towards_it():
    model, _ = left(lambda request: {"B": 0.5, " b": 0.2, "B.": 0.1, "A": 0.1, "hmm": 0.1}, max_rotations=1)

    answer = (await model.decide("state", {"select": TWO})).answers["select"]

    assert answer.probabilities == pytest.approx({"close": 0.1 / 0.9, "wait": 0.8 / 0.9})  # type: ignore[union-attr]


# --- Yes/no and score --------------------------------------------------------------------


async def test_yes_no_is_the_share_of_yes_among_yes_and_no():
    model, seen = left(lambda request: {"Yes": 0.6, "No": 0.2, "Maybe": 0.2})
    question = NoulQuestion(instructions="Allowed?", yes_when="Under the limit", no_when="Over the limit")

    answer = (await model.decide("state", {"gate": question})).answers["gate"]

    assert isinstance(answer, NoulAnswer) and answer.probability == pytest.approx(0.75)
    assert "Answer Yes when: Under the limit" in user_prompt(seen[0])
    assert "Answer No when: Over the limit" in user_prompt(seen[0])


async def test_score_reads_one_digit_per_level():
    model, seen = left(lambda request: {"0": 0.1, "1": 0.3, "2": 0.6})
    question = ScoreQuestion(instructions="How bad?", levels=["Cosmetic", "Degraded", "Outage"])

    answer = (await model.decide("state", {"severity": question})).answers["severity"]

    assert isinstance(answer, ScoreAnswer)
    assert answer.score == pytest.approx(1.5)
    assert answer.legend == {0: "Cosmetic", 1: "Degraded", 2: "Outage"}
    assert "0: Cosmetic\n1: Degraded\n2: Outage" in user_prompt(seen[0])


# --- The request, and what can go wrong --------------------------------------------------


async def test_request_asks_for_a_single_token_with_probabilities_and_no_reasoning():
    model, seen = left(lambda request: {"Yes": 1.0}, base_url="http://localhost:11434/v1", api_key="k")

    await model.decide({"goal": "Resolve the ticket"}, {"gate": NoulQuestion(instructions="Allowed?")})

    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "http://localhost:11434/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer k"
    assert {k: body[k] for k in ("model", "max_tokens", "temperature", "logprobs", "top_logprobs", "reasoning_effort")} == {
        "model": "some-chat-model", "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": 20, "reasoning_effort": "none",
    }  # fmt: skip
    assert user_prompt(seen[0]).startswith('SITUATION\n{\n  "goal": "Resolve the ticket"')  # the shared prefix comes first


async def test_reasoning_effort_can_be_left_out_for_servers_that_reject_it():
    model, seen = left(lambda request: {"Yes": 1.0}, reasoning_effort=None)

    await model.decide("state", {"gate": NoulQuestion(instructions="Allowed?")})

    assert "reasoning_effort" not in json.loads(seen[0].content)


async def test_a_server_without_log_probabilities_is_a_left_failure():
    with pytest.warns(ExperimentalWarning):
        model = ExperimentalLLMLeft(
            "m", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "B"}}]}))
        )

    with pytest.raises(LeftHemisphereError, match="no token log-probabilities"):
        await model.decide("state", {"select": TWO})


async def test_a_model_that_starts_thinking_instead_of_answering_is_a_left_failure():
    model, _ = left(lambda request: {"Thinking": 0.97, "<|channel>": 0.02, "B": 0.01})

    with pytest.raises(LeftHemisphereError, match="only 1% .*LEFT_REASONING_EFFORT"):
        await model.decide("state", {"select": TWO})


async def test_it_announces_itself_as_experimental():
    with pytest.warns(ExperimentalWarning, match="not calibrated"):
        model = ExperimentalLLMLeft("gemma", transport=httpx.MockTransport(lambda request: reply({"Yes": 1.0})))

    result = await model.decide("state", {"gate": NoulQuestion(instructions="Allowed?")})

    assert model.name == "llm-experimental"
    assert result.model == "gemma, experimental"  # shown on every `LEFT decide` line of a trace


# --- In the loop -------------------------------------------------------------------------


def scripted_right(evaluations: list[Evaluation]) -> ScriptedRightHemisphere:
    proposal = Proposal(
        options=[Option(id="close", description="Close the ticket"), Option(id="wait", description="Keep it open")],
        rationale="r", stakes=Stakes.LOW, open_ended=False,
    )  # fmt: skip
    return ScriptedRightHemisphere(
        plan=Plan(situation="s"), proposals=[proposal], evaluations=evaluations,
        reintegrations=[Reintegration(situation="done", goal_met=True)],
    )  # fmt: skip


async def test_harness_does_not_let_a_letter_loving_chat_model_commit():
    model, _ = left(always_b)
    right = scripted_right([Evaluation(verdict=Verdict.COMMIT, option_id="wait", reasoning="context")])

    result = await Harness(right, model).run("goal")

    decision = result.state.history[0].decision
    assert "evaluate" in right.calls  # 0.50 is below the commit threshold, so it went back to the right
    assert decision.committed_by is Hemisphere.RIGHT


async def test_harness_lets_a_consistent_chat_model_commit_a_routine_decision():
    model, _ = left(prefers("wait", p=0.95))
    right = scripted_right([])

    result = await Harness(right, model).run("goal")

    decision = result.state.history[0].decision
    assert "evaluate" not in right.calls
    assert (decision.option_id, decision.committed_by) == ("wait", Hemisphere.LEFT)
