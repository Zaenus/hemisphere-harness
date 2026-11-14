"""Put a past run's decisions to a different left hemisphere, and see where it would differ.

    hemisphere-harness replay 20261113-201108-c0dd9c --left-model tev1:0.8b

Each step the left decided is asked again exactly as it was asked then: the same options,
the same view of the situation, the same policy questions. The new answers go through the
same gate, so the replay shows what the other model would have picked and whether the
harness would have let it act alone. What the right would have done next cannot be
replayed, so each step is judged on its own, with the history as it really happened.
"""

from collections.abc import Sequence
from typing import Any, cast

from pydantic import BaseModel, Field

from .config import HarnessConfig
from .errors import LeftHemisphereError
from .harness import Harness
from .left.base import LeftHemisphere
from .schemas import Hemisphere, Outcome, StepRecord, WorldState
from .tools import Toolbox, ToolResult, ToolSpec
from .trace import TraceEvent

HUMAN_REASONS = ("approved by a human", "a human rejected")


class Pick(BaseModel):
    choice: str | None = None
    probability: float | None = None
    permits: dict[str, float] = Field(default_factory=dict)
    left_commits: bool = False
    reasons: list[str] = Field(default_factory=list)
    error: str | None = None


class StepReplay(BaseModel):
    step: int
    options: dict[str, str]
    authority: str
    before: Pick
    after: Pick

    @property
    def choice_changed(self) -> bool:
        return self.before.choice != self.after.choice

    @property
    def commit_changed(self) -> bool:
        return self.before.left_commits != self.after.left_commits


class Replay(BaseModel):
    run_id: str
    goal: str
    left_before: str
    left_after: str
    steps: list[StepReplay]

    @property
    def differing(self) -> list[StepReplay]:
        return [s for s in self.steps if s.choice_changed or s.commit_changed]


async def replay(
    left: LeftHemisphere,
    state: WorldState,
    *,
    config: HarnessConfig,
    events: Sequence[TraceEvent] = (),
    run_id: str = "",
    left_before: str = "",
) -> Replay:
    """Ask `left` every question the run's left was asked, step by step."""
    harness = Harness(cast(Any, None), left, config=config, toolbox=_Specs(state.tools) if state.tools else None)
    steps = []
    for k, record in enumerate(state.history):
        if any(reason.startswith(HUMAN_REASONS) for reason in record.decision.reasons):
            continue  # a person's sign-off, not a decision the left was asked
        seen = state.model_copy(update={"history": state.history[:k], "situation": _situation_before(state, k, events)})
        authority = harness._authority(record.proposal)
        after = Pick()
        try:
            answers = await left.decide(seen.focus(), harness.build_questions(seen, record.proposal))
        except LeftHemisphereError as exc:
            after.error = str(exc)
        else:
            gated = harness._gate(authority, answers, "", record.proposal)
            after = Pick(
                choice=gated.left_choice,
                probability=gated.probability,
                permits=gated.permits,
                left_commits=gated.outcome is Outcome.PROCEED,
                reasons=gated.reasons,
            )
        steps.append(
            StepReplay(
                step=record.index,
                options={o.id: o.description for o in record.proposal.options},
                authority=authority.value,
                before=_before(record),
                after=after,
            )
        )
    return Replay(run_id=run_id, goal=state.goal, left_before=left_before, left_after=left.name, steps=steps)


def format_replay(result: Replay) -> str:
    def pick(p: Pick) -> str:
        if p.error:
            return f"failed: {p.error}"[:60]
        if p.choice is None:
            return "unavailable"
        alone = "commits alone" if p.left_commits else "back to the right"
        return f"{p.choice} p={p.probability:.2f}, {alone}"

    width = max([len(pick(s.before)) for s in result.steps] + [10])
    lines = [
        f"Run {result.run_id}: {result.goal[:100]}",
        f"Before: {result.left_before or 'the left that ran'}   After: {result.left_after}",
        "",
        f"  step  {'before':<{width}}   after",
    ]
    for s in result.steps:
        mark = "  ← differs" if s.choice_changed or s.commit_changed else ""
        lines.append(f"  {s.step:>4}  {pick(s.before):<{width}}   {pick(s.after)}{mark}")
        if s.commit_changed and s.after.reasons:
            lines.append(f"  {'':>4}  {'':<{width}}   why: {'; '.join(s.after.reasons)}")
    n = len(result.differing)
    lines += ["", f"{n} of {len(result.steps)} step(s) would have gone differently." if result.steps else "No steps the left decided."]
    return "\n".join(lines)


def _before(record: StepRecord) -> Pick:
    decision = record.decision
    if record.left is None:
        return Pick(error="the left was unavailable")
    return Pick(
        choice=decision.left_choice,
        probability=decision.probability,
        permits=decision.permits,
        left_commits=decision.committed_by == Hemisphere.LEFT,
        reasons=[],
    )


def _situation_before(state: WorldState, k: int, events: Sequence[TraceEvent]) -> str:
    if k > 0 and state.history[k - 1].reintegration is not None:
        return state.history[k - 1].reintegration.situation  # type: ignore[union-attr]
    plan = next((e for e in events if e.phase == "plan"), None)
    if plan is not None:
        situation = plan.data.get("plan", {}).get("situation")
        if isinstance(situation, str):
            return situation
    return ""


class _Specs(Toolbox):
    """The tools as the run knew them: enough for the gate to tell which need sign-off."""

    def __init__(self, tools: list[ToolSpec]) -> None:
        self.tools = list(tools)

    async def _call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        raise RuntimeError("a replay calls no tools")
