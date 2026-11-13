"""A scripted right hemisphere for tests and offline demos."""

from collections import deque
from collections.abc import Iterable
from typing import TypeVar

from ..errors import RightHemisphereError
from ..schemas import Decision, Evaluation, Plan, Proposal, Reintegration, StepRecord, WorldState
from .base import RightHemisphere

T = TypeVar("T")


class ScriptedRightHemisphere(RightHemisphere):
    """Replays prepared plans, proposals, evaluations and reintegrations in order.

    Nothing here thinks. It exists so the loop can be exercised deterministically, and
    it raises as soon as the loop asks for something the script did not anticipate.
    `calls` records which methods were invoked, in order.
    """

    name = "scripted"

    def __init__(
        self,
        *,
        plan: Plan,
        proposals: Iterable[Proposal],
        evaluations: Iterable[Evaluation] = (),
        reintegrations: Iterable[Reintegration] = (),
        report: str = "",
    ) -> None:
        self._plan = plan
        self._proposals = deque(proposals)
        self._evaluations = deque(evaluations)
        self._reintegrations = deque(reintegrations)
        self._report = report
        self.calls: list[str] = []

    async def plan(self, state: WorldState) -> Plan:
        self.calls.append("plan")
        return self._plan

    async def propose(self, state: WorldState) -> Proposal:
        return self._next(self._proposals, "propose")

    async def evaluate(self, state: WorldState, proposal: Proposal, decision: Decision) -> Evaluation:
        return self._next(self._evaluations, "evaluate")

    async def reintegrate(self, state: WorldState, step: StepRecord) -> Reintegration:
        return self._next(self._reintegrations, "reintegrate")

    async def generate(self, prompt: str, state: WorldState | None = None) -> str:
        self.calls.append("generate")
        return self._report or (state.situation if state else "")

    def _next(self, queue: deque[T], method: str) -> T:
        self.calls.append(method)
        if not queue:
            raise RightHemisphereError(f"the script has nothing left for {method}()")
        return queue.popleft()
