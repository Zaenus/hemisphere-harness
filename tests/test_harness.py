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
    assert brain.calls[-3:] == ["reintegrate", "generate", "claims"]  # still folded back, still answered


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


# --- The reply that ends a run ------------------------------------------------------------


class RecordingRight(ScriptedRightHemisphere):
    """A scripted right that keeps the prompt it was given for the final reply."""

    async def generate(self, prompt, state=None):
        self.reply_prompt = prompt
        return await super().generate(prompt, state)


async def _run_demo(ticket):
    from hemisphere_harness.examples.simple_loop import DEMO_POLICY

    right = RecordingRight(**ticket.right)
    left = MockLeftHemisphere(script=ticket.left)
    result = await Harness(right, left, config=HarnessConfig(policy=DEMO_POLICY)).run(ticket.goal, context=ticket.context)
    return result, right.reply_prompt


async def test_the_reply_is_asked_for_in_one_voice_without_the_machinery():
    from hemisphere_harness.examples.simple_loop import LOCKOUT

    result, prompt = await _run_demo(LOCKOUT)
    assert result.status is RunStatus.DONE
    assert "first person" in prompt
    assert "Never mention hemispheres" in prompt
    assert "Do not narrate the process" in prompt
    assert "decision before it can happen" not in prompt  # nothing is pending


async def test_a_reply_waiting_on_a_human_names_what_is_waiting():
    from hemisphere_harness.examples.simple_loop import REFUND

    result, prompt = await _run_demo(REFUND)
    assert result.status is RunStatus.NEEDS_HUMAN
    # The right committed to the full refund and the policy stopped it: that is what waits.
    assert "Cancel the plan and refund the full $2,400" in prompt
    assert "ask for their go-ahead" in prompt


def test_a_reply_at_the_step_limit_says_the_work_is_unfinished():
    from hemisphere_harness.harness import answer_prompt

    prompt = answer_prompt(RunStatus.MAX_STEPS, WorldState(goal="g"))
    assert "stopped before it was finished" in prompt
    assert "{" not in prompt  # every placeholder was filled


def test_the_rights_role_keeps_the_division_of_labour_from_the_person():
    from hemisphere_harness.right.prompts import ROLE

    assert "talks to one agent, not to its parts" in ROLE


# --- Checking the answer -------------------------------------------------------------------

from hemisphere_harness import Claim, ClaimVerdict  # noqa: E402


def _answering_right(**overrides):
    script = dict(
        plan=Plan(situation="The reading so far, which the checker must not see."),
        proposals=[
            Proposal(
                options=[Option(id="a", description="do a"), Option(id="b", description="do b")],
                rationale="r",
                stakes=Stakes.LOW,
                open_ended=False,
            )
        ],
        reintegrations=[Reintegration(situation="done", goal_met=True)],
        report="Draft answer.",
        claims=[Claim(text="Water boils at 100 C at sea level."), Claim(text="The moon is made of cheese.")],
        revision="Revised answer.",
    )
    return RecordingRight(**{**script, **overrides})


def _left_judging(*probabilities):
    return MockLeftHemisphere(
        script=[
            {"select": ChoiceAnswer.from_probabilities({"a": 0.95, "b": 0.05})},
            {f"claim:c{i}": NoulAnswer(probability=p) for i, p in enumerate(probabilities, 1)},
        ]
    )


async def test_claims_that_hold_leave_the_answer_as_drafted():
    right = _answering_right()
    result = await Harness(right, _left_judging(0.97, 0.91)).run("Answer a question")

    assert result.report == "Draft answer."
    assert "revise" not in right.calls
    assert [c.verdict for c in result.check.checks] == [ClaimVerdict.HOLDS, ClaimVerdict.HOLDS]
    assert result.check.revised is False
    phases = [event.phase for event in result.trace]
    assert phases[-4:] == ["draft", "check", "report", "end"]


async def test_a_doubtful_claim_goes_back_to_the_right_before_the_person_sees_it():
    right = _answering_right()
    result = await Harness(right, _left_judging(0.97, 0.08)).run("Answer a question")

    assert result.report == "Revised answer."
    assert result.check.draft == "Draft answer."
    assert result.check.revised is True
    assert [c.verdict for c in right.revised_with] == [ClaimVerdict.HOLDS, ClaimVerdict.DOUBTFUL]
    check = next(event for event in result.trace if event.phase == "check")
    assert check.actor is Actor.LEFT
    assert check.data["checks"][1]["claim"]["text"] == "The moon is made of cheese."
    assert [event.phase for event in result.trace][-3:] == ["revise", "report", "end"]


async def test_an_unsure_claim_is_also_sent_back():
    right = _answering_right()
    result = await Harness(right, _left_judging(0.97, 0.5)).run("Answer a question")
    assert result.report == "Revised answer."
    assert right.revised_with[1].verdict is ClaimVerdict.UNSURE


async def test_the_left_checks_claims_without_seeing_the_rights_reading():
    left = _left_judging(0.97, 0.91)
    await Harness(_answering_right(), left).run("Answer a question", context="Facts the person gave")

    focus, questions = left.calls[-1]
    assert focus == {"request": "Answer a question", "context": "Facts the person gave"}
    assert set(questions) == {"claim:c1", "claim:c2"}
    assert "Water boils at 100 C" in questions["claim:c1"].instructions


async def test_a_failing_check_leaves_the_draft_standing():
    class FailsOnClaims(MockLeftHemisphere):
        async def _decide(self, state, questions):
            if any(qid.startswith("claim:") for qid in questions):
                raise LeftHemisphereError("checker down")
            return await super()._decide(state, questions)

    left = FailsOnClaims(script=[{"select": ChoiceAnswer.from_probabilities({"a": 0.95, "b": 0.05})}])
    result = await Harness(_answering_right(), left).run("Answer a question")

    assert result.status is RunStatus.DONE
    assert result.report == "Draft answer."
    assert result.check.error == "checker down"


async def test_a_right_that_cannot_list_claims_leaves_the_draft_standing():
    class NoClaims(RecordingRight):
        async def claims(self, answer, state):
            raise RightHemisphereError("malformed claims")

    right = NoClaims(
        plan=Plan(situation="s"),
        proposals=[Proposal(options=[Option(id="a", description="a"), Option(id="b", description="b")],
                            rationale="r", stakes=Stakes.LOW, open_ended=False)],
        reintegrations=[Reintegration(situation="done", goal_met=True)],
        report="Draft answer.",
    )  # fmt: skip
    result = await Harness(right, _left_judging()).run("Answer a question")
    assert result.report == "Draft answer."
    assert result.check.error == "malformed claims"


async def test_nothing_to_check_means_no_check():
    right = _answering_right(claims=[])
    result = await Harness(right, _left_judging()).run("Answer a question")
    assert result.check is None
    assert "check" not in [event.phase for event in result.trace]


async def test_the_check_can_be_turned_off():
    right = _answering_right()
    result = await Harness(right, _left_judging(), config=HarnessConfig(check_answers=False)).run("Answer a question")
    assert "claims" not in right.calls
    assert result.check is None
    assert result.report == "Draft answer."


async def test_an_llm_right_lists_at_most_eight_claims_and_revises_without_mentioning_the_check():
    import json

    from hemisphere_harness import LLMRightHemisphere, WorldState
    from hemisphere_harness.schemas import ClaimCheck

    class Fake(LLMRightHemisphere):
        def __init__(self):
            self.prompts = []

        async def complete(self, messages, *, json_reply=False):
            self.prompts.append(messages[-1]["content"])
            if json_reply:
                return json.dumps({"claims": [{"text": f"claim {i}"} for i in range(10)]})
            return "Rewritten."

    right = Fake()
    state = WorldState(goal="Is X true?", context="Some facts", situation="private reading")
    claims = await right.claims("Draft.", state)
    assert [c.text for c in claims] == [f"claim {i}" for i in range(8)]

    checks = [ClaimCheck(claim=claims[0], probability=0.1, verdict=ClaimVerdict.DOUBTFUL)]
    assert await right.revise("Draft.", checks, state) == "Rewritten."
    prompt = right.prompts[-1]
    assert "[doubtful 0.10] claim 0" in prompt
    assert "Do not mention the check" in prompt
    assert "no new numbers, names, dates or sources" in prompt  # nothing would check them
    assert "private reading" not in prompt  # revised against the request, not the working notes
