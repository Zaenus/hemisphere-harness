"""Conversations: replies remember what came before, and a sign-off continues the stopped run."""

import pytest

from hemisphere_harness import (
    Actor,
    ChoiceAnswer,
    Conversation,
    Harness,
    HarnessConfig,
    HumanDecision,
    MockLeftHemisphere,
    Option,
    Outcome,
    Plan,
    Proposal,
    Reintegration,
    RunStatus,
    ScriptedRightHemisphere,
    Stakes,
    Turn,
    WorldState,
)
from hemisphere_harness.examples.simple_loop import DEMO_POLICY, REFUND
from hemisphere_harness.right.prompts import MAX_TURNS, render_state


class RememberingRight(ScriptedRightHemisphere):
    """Answers every message in one step, and keeps the state it was asked to plan from."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen: list[WorldState] = []
        super().__init__(plan=Plan(situation="s"), proposals=[])

    async def plan(self, state):
        self.seen.append(state.model_copy(deep=True))
        return Plan(situation="reading")

    async def propose(self, state):
        return Proposal(
            options=[Option(id="answer", description="answer it"), Option(id="ask", description="ask back")],
            rationale="r",
            stakes=Stakes.LOW,
            open_ended=False,
        )

    async def reintegrate(self, state, step):
        return Reintegration(situation="answered", goal_met=True)

    async def generate(self, prompt, state=None):
        return self.answers.pop(0)


def sure_left(n):
    return MockLeftHemisphere(script=[{"select": ChoiceAnswer.from_probabilities({"answer": 0.95, "ask": 0.05})}] * n)


async def test_a_reply_sees_the_conversation_so_far():
    right = RememberingRight(["Bitcoin is partly decentralized.", "Ethereum is staked, not mined."])
    left = sure_left(2)
    conversation = Conversation(Harness(right, left), context="For a newsletter")

    await conversation.send("Is Bitcoin decentralized?")
    await conversation.send("And Ethereum?")

    assert right.seen[0].conversation == []
    assert right.seen[1].conversation == [Turn(request="Is Bitcoin decentralized?", answer="Bitcoin is partly decentralized.")]
    assert right.seen[1].context == "For a newsletter"
    assert [t.request for t in conversation.turns] == ["Is Bitcoin decentralized?", "And Ethereum?"]


def test_the_right_sees_earlier_exchanges_and_the_left_only_the_last_one_briefly():
    turns = [Turn(request=f"question {i}", answer=f"answer {i} " + "x" * 3000) for i in range(MAX_TURNS + 2)]
    state = WorldState(goal="latest", conversation=turns)

    rendered = render_state(state)
    assert rendered.index("CONVERSATION SO FAR") < rendered.index("GOAL (the person's latest message)")
    assert "question 0" not in rendered and f"question {MAX_TURNS + 1}" in rendered
    assert "2 earlier exchange(s) not shown" in rendered
    assert "[…]" in rendered  # long answers are shortened

    focus = state.focus()
    assert set(focus["previous_exchange"]) == {"person", "agent"}
    assert focus["previous_exchange"]["person"] == f"question {MAX_TURNS + 1}"
    assert len(focus["previous_exchange"]["agent"]) <= 400


async def test_the_claim_check_sees_what_the_person_said_but_not_what_the_agent_answered():
    from hemisphere_harness import Claim, NoulAnswer

    class Checked(RememberingRight):
        async def claims(self, answer, state):
            return [Claim(text="a claim")]

    left = MockLeftHemisphere(script=[
        {"select": ChoiceAnswer.from_probabilities({"answer": 0.95, "ask": 0.05})},
        {"claim:c1": NoulAnswer(probability=0.9)},
    ])  # fmt: skip
    harness = Harness(Checked(["second answer"]), left)
    await harness.run("follow-up", conversation=[Turn(request="My plan costs $30.", answer="An answer to check against")])

    focus, _ = left.calls[-1]
    assert focus["earlier_messages"] == ["My plan costs $30."]
    assert "An answer to check against" not in str(focus)


# --- Sign-off --------------------------------------------------------------------------------


async def stopped_refund():
    harness = Harness(ScriptedRightHemisphere(**REFUND.right), MockLeftHemisphere(script=REFUND.left), config=HarnessConfig(policy=DEMO_POLICY))
    result = await harness.run(REFUND.goal, context=REFUND.context)
    assert result.status is RunStatus.NEEDS_HUMAN
    return result


async def test_an_approval_carries_out_the_option_as_committed_by_the_human():
    stopped = await stopped_refund()
    done = []

    async def act(option, state):
        done.append(option.id)
        return "refunded"

    harness = Harness(ScriptedRightHemisphere(**REFUND.after_signoff), MockLeftHemisphere(), act=act)
    result = await harness.resume(stopped.state, HumanDecision(approve=True, option_id="full_refund", note="confirmed with finance"))

    assert result.status is RunStatus.DONE
    assert done == ["full_refund"]
    step = result.state.history[-1]
    assert step.index == 2
    assert step.decision.committed_by == "human"
    assert step.decision.reasons == ["approved by a human: confirmed with finance"]
    assert step.summary().startswith("step 2: a human approved 'full_refund' -> done")
    assert [(e.actor, e.phase) for e in result.trace[:3]] == [(Actor.HUMAN, "approve"), (Actor.HARNESS, "act"), (Actor.RIGHT, "reintegrate")]
    assert result.report.startswith("Done: with your sign-off")
    assert len(stopped.state.history) == 1  # the stopped run's state is left as it was


async def test_a_rejection_sends_the_right_back_to_propose_again():
    stopped = await stopped_refund()
    right = ScriptedRightHemisphere(
        plan=Plan(situation="unused"),
        proposals=[
            Proposal(
                options=[Option(id="call_customer", description="Call the customer"), Option(id="email", description="Email them")],
                rationale="The human wants another way.",
                stakes=Stakes.LOW,
                open_ended=False,
            )
        ],
        reintegrations=[Reintegration(situation="Rejected; look for another way.", goal_met=False), Reintegration(situation="Called.", goal_met=True)],
        report="I called them instead.",
    )
    left = MockLeftHemisphere(script=[{"select": ChoiceAnswer.from_probabilities({"call_customer": 0.9, "email": 0.1})}])
    result = await Harness(right, left).resume(stopped.state, HumanDecision(approve=False, note="talk to them first"))

    rejected, next_step = result.state.history[-2:]
    assert rejected.decision.outcome is Outcome.REVISE
    assert rejected.decision.reasons == ["a human rejected these options: talk to them first"]
    assert "talk to them first" in rejected.summary()
    assert next_step.decision.option_id == "call_customer"
    assert result.status is RunStatus.DONE


async def test_only_a_stopped_run_can_be_resumed_and_only_with_an_offered_option():
    stopped = await stopped_refund()
    harness = Harness(ScriptedRightHemisphere(**REFUND.after_signoff), MockLeftHemisphere())
    with pytest.raises(ValueError, match="was not one of the options"):
        await harness.resume(stopped.state, HumanDecision(approve=True, option_id="invented"))
    with pytest.raises(ValueError, match="not waiting for a human"):
        await harness.resume(WorldState(goal="g"), HumanDecision(approve=False))


def test_an_approval_must_name_what_it_approves():
    with pytest.raises(ValueError, match="must name the option_id"):
        HumanDecision(approve=True)


async def test_a_conversation_continues_after_its_sign_off():
    right = ScriptedRightHemisphere(**REFUND.right)
    conversation = Conversation(Harness(right, MockLeftHemisphere(script=REFUND.left), config=HarnessConfig(policy=DEMO_POLICY)))
    await conversation.send(REFUND.goal)
    assert [o.id for o in conversation.waiting] == ["full_refund", "prorated_refund", "offer_credit"]

    with pytest.raises(ValueError, match="is not one of"):
        await conversation.approve("invented")

    conversation.harness = Harness(ScriptedRightHemisphere(**REFUND.after_signoff), MockLeftHemisphere())
    result = await conversation.approve("full_refund")
    assert result.status is RunStatus.DONE
    assert conversation.turns[-1].request == "Approved: Cancel the plan and refund the full $2,400"
    assert conversation.waiting == []
    with pytest.raises(ValueError, match="nothing is waiting"):
        await conversation.reject()


# --- The terminal chat ------------------------------------------------------------------------


async def test_the_terminal_chat_answers_and_takes_a_sign_off():
    from hemisphere_harness.cli import chat_loop

    script = dict(REFUND.right)
    script["reintegrations"] = [*REFUND.right["reintegrations"], *REFUND.after_signoff["reintegrations"]]
    harness = Harness(ScriptedRightHemisphere(**script), MockLeftHemisphere(script=REFUND.left), config=HarnessConfig(policy=DEMO_POLICY))
    lines = iter([REFUND.goal, "/approve", "/approve invented", "/frobnicate", "/approve full_refund finance agrees", "/quit", "never read"])
    out: list[str] = []

    async def read():
        return next(lines)

    conversation = Conversation(harness)
    await chat_loop(conversation, read, out.append)
    text = "\n".join(out)

    assert "Waiting for your sign-off on one of these:" in text
    assert "  full_refund: Cancel the plan and refund the full $2,400" in text
    assert "Which option?" in text
    assert "Cannot do that: 'invented' is not one of" in text
    assert "Unknown command /frobnicate" in text
    assert conversation.last.status is RunStatus.DONE
    assert conversation.turns[-1].request == "Approved: Cancel the plan and refund the full $2,400 — finance agrees"


def test_the_chat_command_needs_a_right_hemisphere(clean_env, capsys):
    from hemisphere_harness.cli import main

    assert main(["chat"]) == 2
    assert "Chat needs a right hemisphere" in capsys.readouterr().err
