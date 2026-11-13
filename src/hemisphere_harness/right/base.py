"""The Master's contract."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..memory import Memo
from ..schemas import (
    Claim,
    ClaimCheck,
    Decision,
    Evaluation,
    Plan,
    Proposal,
    Reintegration,
    StepRecord,
    WorldState,
)


class RightHemisphere(ABC):
    """Holds the whole: the goal, its context, and what everything so far adds up to.

    The right is the only part of the agent that generates. It opens each step by
    proposing an option space and closes each step by folding the result back into its
    reading of the situation. In between, the left may narrow the options to one, but
    whatever the left is not entitled to settle alone comes back here first.

    Every method raises RightHemisphereError on failure. The harness does not catch it.
    """

    name: str = "right"

    @abstractmethod
    async def plan(self, state: WorldState) -> Plan:
        """Read the situation, including what the goal implies but does not say, and sketch a plan."""

    @abstractmethod
    async def propose(self, state: WorldState) -> Proposal:
        """Generate two to four candidate next actions, and say how far the left can be trusted with them.

        The proposal's `stakes` and `open_ended` labels are how the right decides whether
        left-hemisphere precision is enough for this step.
        """

    @abstractmethod
    async def evaluate(self, state: WorldState, proposal: Proposal, decision: Decision) -> Evaluation:
        """Weigh a left verdict in context, before anything is committed.

        Called whenever the left was not allowed to commit alone: the decision was
        high-stakes or open-ended, the left was unsure, the policy gate objected, or the
        left failed. Return commit (naming an option), revise, or ask_human.
        """

    @abstractmethod
    async def reintegrate(self, state: WorldState, step: StepRecord) -> Reintegration:
        """Fold a finished step into the whole: rewrite the situation, judge whether the goal is met.

        Called at the end of every step, whatever happened in it. Its result is the only
        thing the harness ever writes into the world state's `situation`.
        """

    @abstractmethod
    async def generate(self, prompt: str, state: WorldState | None = None) -> str:
        """Free-form text. Anything the agent says in its own words comes from here."""

    async def stream(self, prompt: str, state: WorldState | None = None) -> AsyncIterator[str]:
        """Like `generate`, but yields the text in pieces as it is written.

        Not abstract: the default yields `generate`'s whole result as one piece.
        """
        yield await self.generate(prompt, state)

    async def claims(self, answer: str, state: WorldState) -> list[Claim]:
        """The factual claims in a draft answer that are worth checking.

        Each claim must make sense on its own, because the left checks it without seeing
        the draft. Not abstract: the default lists none, which skips the check.
        """
        return []

    async def remember(self, answer: str, state: WorldState) -> "list[Memo]":
        """What, if anything, from this run is worth remembering in later conversations, and of
        what kind: a preference (always recalled) or a fact (recalled when relevant).

        Not abstract: the default remembers nothing.
        """
        return []

    async def revise(self, answer: str, checks: list[ClaimCheck], state: WorldState) -> str:
        """Rewrite a draft answer in the light of the left's check of its claims.

        The check is evidence, not the last word. The default returns the draft unchanged.
        """
        return answer

    async def aclose(self) -> None:
        """Release any network resources."""
