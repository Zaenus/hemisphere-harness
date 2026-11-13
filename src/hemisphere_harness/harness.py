"""The orchestrator. It owns the loop and the world state, and it enforces who may commit what.

One step:

    RIGHT   propose       open an option space, and say how far the left can be trusted with it
    LEFT    decide        answer typed questions about those options, in one pass
    HARNESS gate          the left commits only a routine, low-stakes pick it is sure of
    RIGHT   evaluate      everything else returns to the right before anything is done
    HARNESS act           carry out the committed option, or stop for a human
    RIGHT   reintegrate   fold the step back into the whole

The Master/Emissary relationship is held in three places: `_gate` is the only code path
by which a left verdict becomes a commitment, `_resolve` is where the right's judgement
replaces it, and reintegration is the only write to the world state's situation.
"""

from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self

from .config import HarnessConfig
from .errors import LeftHemisphereError
from .left.base import LeftHemisphere
from .right.base import RightHemisphere
from .schemas import (
    ActionResult,
    Authority,
    ChoiceAnswer,
    ChoiceQuestion,
    Decision,
    Evaluation,
    Hemisphere,
    LeftResult,
    NoulAnswer,
    NoulQuestion,
    Option,
    Outcome,
    Proposal,
    Question,
    RunResult,
    RunStatus,
    Stakes,
    StepRecord,
    Verdict,
    WorldState,
)
from .trace import Actor, EventSink, Trace

Actuator = Callable[[Option, WorldState], Awaitable[str]]
"""Carries out a committed option and returns what happened. Treat the state as read-only."""

SELECT = "select"
PERMIT = "permit:"

REPORT_PROMPT = (
    "The run has ended with status '{status}'. Write a short report for the person who set "
    "this goal: what was done, where things stand, and anything that needs their attention. "
    "If a decision is waiting on them, say what it is and what you recommend. Plain prose."
)


async def dry_run(option: Option, state: WorldState) -> str:
    """The default actuator: touches nothing and reports what would have been done."""
    return f"(dry run) {option.description}"


class Harness:
    def __init__(
        self,
        right: RightHemisphere,
        left: LeftHemisphere,
        *,
        config: HarnessConfig | None = None,
        act: Actuator = dry_run,
        on_event: EventSink | None = None,
    ) -> None:
        self.right = right
        self.left = left
        self.config = config or HarnessConfig()
        self._actuator = act
        self._on_event = on_event

    async def run(self, goal: str, *, context: str = "") -> RunResult:
        """Pursue `goal` until the right judges it met, a human is needed, or steps run out."""
        state = WorldState(goal=goal, context=context)
        trace = Trace(self._on_event)

        plan = await self.right.plan(state)
        state.situation, state.plan = plan.situation, plan.steps
        trace.emit(
            Actor.RIGHT,
            "plan",
            plan.situation,
            *(f"{i}. {s}" for i, s in enumerate(plan.steps, 1)),
            data={"plan": plan.model_dump(mode="json")},
        )

        status = RunStatus.MAX_STEPS
        for _ in range(self.config.max_steps):
            record = await self.step(state, trace)
            if record.decision.outcome is Outcome.FLAG_HUMAN:
                status = RunStatus.NEEDS_HUMAN
                break
            if record.reintegration is not None and record.reintegration.goal_met:
                status = RunStatus.DONE
                break

        report = await self.right.generate(REPORT_PROMPT.format(status=status), state)
        trace.emit(Actor.RIGHT, "report", report, data={"report": report})
        trace.emit(
            Actor.HARNESS,
            "end",
            f"{status.upper()} after {len(state.history)} step(s)",
            data={"status": status, "steps": len(state.history)},
        )
        return RunResult(status=status, state=state, report=report, trace=trace.events)

    async def step(self, state: WorldState, trace: Trace | None = None) -> StepRecord:
        """One full cycle. Appends the finished step to `state.history` and returns it."""
        trace = trace or Trace(self._on_event)
        index = len(state.history) + 1

        # RIGHT opens the option space.
        proposal = await self.right.propose(state)
        authority = self._authority(proposal)
        trace.emit(
            Actor.RIGHT,
            "propose",
            f"{len(proposal.options)} options · stakes {proposal.stakes} · "
            f"{'open-ended' if proposal.open_ended else 'routine'} · "
            f"the left may {'decide' if authority is Authority.DECIDE else 'only advise'}",
            proposal.rationale,
            *(f"• {option.id}: {option.description}" for option in proposal.options),
            step=index,
            data={"proposal": proposal.model_dump(mode="json"), "authority": authority},
        )

        # LEFT answers within it. A failure here is survivable: the right carries on alone.
        left: LeftResult | None = None
        failure = ""
        try:
            left = await self.left.decide(state.focus(), self.build_questions(state, proposal))
        except LeftHemisphereError as exc:
            failure = str(exc)
        decision = self._gate(authority, left, failure)
        self._trace_left(trace, index, left, decision, failure)
        self._trace_gate(trace, index, decision)

        # RIGHT takes back whatever the left may not settle alone, before anything is done.
        evaluation: Evaluation | None = None
        if decision.outcome is Outcome.ESCALATE_TO_RIGHT:
            evaluation = await self.right.evaluate(state, proposal, decision)
            verdict = evaluation.verdict.replace("_", " ")
            if evaluation.verdict is Verdict.COMMIT:
                verdict += f" '{evaluation.option_id}'"
            trace.emit(
                Actor.RIGHT,
                "evaluate",
                verdict,
                evaluation.reasoning,
                step=index,
                data={"evaluation": evaluation.model_dump(mode="json")},
            )
            decision = self._resolve(proposal, decision, evaluation)
            self._trace_gate(trace, index, decision)

        # Act, but only on a committed option.
        action: ActionResult | None = None
        option = proposal.option(decision.option_id)
        if decision.outcome is Outcome.PROCEED and option is not None:
            action = await self._act(option, state)
            trace.emit(
                Actor.HARNESS,
                "act",
                f"{option.id} → {action.output}" if action.ok else f"{option.id} FAILED: {action.output}",
                step=index,
                data={"action": action.model_dump(mode="json")},
            )

        # RIGHT folds the step back into the whole. This is the only write to the situation.
        record = StepRecord(
            index=index,
            proposal=proposal,
            left=left,
            decision=decision,
            evaluation=evaluation,
            action=action,
        )
        reintegration = await self.right.reintegrate(state, record)
        record.reintegration = reintegration
        state.situation = reintegration.situation
        state.history.append(record)
        trace.emit(
            Actor.RIGHT,
            "reintegrate",
            reintegration.situation,
            "goal met" if reintegration.goal_met else "goal not yet met",
            step=index,
            data={"reintegration": reintegration.model_dump(mode="json")},
        )
        return record

    def build_questions(self, state: WorldState, proposal: Proposal) -> dict[str, Question]:
        """The typed questions put to the left for one proposal. All are answered in one pass.

        Always a choice among the right's options. If an operator policy is configured,
        also one yes/no gate per option, since the left answers each question
        independently and cannot gate "whichever option wins".

        Override to ask more. `_gate` expects the SELECT question to be present.
        """
        questions: dict[str, Question] = {
            SELECT: ChoiceQuestion(
                instructions=(
                    "Which of these candidate actions best advances the goal, "
                    "given the situation described in the state?"
                ),
                options={option.id: option.description for option in proposal.options},
            )
        }
        if self.config.policy:
            for option in proposal.options:
                questions[PERMIT + option.id] = NoulQuestion(
                    instructions=(
                        f"Policy: {self.config.policy}\n"
                        "Under this policy, may the following action be carried out without "
                        f"human sign-off?\nAction: {option.description}"
                    )
                )
        return questions

    def _authority(self, proposal: Proposal) -> Authority:
        """The right's own labels decide how much the left's verdict counts."""
        if proposal.stakes is Stakes.HIGH or proposal.open_ended:
            return Authority.ADVISE
        return Authority.DECIDE

    def _gate(self, authority: Authority, left: LeftResult | None, failure: str) -> Decision:
        """The only place a left verdict can become a commitment.

        The left commits when all of these hold: the right marked the decision routine
        and low-stakes, the left's pick is probable enough, and the policy gate (if any)
        allows that pick. Any doubt on any count sends the decision back to the right.
        """
        if left is None:
            return Decision(
                outcome=Outcome.ESCALATE_TO_RIGHT,
                authority=authority,
                reasons=[f"the left hemisphere is unavailable: {failure}"],
            )

        select = left.answers[SELECT]
        assert isinstance(select, ChoiceAnswer)  # LeftHemisphere.decide checked it against the question
        permits = {
            qid.removeprefix(PERMIT): answer.probability
            for qid, answer in left.answers.items()
            if qid.startswith(PERMIT) and isinstance(answer, NoulAnswer)
        }

        config = self.config
        reasons: list[str] = []
        if authority is Authority.ADVISE:
            reasons.append("high-stakes or open-ended, so the left advises and the right commits")
        if select.probability < config.commit_threshold:
            reasons.append(
                f"p={select.probability:.2f} for '{select.choice}' is below "
                f"the commit threshold {config.commit_threshold:.2f}"
            )
        permit = permits.get(select.choice)
        if permit is not None and permit < config.permit_yes_threshold:
            stance = "refuses" if permit <= config.permit_no_threshold else "is unsure about"
            reasons.append(f"the policy gate {stance} '{select.choice}' (p={permit:.2f})")

        committed = not reasons
        return Decision(
            outcome=Outcome.PROCEED if committed else Outcome.ESCALATE_TO_RIGHT,
            authority=authority,
            left_choice=select.choice,
            probability=select.probability,
            confidence=select.confidence,
            probabilities=select.probabilities,
            permits=permits,
            option_id=select.choice if committed else None,
            committed_by=Hemisphere.LEFT if committed else None,
            reasons=reasons,
        )

    def _resolve(self, proposal: Proposal, decision: Decision, evaluation: Evaluation) -> Decision:
        """Apply the right's evaluation to a decision that came back to it.

        The right may commit to any offered option, with one limit: an option the policy
        gate refused is never carried out without a human, whichever hemisphere wants it.
        The policy is the operator's, and it binds the Master as well as the Emissary.
        """
        outcome = Outcome.FLAG_HUMAN
        option_id: str | None = None
        reason = ""

        if evaluation.verdict is Verdict.REVISE:
            outcome = Outcome.REVISE
            reason = "the right rejected these options and will propose again"
        elif evaluation.verdict is Verdict.ASK_HUMAN:
            reason = "the right asked for a human"
        elif proposal.option(evaluation.option_id) is None:
            reason = f"the right committed to '{evaluation.option_id}', which was not an option"
        elif decision.permits.get(evaluation.option_id or "", 1.0) <= self.config.permit_no_threshold:
            reason = (
                f"the right chose '{evaluation.option_id}', but policy requires "
                f"human sign-off for it (p={decision.permits[evaluation.option_id or '']:.2f})"
            )
        else:
            outcome = Outcome.PROCEED
            option_id = evaluation.option_id

        return decision.model_copy(
            update={
                "outcome": outcome,
                "option_id": option_id,
                "committed_by": Hemisphere.RIGHT if option_id else None,
                "reasons": [*decision.reasons, reason] if reason else decision.reasons,
            }
        )

    async def _act(self, option: Option, state: WorldState) -> ActionResult:
        try:
            output = await self._actuator(option, state)
        except Exception as exc:  # a failed action is information for the right, not a crash
            return ActionResult(option_id=option.id, ok=False, output=f"{type(exc).__name__}: {exc}")
        return ActionResult(option_id=option.id, ok=True, output=output)

    def _trace_left(
        self, trace: Trace, index: int, left: LeftResult | None, decision: Decision, failure: str
    ) -> None:
        if left is None:
            trace.emit(Actor.LEFT, "decide", f"unavailable: {failure}", step=index, data={"error": failure})
            return
        latency = f", {left.latency_ms:.0f} ms" if left.latency_ms is not None else ""
        details = [_distribution(decision.probabilities)]
        if decision.permits:
            details.append("allowed without sign-off: " + _distribution(decision.permits, ranked=False))
        trace.emit(
            Actor.LEFT,
            "decide",
            f"{decision.left_choice}  p={decision.probability:.2f}  "
            f"[{left.model or self.left.name}, {len(left.answers)} question(s){latency}]",
            *details,
            step=index,
            data={
                "decision": decision.model_dump(mode="json"),
                "model": left.model or self.left.name,
                "latency_ms": left.latency_ms,
                "questions": len(left.answers),
            },
        )

    def _trace_gate(self, trace: Trace, index: int, decision: Decision) -> None:
        if decision.outcome is Outcome.PROCEED:
            summary = f"PROCEED: the {decision.committed_by} commits '{decision.option_id}'"
            if decision.committed_by is Hemisphere.LEFT:
                summary += f" (p={decision.probability:.2f} ≥ {self.config.commit_threshold:.2f})"
            trace.emit(Actor.HARNESS, "gate", summary, step=index, data={"decision": decision.model_dump(mode="json")})
            return
        label = {
            Outcome.ESCALATE_TO_RIGHT: "BACK TO THE RIGHT",
            Outcome.REVISE: "REVISE: nothing done this step",
            Outcome.FLAG_HUMAN: "FLAG FOR A HUMAN: nothing done",
        }[decision.outcome]
        # After the right has resolved a decision, only its closing reason is new.
        reasons = decision.reasons if decision.outcome is Outcome.ESCALATE_TO_RIGHT else decision.reasons[-1:]
        trace.emit(
            Actor.HARNESS,
            "gate",
            label,
            *reasons,
            step=index,
            data={"decision": decision.model_dump(mode="json"), "new_reasons": reasons},
        )

    async def aclose(self) -> None:
        await self.left.aclose()
        await self.right.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


def _distribution(probabilities: dict[str, float], *, ranked: bool = True) -> str:
    items = list(probabilities.items())
    if ranked:
        items.sort(key=lambda item: item[1], reverse=True)
    return " · ".join(f"{key} {value:.2f}" for key, value in items)
