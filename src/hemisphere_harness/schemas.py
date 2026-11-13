"""Typed vocabulary shared by both hemispheres and the harness.

Three layers lead from the left's raw material to the harness's verdict:

    Question / Answer   one typed question and its calibrated answer: the left's whole language
    LeftResult          every answer from a single left-hemisphere pass
    Decision            what the harness makes of that pass: who commits, to what, and why

What the right hemisphere produces (Plan, Proposal, Evaluation, Reintegration) is
structured too, but it is generated. The left only ever selects, scores or gates.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from .trace import TraceEvent

MIN_OPTIONS = 2
MAX_OPTIONS = 4


class Hemisphere(StrEnum):
    RIGHT = "right"
    LEFT = "left"


HUMAN = "human"
"""Who committed a step that a person signed off. See `Harness.resume`."""


class Stakes(StrEnum):
    LOW = "low"
    HIGH = "high"


class Authority(StrEnum):
    """How much the left's verdict counts on a given step. Set from the right's own labels."""

    DECIDE = "decide"  # routine and low-stakes: the left may commit if it is sure enough
    ADVISE = "advise"  # open-ended or high-stakes: the left informs, the right commits


class Outcome(StrEnum):
    PROCEED = "proceed"
    ESCALATE_TO_RIGHT = "escalate_to_right"  # transient: resolved within the same step
    REVISE = "revise"
    FLAG_HUMAN = "flag_human"


class Verdict(StrEnum):
    COMMIT = "commit"
    REVISE = "revise"
    ASK_HUMAN = "ask_human"


class RunStatus(StrEnum):
    DONE = "done"
    NEEDS_HUMAN = "needs_human"
    MAX_STEPS = "max_steps"


# --- Questions: what the left can be asked -----------------------------------------------
#
# These mirror Jev's three primitives but use the harness's own field names. The wire
# format (where all three call their rubric `criteria`) lives in left/jev_client.py only.


class NoulQuestion(BaseModel):
    """A yes/no question. The answer is the probability of yes."""

    type: Literal["noul"] = "noul"
    instructions: str
    yes_when: str | None = None
    no_when: str | None = None


class ChoiceQuestion(BaseModel):
    """Pick one of a closed set. Keys are option ids, values describe them."""

    type: Literal["choice"] = "choice"
    instructions: str
    options: dict[str, str | None] = Field(min_length=1, max_length=255)


class ScoreQuestion(BaseModel):
    """Place the state on an ordered rubric. Levels are listed lowest first."""

    type: Literal["score"] = "score"
    instructions: str
    levels: list[str] = Field(min_length=2, max_length=10)


Question = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]


# --- Answers: all the left can say -------------------------------------------------------


class NoulAnswer(BaseModel):
    type: Literal["noul"] = "noul"
    probability: float = Field(ge=0, le=1)
    """Probability that the answer is yes. Near 0.5 means the left cannot tell."""


class ChoiceAnswer(BaseModel):
    type: Literal["choice"] = "choice"
    choice: str
    confidence: float = Field(ge=0, le=1)
    """How concentrated the distribution is. Not the chance that `choice` is right."""
    probabilities: dict[str, float]

    @model_validator(mode="after")
    def _choice_has_a_probability(self) -> Self:
        if self.choice not in self.probabilities:
            raise ValueError(f"choice '{self.choice}' is missing from probabilities")
        return self

    @property
    def probability(self) -> float:
        """Calibrated probability of the chosen option. This is what the harness thresholds on."""
        return self.probabilities[self.choice]

    @classmethod
    def from_probabilities(cls, probabilities: Mapping[str, float]) -> Self:
        """Build an answer the way Jev derives one: top option, concentration as confidence."""
        choice = max(probabilities, key=probabilities.__getitem__)
        n = len(probabilities)
        confidence = 1.0 if n == 1 else (probabilities[choice] - 1 / n) / (1 - 1 / n)
        return cls(
            choice=choice,
            confidence=min(1.0, max(0.0, confidence)),
            probabilities=dict(probabilities),
        )


class ScoreAnswer(BaseModel):
    type: Literal["score"] = "score"
    score: float
    """Probability-weighted mean of the level indices, which start at 0."""
    confidence: float = Field(ge=0, le=1)
    probabilities: dict[int, float]
    legend: dict[int, str] = Field(default_factory=dict)

    @classmethod
    def from_probabilities(cls, probabilities: Sequence[float], levels: Sequence[str] = ()) -> Self:
        """Build an answer from one probability per level, lowest level first."""
        n = len(probabilities)
        mode = max(range(n), key=probabilities.__getitem__)
        spread = sum(p * abs(i - mode) for i, p in enumerate(probabilities))
        uniform_spread = sum(abs(i - (n - 1) / 2) for i in range(n)) / n
        confidence = 1.0 if uniform_spread == 0 else 1 - spread / uniform_spread
        return cls(
            score=sum(i * p for i, p in enumerate(probabilities)),
            confidence=min(1.0, max(0.0, confidence)),
            probabilities=dict(enumerate(probabilities)),
            legend=dict(enumerate(levels)),
        )


Answer = Annotated[NoulAnswer | ChoiceAnswer | ScoreAnswer, Field(discriminator="type")]


class LeftResult(BaseModel):
    """Everything the left returned from one pass. Typed answers, never prose."""

    answers: dict[str, Answer]
    model: str | None = None
    latency_ms: float | None = None


# --- What the right generates ------------------------------------------------------------


class Plan(BaseModel):
    situation: str
    steps: list[str] = Field(default_factory=list)


class Option(BaseModel):
    id: str = Field(min_length=1)
    description: str
    rationale: str = ""


class Proposal(BaseModel):
    """The option space for one step, opened by the right.

    `stakes` and `open_ended` have no defaults: the right must say, every time, whether
    this is a decision the left can be trusted to close alone.
    """

    options: list[Option] = Field(min_length=MIN_OPTIONS, max_length=MAX_OPTIONS)
    rationale: str
    stakes: Stakes
    open_ended: bool

    @model_validator(mode="after")
    def _option_ids_are_unique(self) -> Self:
        ids = [option.id for option in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError(f"option ids must be unique, got {ids}")
        return self

    def option(self, option_id: str | None) -> Option | None:
        return next((option for option in self.options if option.id == option_id), None)


class Evaluation(BaseModel):
    """The right's judgement on a left verdict that the left was not allowed to commit."""

    verdict: Verdict
    option_id: str | None = None
    reasoning: str

    @model_validator(mode="after")
    def _commit_names_an_option(self) -> Self:
        if self.verdict is Verdict.COMMIT and not self.option_id:
            raise ValueError("a 'commit' verdict must name an option_id")
        return self


class Reintegration(BaseModel):
    """The step folded back into the whole: the right's rewritten reading of the situation."""

    situation: str
    goal_met: bool


# --- What the harness records ------------------------------------------------------------


class Decision(BaseModel):
    """The harness's verdict for one step: what the left said, and who committed to what."""

    outcome: Outcome
    authority: Authority

    # What the left said. All empty if the left was unavailable.
    left_choice: str | None = None
    probability: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] = Field(default_factory=dict)
    permits: dict[str, float] = Field(default_factory=dict)

    # What was committed. Set only when outcome is PROCEED.
    option_id: str | None = None
    committed_by: Hemisphere | Literal["human"] | None = None

    reasons: list[str] = Field(default_factory=list)


class ActionResult(BaseModel):
    option_id: str
    ok: bool
    output: str


class StepRecord(BaseModel):
    index: int
    proposal: Proposal
    left: LeftResult | None = None
    decision: Decision
    evaluation: Evaluation | None = None
    action: ActionResult | None = None
    reintegration: Reintegration | None = None

    def summary(self) -> str:
        """One line of history, as both hemispheres see it on later steps."""
        decision = self.decision
        if self.action is not None:
            result = "done" if self.action.ok else "FAILED"
            who = "a human approved" if decision.committed_by == HUMAN else f"the {decision.committed_by} hemisphere chose"
            return f"step {self.index}: {who} '{decision.option_id}' -> {result}: {self.action.output}"
        offered = ", ".join(option.id for option in self.proposal.options)
        why = self.evaluation.reasoning if self.evaluation else "; ".join(decision.reasons)
        if decision.outcome is Outcome.FLAG_HUMAN:
            return f"step {self.index}: nothing done, flagged for a human (offered: {offered}): {why}"
        return f"step {self.index}: nothing done, options rejected (offered: {offered}): {why}"


class Turn(BaseModel):
    """One earlier exchange of a conversation: what the person said, and the agent's answer."""

    request: str
    answer: str


class HumanDecision(BaseModel):
    """A person's call on a step the harness stopped for. See `Harness.resume`."""

    approve: bool
    option_id: str | None = None
    """The option approved. Required to approve; ignored to reject."""
    note: str = ""

    @model_validator(mode="after")
    def _approval_names_an_option(self) -> Self:
        if self.approve and not self.option_id:
            raise ValueError("an approval must name the option_id it approves")
        return self


class WorldState(BaseModel):
    """The running world model. Owned by the harness, written only from what the right produces."""

    goal: str
    """The person's latest message. Earlier ones are in `conversation`."""
    context: str = ""
    conversation: list[Turn] = Field(default_factory=list)
    """Earlier exchanges in the same conversation, oldest first."""
    situation: str = ""
    plan: list[str] = Field(default_factory=list)
    history: list[StepRecord] = Field(default_factory=list)

    def focus(self, recent: int = 3) -> dict[str, Any]:
        """The narrow slice handed to the left hemisphere.

        A fresh dict on every call, so nothing the left does with it reaches the world model.
        Of an ongoing conversation it sees only the last exchange, briefly.
        """
        view: dict[str, Any] = {"goal": self.goal, "situation": self.situation}
        if self.context:
            view["context"] = self.context
        if self.conversation:
            last = self.conversation[-1]
            view["previous_exchange"] = {"person": _clip(last.request, 300), "agent": _clip(last.answer, 400)}
        if self.history:
            view["recent_steps"] = [step.summary() for step in self.history[-recent:]]
        return view


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --- Checking the answer -------------------------------------------------------------------


class ClaimVerdict(StrEnum):
    HOLDS = "holds"
    UNSURE = "unsure"
    DOUBTFUL = "doubtful"


class Claim(BaseModel):
    """One factual statement from a draft answer, phrased to stand on its own."""

    id: str = ""
    text: str = Field(min_length=1)


class ClaimList(BaseModel):
    """What the right lists from its own draft for the left to check."""

    claims: list[Claim] = Field(default_factory=list)


class ClaimCheck(BaseModel):
    claim: Claim
    probability: float = Field(ge=0, le=1)
    """The left's probability that the claim holds."""
    verdict: ClaimVerdict


class AnswerCheck(BaseModel):
    """How the final answer was checked before the person saw it."""

    draft: str
    checks: list[ClaimCheck] = Field(default_factory=list)
    revised: bool = False
    error: str | None = None
    """Why the check could not finish, if it could not. The draft then stands."""


class RunResult(BaseModel):
    status: RunStatus
    state: WorldState
    report: str = ""
    """The reply to the person: the agent's answer, in one voice."""
    check: AnswerCheck | None = None
    """How that reply was checked. None if checking was off or there was nothing to check."""
    trace: list[TraceEvent] = Field(default_factory=list)
