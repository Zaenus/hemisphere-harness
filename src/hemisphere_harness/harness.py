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

from collections.abc import Awaitable, Callable, Sequence
from types import TracebackType
from typing import Any, Self

from .config import HarnessConfig
from .errors import LeftHemisphereError, RightHemisphereError
from .left.base import LeftHemisphere
from .right.base import RightHemisphere
from .schemas import (
    ActionResult,
    AnswerCheck,
    Authority,
    ChoiceAnswer,
    ChoiceQuestion,
    Claim,
    ClaimCheck,
    ClaimVerdict,
    HUMAN,
    Decision,
    Evaluation,
    Hemisphere,
    HumanDecision,
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
    Turn,
    Verdict,
    WorldState,
)
from .lookup import Searcher
from .memory import MemoryStore, Note
from .tools import Toolbox, ToolSpec
from .trace import Actor, EventSink, Trace

Actuator = Callable[[Option, WorldState], Awaitable[str]]
"""Carries out a committed option and returns what happened. Treat the state as read-only."""

SELECT = "select"
PERMIT = "permit:"
CLAIM = "claim:"
RECALL = "recall:"
MAX_RECALL_CANDIDATES = 40
MAX_RECALLED_WITHOUT_LEFT = 5
MAX_EARLIER = 4  # earlier messages from the person shown to the claim check

ANSWER_PROMPT = """\
Now reply to the person who made this request. This reply is all they will see.

- Answer the request itself. For a question, give the answer. For a task, say what is \
now done and what came of it.
- Speak as one agent, in the first person. Everything settled along the way, including \
the quick judgements on routine choices, is now your own view. Never mention \
hemispheres, a left or right side, a harness, steps, gates, probabilities, option ids, \
or how the work was organised.
- Start with the substance. Do not narrate the process: no "the inquiry has been \
completed", no "the agent delivered".
- Where you are unsure or the evidence is mixed, say so plainly as part of the answer.
- Write as in a conversation, in the language of the request. Short paragraphs; lists or \
headings only where they make the answer clearer.{ending}"""

_ENDINGS = {
    RunStatus.DONE: "",
    RunStatus.NEEDS_HUMAN: (
        "\n- One thing needs their decision before it can happen: {pending}. Nothing has been "
        "done about it yet. Say what it is, what you recommend and why, and ask for their "
        "go-ahead."
    ),
    RunStatus.MAX_STEPS: (
        "\n- The work stopped before it was finished. Say what is done, what is still open, "
        "and that you can carry on if they want."
    ),
}


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
        toolbox: Toolbox | None = None,
        searcher: Searcher | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.right = right
        self.left = left
        self.config = config or HarnessConfig()
        self._actuator = act
        self._on_event = on_event
        self.toolbox = toolbox
        """Tools the right may build options on. An option that calls one is carried out
        by calling it; `act` handles every option that does not."""
        self.searcher = searcher
        """Where the answer check looks up claims the left is unsure of. None: no lookups."""
        self.memory = memory
        """Notes kept across conversations. None: the agent remembers nothing between them."""

    async def run(self, goal: str, *, context: str = "", conversation: Sequence[Turn] = ()) -> RunResult:
        """Pursue `goal` until the right judges it met, a human is needed, or steps run out.

        `conversation` holds the earlier exchanges when `goal` is a reply. See Conversation.
        """
        state = WorldState(goal=goal, context=context, conversation=list(conversation), tools=self._tools())
        trace = Trace(self._on_event)
        if self.memory is not None:
            state.memory = await self._recall(state, trace)

        plan = await self.right.plan(state)
        state.situation, state.plan = plan.situation, plan.steps
        trace.emit(
            Actor.RIGHT,
            "plan",
            plan.situation,
            *(f"{i}. {s}" for i, s in enumerate(plan.steps, 1)),
            data={"plan": plan.model_dump(mode="json")},
        )

        return await self._continue(state, trace)

    async def resume(self, state: WorldState, decision: HumanDecision) -> RunResult:
        """Continue a run that stopped for a human, with that human's decision.

        `state` is `RunResult.state` from the stopped run. An approval carries out the chosen
        option as a step committed by the human: the policy gate refused it to the
        hemispheres, and a person's sign-off is exactly what it asked for. A rejection is
        recorded and the right proposes again. Either way the right reintegrates, and the
        run continues for up to `max_steps` more steps, then answers as usual.
        """
        last = state.history[-1] if state.history else None
        if last is None or last.decision.outcome is not Outcome.FLAG_HUMAN:
            raise ValueError("this run is not waiting for a human")
        state = state.model_copy(deep=True)
        state.tools = self._tools()  # the tools connected now, which may differ from before
        trace = Trace(self._on_event)
        index = len(state.history) + 1
        note = decision.note.strip()

        action: ActionResult | None = None
        if decision.approve:
            option = last.proposal.option(decision.option_id)
            if option is None:
                offered = ", ".join(o.id for o in last.proposal.options)
                raise ValueError(f"'{decision.option_id}' was not one of the options ({offered})")
            reason = "approved by a human" + (f": {note}" if note else "")
            human = last.decision.model_copy(
                update={"outcome": Outcome.PROCEED, "option_id": option.id, "committed_by": HUMAN, "reasons": [reason]}
            )
            trace.emit(Actor.HUMAN, "approve", f"approves '{option.id}'", *([note] if note else []), step=index,
                       data={"decision": human.model_dump(mode="json"), "option": option.model_dump(mode="json")})  # fmt: skip
            action = await self._act(option, state)
            trace.emit(
                Actor.HARNESS,
                "act",
                f"{option.id} → {action.output}" if action.ok else f"{option.id} FAILED: {action.output}",
                step=index,
                data={"action": action.model_dump(mode="json")},
            )
        else:
            reason = "a human rejected these options" + (f": {note}" if note else "")
            human = last.decision.model_copy(
                update={"outcome": Outcome.REVISE, "option_id": None, "committed_by": None, "reasons": [reason]}
            )
            trace.emit(Actor.HUMAN, "reject", "rejects these options", *([note] if note else []), step=index,
                       data={"decision": human.model_dump(mode="json")})  # fmt: skip

        record = StepRecord(index=index, proposal=last.proposal, decision=human, action=action)
        await self._reintegrate(state, record, trace)
        if record.reintegration is not None and record.reintegration.goal_met:
            return await self._finish(state, trace, RunStatus.DONE)
        return await self._continue(state, trace)

    async def _continue(self, state: WorldState, trace: Trace) -> RunResult:
        status = RunStatus.MAX_STEPS
        for _ in range(self.config.max_steps):
            record = await self.step(state, trace)
            if record.decision.outcome is Outcome.FLAG_HUMAN:
                status = RunStatus.NEEDS_HUMAN
                break
            if record.reintegration is not None and record.reintegration.goal_met:
                status = RunStatus.DONE
                break
        return await self._finish(state, trace, status)

    async def _finish(self, state: WorldState, trace: Trace, status: RunStatus) -> RunResult:
        # The run ends where McGilchrist's cycle does: back in the right, which speaks for
        # the whole. The reply is the agent's settled view, in one voice. Before the person
        # sees it, the left checks its claims and the right takes back what does not hold.
        draft = await self._draft(answer_prompt(status, state), state, trace)
        report, check = draft, None
        if self.config.check_answers:
            report, check = await self.check_answer(draft, state, trace)
        trace.emit(Actor.RIGHT, "report", report, data={"report": report})
        if self.memory is not None:
            await self._remember(report, state, trace)
        trace.emit(
            Actor.HARNESS,
            "end",
            f"{status.upper()} after {len(state.history)} step(s)",
            data={"status": status, "steps": len(state.history)},
        )
        return RunResult(status=status, state=state, report=report, check=check, trace=trace.events)

    async def check_answer(self, draft: str, state: WorldState, trace: Trace) -> tuple[str, AnswerCheck | None]:
        """Right lists the draft's claims, left judges each, right revises what does not hold.

        Best-effort: the answer already exists, so a failure anywhere here leaves the draft
        standing, with the failure recorded, rather than ending the run.
        """
        try:
            claims = await self.right.claims(draft, state)
        except RightHemisphereError as exc:
            trace.emit(Actor.RIGHT, "draft", "could not list claims; the answer is unchecked", str(exc))
            return draft, AnswerCheck(draft=draft, error=str(exc))
        claims = [claim.model_copy(update={"id": f"c{i}"}) for i, claim in enumerate(claims, 1)]
        if not claims:
            return draft, None
        trace.emit(
            Actor.RIGHT,
            "draft",
            f"drafted the answer; {len(claims)} claim(s) to check",
            *(f"• {claim.text}" for claim in claims),
            data={"draft": draft, "claims": [claim.model_dump(mode="json") for claim in claims]},
        )

        # The left judges each claim with only the request in view, never the right's own
        # reading, so it cannot simply agree with the draft.
        try:
            result = await self.left.decide(self._check_focus(state), self._claim_questions(claims))
        except LeftHemisphereError as exc:
            trace.emit(Actor.LEFT, "check", f"unavailable: {exc}", data={"error": str(exc)})
            return draft, AnswerCheck(draft=draft, error=str(exc))

        checks = []
        for claim in claims:
            answer = result.answers[CLAIM + claim.id]
            assert isinstance(answer, NoulAnswer)  # LeftHemisphere.decide checked it against the question
            checks.append(
                ClaimCheck(claim=claim, probability=answer.probability, verdict=self._claim_verdict(answer.probability))
            )
        latency = f", {result.latency_ms:.0f} ms" if result.latency_ms is not None else ""
        if self.searcher is not None and any(check.verdict is not ClaimVerdict.HOLDS for check in checks):
            checks = await self._look_up(checks, state, trace)
        weak = [check for check in checks if check.verdict is not ClaimVerdict.HOLDS]
        trace.emit(
            Actor.LEFT,
            "check",
            f"{len(checks) - len(weak)} of {len(checks)} claim(s) hold  [{result.model or self.left.name}{latency}]",
            *(f"{check.probability:.2f} {check.verdict}: {check.claim.text}" for check in checks),
            data={
                "checks": [check.model_dump(mode="json") for check in checks],
                "looked_up": self.searcher.name if self.searcher is not None else None,
                "model": result.model or self.left.name,
                "latency_ms": result.latency_ms,
                "thresholds": {"holds": self.config.permit_yes_threshold, "doubtful": self.config.permit_no_threshold},
            },
        )
        if not weak:
            return draft, AnswerCheck(draft=draft, checks=checks)

        try:
            revised = await self.right.revise(draft, checks, state)
        except RightHemisphereError as exc:
            trace.emit(Actor.RIGHT, "revise", "could not revise; the draft stands", str(exc))
            return draft, AnswerCheck(draft=draft, checks=checks, error=str(exc))
        doubtful = sum(check.verdict is ClaimVerdict.DOUBTFUL for check in weak)
        trace.emit(
            Actor.RIGHT,
            "revise",
            f"revised the answer: {doubtful} doubtful, {len(weak) - doubtful} unsure claim(s)",
            data={"revised": revised},
        )
        return revised, AnswerCheck(draft=draft, checks=checks, revised=True)

    async def _recall(self, state: WorldState, trace: Trace) -> list[str]:
        """The saved notes that bear on this request, as the left judges them.

        The left is asked one yes/no question per note: exactly the narrow, typed judgement it
        is for. If it fails, the most recent notes are used instead.
        """
        assert self.memory is not None
        saved = self.memory.notes
        if not saved:
            return []
        # A preference is about how to work with the person, so it always applies. Only facts
        # are put to the left, which judges each against the request.
        preferences = [note for note in saved if note.kind == "preference"]
        notes = [note for note in saved if note.kind != "preference"][-MAX_RECALL_CANDIDATES:]
        relevant, how = await self._recall_facts(state, notes) if notes else ([], "")
        recalled = preferences + relevant
        trace.emit(
            Actor.LEFT,
            "recall",
            f"recalled {len(preferences)} preference(s) and {len(relevant)} of {len(notes)} fact(s)" + (f": {how}" if how else ""),
            *(f"• {note.text}" for note in recalled),
            data={"recalled": [n.model_dump(mode="json") for n in recalled], "considered": len(notes), "preferences": len(preferences)},
        )
        return [note.text for note in recalled]

    async def _recall_facts(self, state: WorldState, notes: "list[Note]") -> "tuple[list[Note], str]":
        questions: dict[str, Question] = {
            RECALL + note.id: NoulQuestion(
                instructions=f"Would this note about the person help with their request?\nNote: {note.text}",
                yes_when="It bears on what they are asking or how they want it answered.",
                no_when="It is about something else.",
            )
            for note in notes
        }
        view: dict[str, Any] = {"request": state.goal}
        if state.conversation:
            view["previous_message"] = state.conversation[-1].request
        try:
            result = await self.left.decide(view, questions)
            relevant = [
                note for note in notes
                if (answer := result.answers[RECALL + note.id]) and isinstance(answer, NoulAnswer) and answer.probability > 0.5
            ]  # fmt: skip
            how = "the left judged them relevant"
        except LeftHemisphereError:
            relevant = notes[-MAX_RECALLED_WITHOUT_LEFT:]
            how = "the left was unavailable, so the most recent were used"
        return relevant, how

    async def _remember(self, answer: str, state: WorldState, trace: Trace) -> None:
        """Let the right keep what is worth keeping. Best-effort: a failure keeps nothing."""
        assert self.memory is not None
        try:
            texts = await self.right.remember(answer, state)
        except RightHemisphereError:
            return
        added = self.memory.add(texts, source=state.goal)
        if added:
            trace.emit(
                Actor.RIGHT,
                "remember",
                f"remembered {len(added)} note(s) for later conversations",
                *(f"• {note.text}" for note in added),
                data={"notes": [n.model_dump(mode="json") for n in added]},
            )

    async def _look_up(self, checks: list[ClaimCheck], state: WorldState, trace: Trace) -> list[ClaimCheck]:
        """Search for evidence on the claims the left was not sure of, and let it judge them again.

        Best-effort: a failed search finds nothing, a failed second pass keeps the first.
        """
        assert self.searcher is not None
        unsure = [check for check in checks if check.verdict is not ClaimVerdict.HOLDS]
        found = await self.searcher.search_all([check.claim.query or check.claim.text for check in unsure])
        evidence = {check.claim.id: sources for check, sources in zip(unsure, found, strict=True) if sources}
        trace.emit(
            Actor.HARNESS,
            "lookup",
            f"looked up {len(unsure)} claim(s) on {self.searcher.name}; found evidence for {len(evidence)}",
            *(f"{check.claim.text} → {', '.join(s.title for s in sources) or 'nothing'}" for check, sources in zip(unsure, found, strict=True)),
            data={
                "source": self.searcher.name,
                "results": [
                    {"claim": check.claim.model_dump(mode="json"), "sources": [s.model_dump(mode="json") for s in sources]}
                    for check, sources in zip(unsure, found, strict=True)
                ],
            },
        )
        if not evidence:
            return checks

        focus = self._check_focus(state)
        focus["evidence"] = {
            claim_id: [{"source": f"{s.title} ({s.url})", "text": s.snippet} for s in sources] for claim_id, sources in evidence.items()
        }
        rechecked = [check for check in unsure if check.claim.id in evidence]
        questions = self._claim_questions([check.claim for check in rechecked], with_evidence=True)
        try:
            result = await self.left.decide(focus, questions)
        except LeftHemisphereError:
            return checks
        updated = {}
        for check in rechecked:
            answer = result.answers[CLAIM + check.claim.id]
            assert isinstance(answer, NoulAnswer)
            updated[check.claim.id] = check.model_copy(
                update={
                    "probability": answer.probability,
                    "verdict": self._claim_verdict(answer.probability),
                    "first_probability": check.probability,
                    "sources": evidence[check.claim.id],
                }
            )
        return [updated.get(check.claim.id, check) for check in checks]

    async def _draft(self, prompt: str, state: WorldState, trace: Trace) -> str:
        """The answer's first draft, streamed to whoever is listening as it is written.

        If streaming fails part of the way, the pieces are withdrawn and the draft is
        generated again in one go.
        """
        if not trace.listening:
            return await self.right.generate(prompt, state)
        pieces: list[str] = []
        try:
            async for piece in self.right.stream(prompt, state):
                pieces.append(piece)
                trace.emit_transient(Actor.RIGHT, "answer_delta", {"text": piece})
            if "".join(pieces).strip():
                return "".join(pieces)
        except RightHemisphereError:
            pass
        trace.emit_transient(Actor.RIGHT, "answer_delta", {"reset": True})
        return await self.right.generate(prompt, state)

    def _check_focus(self, state: WorldState) -> dict[str, Any]:
        view: dict[str, Any] = {"request": state.goal}
        if state.context:
            view["context"] = state.context
        if state.conversation:
            # What the person said earlier may hold facts; what the agent answered is not evidence.
            view["earlier_messages"] = [turn.request for turn in state.conversation[-MAX_EARLIER:]]
        if observed := state.observations():
            view["tool_results"] = observed  # observed, not asserted: evidence
        return view

    def _claim_questions(self, claims: list[Claim], *, with_evidence: bool = False) -> dict[str, Question]:
        basis = (
            "Judge it first on the evidence found for it (under evidence, keyed by the statement's id "
            "'{id}'), then on the context given and what the tools returned."
            if with_evidence
            else "Judge it on the context given, on what the tools returned, and on well-established general knowledge."
        )
        return {
            CLAIM + claim.id: NoulQuestion(
                instructions=f"Is this statement accurate? {basis.format(id=claim.id)}\nStatement: {claim.text}",
                yes_when="The statement is accurate, or a fair summary of what is well established.",
                no_when="The statement is false, exaggerated, out of date, or not supported.",
            )
            for claim in claims
        }

    def _claim_verdict(self, probability: float) -> ClaimVerdict:
        if probability >= self.config.permit_yes_threshold:
            return ClaimVerdict.HOLDS
        if probability <= self.config.permit_no_threshold:
            return ClaimVerdict.DOUBTFUL
        return ClaimVerdict.UNSURE

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
        decision = self._gate(authority, left, failure, proposal)
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
        await self._reintegrate(state, record, trace)
        return record

    async def _reintegrate(self, state: WorldState, record: StepRecord, trace: Trace) -> None:
        reintegration = await self.right.reintegrate(state, record)
        record.reintegration = reintegration
        state.situation = reintegration.situation
        state.history.append(record)
        trace.emit(
            Actor.RIGHT,
            "reintegrate",
            reintegration.situation,
            "goal met" if reintegration.goal_met else "goal not yet met",
            step=record.index,
            data={"reintegration": reintegration.model_dump(mode="json")},
        )

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
                        f"human sign-off?\nAction: {option.as_action()}"
                    )
                )
        return questions

    def _authority(self, proposal: Proposal) -> Authority:
        """The right's own labels decide how much the left's verdict counts."""
        if proposal.stakes is Stakes.HIGH or proposal.open_ended:
            return Authority.ADVISE
        return Authority.DECIDE

    def _gate(
        self, authority: Authority, left: LeftResult | None, failure: str, proposal: Proposal | None = None
    ) -> Decision:
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
        tool = self._tool_needing_signoff(proposal.option(select.choice) if proposal else None)
        if tool:
            reasons.append(f"'{select.choice}' calls {tool}, which needs a person's sign-off")

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
        elif tool := self._tool_needing_signoff(proposal.option(evaluation.option_id)):
            reason = f"the right chose '{evaluation.option_id}', which calls {tool}: that needs a person's sign-off"
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
        if option.tool:
            if self.toolbox is None:
                return ActionResult(option_id=option.id, ok=False, output="no tools are connected", tool=option.tool)
            result = await self.toolbox.call(option.tool, option.arguments)
            return ActionResult(option_id=option.id, ok=result.ok, output=result.output, tool=option.tool)
        try:
            output = await self._actuator(option, state)
        except Exception as exc:  # a failed action is information for the right, not a crash
            return ActionResult(option_id=option.id, ok=False, output=f"{type(exc).__name__}: {exc}")
        return ActionResult(option_id=option.id, ok=True, output=output)

    def _tools(self) -> list[ToolSpec]:
        return list(self.toolbox.tools) if self.toolbox else []

    def _tool_needing_signoff(self, option: Option | None) -> str | None:
        """The tool an option calls, if that tool may only run with a person's sign-off.

        A tool the harness does not know counts as needing it: better to ask than to guess.
        """
        if option is None or not option.tool:
            return None
        spec = self.toolbox.get(option.tool) if self.toolbox else None
        return option.tool if spec is None or spec.needs_approval else None

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


def answer_prompt(status: RunStatus, state: WorldState) -> str:
    """The instruction for the reply that ends a run, which is all the person sees."""
    pending = "a decision the work could not settle on its own"
    if status is RunStatus.NEEDS_HUMAN and state.history:
        last = state.history[-1]
        wanted = last.evaluation.option_id if last.evaluation else None
        option = last.proposal.option(wanted or last.decision.left_choice)
        if option is not None:
            pending = option.description
        else:
            pending = "a choice between: " + "; ".join(o.description for o in last.proposal.options)
    ending = _ENDINGS[status].format(pending=pending)
    if state.memory:
        # Recalled notes are background for every step; here they are a standing instruction.
        ending += (
            "\n- Follow what you remember about this person, above all how they like to be "
            "answered. It overrides the format suggested above."
        )
    return ANSWER_PROMPT.format(ending=ending)


def _distribution(probabilities: dict[str, float], *, ranked: bool = True) -> str:
    items = list(probabilities.items())
    if ranked:
        items.sort(key=lambda item: item[1], reverse=True)
    return " · ".join(f"{key} {value:.2f}" for key, value in items)
