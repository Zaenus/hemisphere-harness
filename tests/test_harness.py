"""The decision and reintegration flow: who may commit what, and what happens when they may not."""

import pytest

from hemisphere_harness import (
    Actor,
    Answer,
    ChoiceAnswer,
    Evaluation,
    Harness,
    HarnessConfig,
    Hemisphere,
    LeftHemisphere,
    LeftHemisphereError,
    LeftResult,
    MockLeftHemisphere,
    NoulAnswer,
    Option,
    Outcome,
    Plan,
    Proposal,
    Reintegration,
    RightHemisphereError,
    RunStatus,
    ScriptedRightHemisphere,
    Stakes,
    Verdict,
    WorldState,
)

PLAN = Plan(situation="initial reading", steps=["do the thing"])


def proposal(*ids: str, stakes: Stakes = Stakes.LOW, open_ended: bool = False) -> Proposal:
    return Proposal(
        options=[Option(id=i, description=f"do {i}") for i in ids or ("a", "b", "c")],
        rationale="because",
        stakes=stakes,
        open_ended=open_ended,
    )


def left_says(probabilities: dict[str, float], **permits: float) -> dict[str, Answer]:
    """One scripted left pass: a choice distribution plus optional policy-gate answers."""
    answers: dict[str, Answer] = {"select": ChoiceAnswer.from_probabilities(probabilities)}
    answers.update({f"permit:{option}": NoulAnswer(probability=p) for option, p in permits.items()})
    return answers


def right(
    proposals: list[Proposal],
    evaluations: list[Evaluation] | None = None,
    goal_met: list[bool] | None = None,
) -> ScriptedRightHemisphere:
    goal_met = goal_met if goal_met is not None else [True] * len(proposals)
    return ScriptedRightHemisphere(
        plan=PLAN,
        proposals=proposals,
        evaluations=evaluations or [],
        reintegrations=[
            Reintegration(situation=f"reading after step {i}", goal_met=met)
            for i, met in enumerate(goal_met, 1)
        ],
        report="final report",
    )


class Recorder:
    """An actuator that remembers which options it was asked to carry out."""

    def __init__(self) -> None:
        self.done: list[str] = []

    async def __call__(self, option: Option, state: WorldState) -> str:
        self.done.append(option.id)
        return f"did {option.id}"


CONFIDENT = left_says({"a": 0.9, "b": 0.07, "c": 0.03})
UNSURE = left_says({"a": 0.5, "b": 0.3, "c": 0.2})


async def test_confident_left_commits_a_routine_low_stakes_decision():
    brain = right([proposal()])
    act = Recorder()

    result = await Harness(brain, MockLeftHemisphere([CONFIDENT]), act=act).run("goal")

    step = result.state.history[0]
    assert result.status is RunStatus.DONE
    assert step.decision.outcome is Outcome.PROCEED
    assert step.decision.committed_by is Hemisphere.LEFT
    assert act.done == ["a"]
    assert "evaluate" not in brain.calls


async def test_unsure_left_hands_the_decision_back_and_the_right_may_choose_differently():
    brain = right([proposal()], [Evaluation(verdict=Verdict.COMMIT, option_id="b", reasoning="context")])
    act = Recorder()

    result = await Harness(brain, MockLeftHemisphere([UNSURE]), act=act).run("goal")

    decision = result.state.history[0].decision
    assert decision.left_choice == "a"
    assert decision.option_id == "b"
    assert decision.committed_by is Hemisphere.RIGHT
    assert act.done == ["b"]


@pytest.mark.parametrize(
    "labels",
    [{"stakes": Stakes.HIGH}, {"open_ended": True}],
    ids=["high-stakes", "open-ended"],
)
async def test_left_never_commits_alone_when_stakes_are_high_or_the_question_is_open(labels):
    certain = left_says({"a": 0.99, "b": 0.005, "c": 0.005})
    brain = right([proposal(**labels)], [Evaluation(verdict=Verdict.COMMIT, option_id="a", reasoning="agreed")])

    result = await Harness(brain, MockLeftHemisphere([certain])).run("goal")

    assert "evaluate" in brain.calls
    assert result.state.history[0].decision.committed_by is Hemisphere.RIGHT


async def test_right_can_ask_for_a_human_and_nothing_is_done():
    brain = right(
        [proposal()], [Evaluation(verdict=Verdict.ASK_HUMAN, reasoning="not mine to decide")], goal_met=[False]
    )
    act = Recorder()

    result = await Harness(brain, MockLeftHemisphere([UNSURE]), act=act).run("goal")

    assert result.status is RunStatus.NEEDS_HUMAN
    assert result.state.history[0].decision.outcome is Outcome.FLAG_HUMAN
    assert act.done == []
    assert brain.calls[-2:] == ["reintegrate", "generate"]  # still folded back, still reported


async def test_revise_does_nothing_this_step_and_the_right_proposes_again():
    brain = right(
        [proposal("a", "b"), proposal("x", "y")],
        [Evaluation(verdict=Verdict.REVISE, reasoning="neither fits")],
        goal_met=[False, True],
    )
    act = Recorder()
    left = MockLeftHemisphere([left_says({"a": 0.5, "b": 0.5}), left_says({"x": 0.95, "y": 0.05})])

    result = await Harness(brain, left, act=act).run("goal")

    first, second = result.state.history
    assert first.decision.outcome is Outcome.REVISE and first.action is None
    assert second.decision.outcome is Outcome.PROCEED
    assert act.done == ["x"]


async def test_left_failure_escalates_to_the_right_instead_of_ending_the_run():
    class BrokenLeft(LeftHemisphere):
        async def _decide(self, state, questions):
            raise LeftHemisphereError("Jev unavailable after 3 attempts: HTTP 529")

    brain = right([proposal()], [Evaluation(verdict=Verdict.COMMIT, option_id="c", reasoning="carrying on")])
    act = Recorder()

    result = await Harness(brain, BrokenLeft(), act=act).run("goal")

    step = result.state.history[0]
    assert result.status is RunStatus.DONE
    assert step.left is None
    assert "HTTP 529" in step.decision.reasons[0]
    assert act.done == ["c"]


async def test_left_answer_outside_the_option_space_is_treated_as_a_left_failure():
    class RogueLeft(LeftHemisphere):
        async def _decide(self, state, questions):
            return LeftResult(answers={"select": ChoiceAnswer.from_probabilities({"delete_everything": 1.0})})

    brain = right([proposal()], [Evaluation(verdict=Verdict.ASK_HUMAN, reasoning="left is confused")], goal_met=[False])
    act = Recorder()

    result = await Harness(brain, RogueLeft(), act=act).run("goal")

    decision = result.state.history[0].decision
    assert decision.left_choice is None
    assert "not among the options" in decision.reasons[0]
    assert act.done == []


async def test_right_failure_ends_the_run():
    brain = ScriptedRightHemisphere(plan=PLAN, proposals=[])  # nothing to propose

    with pytest.raises(RightHemisphereError):
        await Harness(brain, MockLeftHemisphere()).run("goal")


async def test_right_committing_to_an_unknown_option_flags_a_human():
    brain = right(
        [proposal()], [Evaluation(verdict=Verdict.COMMIT, option_id="zzz", reasoning="?")], goal_met=[False]
    )
    act = Recorder()

    result = await Harness(brain, MockLeftHemisphere([UNSURE]), act=act).run("goal")

    assert result.status is RunStatus.NEEDS_HUMAN
    assert act.done == []


# --- Policy gate -------------------------------------------------------------------------

POLICY = HarnessConfig(policy="Refunds need sign-off.")


async def test_no_policy_means_one_question_and_a_policy_adds_one_gate_per_option():
    plain, gated = MockLeftHemisphere([CONFIDENT]), MockLeftHemisphere([CONFIDENT])

    await Harness(right([proposal()]), plain).run("goal")
    await Harness(right([proposal()], [Evaluation(verdict=Verdict.REVISE, reasoning="-")]), gated, config=POLICY).run("goal")

    assert set(plain.calls[0][1]) == {"select"}
    assert set(gated.calls[0][1]) == {"select", "permit:a", "permit:b", "permit:c"}
    assert "Refunds need sign-off." in gated.calls[0][1]["permit:a"].instructions


async def test_policy_gate_allowing_the_pick_keeps_the_fast_path():
    brain = right([proposal()])
    left = MockLeftHemisphere([left_says({"a": 0.9, "b": 0.07, "c": 0.03}, a=0.95, b=0.1, c=0.1)])

    result = await Harness(brain, left, config=POLICY).run("goal")

    assert result.state.history[0].decision.committed_by is Hemisphere.LEFT


async def test_unsure_policy_gate_sends_a_confident_pick_back_to_the_right():
    brain = right([proposal()], [Evaluation(verdict=Verdict.COMMIT, option_id="a", reasoning="fine in context")])
    left = MockLeftHemisphere([left_says({"a": 0.9, "b": 0.07, "c": 0.03}, a=0.5, b=0.9, c=0.9)])
    act = Recorder()

    result = await Harness(brain, left, config=POLICY, act=act).run("goal")

    decision = result.state.history[0].decision
    assert "evaluate" in brain.calls
    assert decision.committed_by is Hemisphere.RIGHT  # unsure is the right's call to make
    assert act.done == ["a"]


async def test_policy_refusal_needs_a_human_even_when_the_right_commits():
    brain = right(
        [proposal()], [Evaluation(verdict=Verdict.COMMIT, option_id="a", reasoning="it is the right remedy")], goal_met=[False]
    )
    left = MockLeftHemisphere([left_says({"a": 0.9, "b": 0.07, "c": 0.03}, a=0.05, b=0.9, c=0.9)])
    act = Recorder()

    result = await Harness(brain, left, config=POLICY, act=act).run("goal")

    assert result.status is RunStatus.NEEDS_HUMAN
    assert "policy requires human sign-off" in result.state.history[0].decision.reasons[-1]
    assert act.done == []


async def test_right_may_route_around_a_refused_option():
    brain = right([proposal()], [Evaluation(verdict=Verdict.COMMIT, option_id="b", reasoning="b needs no sign-off")])
    left = MockLeftHemisphere([left_says({"a": 0.9, "b": 0.07, "c": 0.03}, a=0.05, b=0.9, c=0.9)])
    act = Recorder()

    await Harness(brain, left, config=POLICY, act=act).run("goal")

    assert act.done == ["b"]


# --- World state, limits, trace ----------------------------------------------------------


async def test_only_reintegration_writes_the_situation_and_the_left_sees_a_narrow_copy():
    brain = right([proposal(), proposal()], goal_met=[False, True])
    left = MockLeftHemisphere([CONFIDENT, CONFIDENT])

    result = await Harness(brain, left).run("goal", context="background")

    first_view, second_view = (state for state, _ in left.calls)
    assert first_view == {"goal": "goal", "situation": "initial reading", "context": "background"}
    assert second_view["situation"] == "reading after step 1"
    assert len(second_view["recent_steps"]) == 1
    assert result.state.situation == "reading after step 2"

    first_view["situation"] = "tampered"  # the left holds a copy, not the world model
    assert result.state.situation == "reading after step 2"


async def test_run_stops_at_max_steps():
    brain = right([proposal()] * 5, goal_met=[False] * 5)
    left = MockLeftHemisphere([CONFIDENT] * 5)

    result = await Harness(brain, left, config=HarnessConfig(max_steps=3)).run("goal")

    assert result.status is RunStatus.MAX_STEPS
    assert len(result.state.history) == 3


async def test_failed_action_is_reintegrated_rather_than_raised():
    async def explode(option: Option, state: WorldState) -> str:
        raise TimeoutError("ticket system did not respond")

    brain = right([proposal()], goal_met=[False])

    result = await Harness(brain, MockLeftHemisphere([CONFIDENT]), act=explode, config=HarnessConfig(max_steps=1)).run("goal")

    action = result.state.history[0].action
    assert action is not None and not action.ok
    assert "ticket system did not respond" in action.output
    assert "reintegrate" in brain.calls


async def test_trace_attributes_each_phase_to_whoever_did_it():
    brain = right([proposal()], [Evaluation(verdict=Verdict.COMMIT, option_id="b", reasoning="context")])
    seen = []

    result = await Harness(brain, MockLeftHemisphere([UNSURE]), on_event=seen.append).run("goal")

    assert [(event.actor, event.phase) for event in result.trace] == [
        (Actor.RIGHT, "plan"),
        (Actor.RIGHT, "propose"),
        (Actor.LEFT, "decide"),
        (Actor.HARNESS, "gate"),
        (Actor.RIGHT, "evaluate"),
        (Actor.HARNESS, "gate"),
        (Actor.HARNESS, "act"),
        (Actor.RIGHT, "reintegrate"),
        (Actor.RIGHT, "report"),
        (Actor.HARNESS, "end"),
    ]
    assert seen == result.trace
    assert result.report == "final report"
