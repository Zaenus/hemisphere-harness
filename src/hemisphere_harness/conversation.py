"""A conversation with the harness: send a message, read the answer, reply.

    conversation = Conversation(harness, context="Facts that hold for the whole conversation")
    result = await conversation.send("Is Bitcoin decentralized?")
    result = await conversation.send("Compare it with Ethereum.")

Every message is a full run: the right plans, the left decides, the answer is checked.
What carries over is the conversation itself. Each run sees the earlier messages and
answers, the right in full and the left only the last exchange, briefly.

When a run stops for a human, `approve` or `reject` continues that same run with the
person's decision (see `Harness.resume`). `send` instead starts a new run, which may
reach the same sign-off again.
"""

from .harness import Harness
from .schemas import HumanDecision, Option, RunResult, RunStatus, Turn


class Conversation:
    def __init__(self, harness: Harness, *, context: str = "") -> None:
        self.harness = harness
        self.context = context
        self.turns: list[Turn] = []
        self.last: RunResult | None = None

    async def send(self, message: str) -> RunResult:
        """A new message from the person: a new run that remembers the conversation so far."""
        result = await self.harness.run(message, context=self.context, conversation=self.turns)
        return self._record(message, result)

    @property
    def waiting(self) -> list[Option]:
        """The options the last run stopped on for a human, or nothing if it did not."""
        if self.last is None or self.last.status is not RunStatus.NEEDS_HUMAN or not self.last.state.history:
            return []
        return list(self.last.state.history[-1].proposal.options)

    async def approve(self, option_id: str, note: str = "") -> RunResult:
        """Sign off one of the options the last run stopped on, and let it continue."""
        option = next((o for o in self.waiting if o.id == option_id), None)
        if option is None:
            raise ValueError(self._nothing_waiting() or f"'{option_id}' is not one of: {', '.join(o.id for o in self.waiting)}")
        return await self._resume(HumanDecision(approve=True, option_id=option_id, note=note), f"Approved: {option.description}")

    async def reject(self, note: str = "") -> RunResult:
        """Turn down the options the last run stopped on. The right proposes again."""
        if problem := self._nothing_waiting():
            raise ValueError(problem)
        return await self._resume(HumanDecision(approve=False, note=note), "Rejected those options")

    async def _resume(self, decision: HumanDecision, said: str) -> RunResult:
        assert self.last is not None
        result = await self.harness.resume(self.last.state, decision)
        note = decision.note.strip()
        return self._record(said + (f" — {note}" if note else ""), result)

    def _nothing_waiting(self) -> str:
        return "" if self.waiting else "nothing is waiting for a decision"

    def _record(self, message: str, result: RunResult) -> RunResult:
        self.turns.append(Turn(request=message, answer=result.report))
        self.last = result
        return result
