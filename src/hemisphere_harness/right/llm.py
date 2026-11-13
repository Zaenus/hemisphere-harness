"""A right hemisphere for any model that turns a chat into text."""

import json
from abc import abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel

from ..errors import RightHemisphereError
from ..schemas import Decision, Evaluation, Plan, Proposal, Reintegration, StepRecord, WorldState
from . import prompts
from .base import RightHemisphere

ChatMessage = dict[str, str]
"""{"role": "system" | "user" | "assistant", "content": "..."}"""

T = TypeVar("T", bound=BaseModel)


class LLMRightHemisphere(RightHemisphere):
    """Drives any text-generating model as the right hemisphere.

    A subclass supplies one method, `complete`. The prompts, the parsing of each reply
    into its schema, and the repair attempt are shared, so adding a provider means
    writing its transport and nothing else.

    Structured replies are requested in the prompt and validated here. A reply that
    fails validation is sent back with the error, `repair_attempts` times, before the
    call is given up as malformed.
    """

    repair_attempts: int = 1

    @abstractmethod
    async def complete(self, messages: list[ChatMessage], *, json_reply: bool = False) -> str:
        """Send `messages` to the model and return the text of its reply.

        The system message, when there is one, comes first. `json_reply` is True when
        the caller will parse the reply as a JSON object: a provider with a native JSON
        mode may switch it on, and any other can ignore the flag.

        Raise RightHemisphereError on any failure.
        """

    async def plan(self, state: WorldState) -> Plan:
        return await self._structured(Plan, prompts.plan_prompt(state))

    async def propose(self, state: WorldState) -> Proposal:
        return await self._structured(Proposal, prompts.propose_prompt(state))

    async def evaluate(self, state: WorldState, proposal: Proposal, decision: Decision) -> Evaluation:
        return await self._structured(Evaluation, prompts.evaluate_prompt(state, proposal, decision))

    async def reintegrate(self, state: WorldState, step: StepRecord) -> Reintegration:
        return await self._structured(Reintegration, prompts.reintegrate_prompt(state, step))

    async def generate(self, prompt: str, state: WorldState | None = None) -> str:
        user = prompt if state is None else f"{prompts.render_state(state)}\n\n{prompt}"
        return await self.complete(
            [{"role": "system", "content": prompts.ROLE}, {"role": "user", "content": user}]
        )

    async def _structured(self, schema: type[T], user: str) -> T:
        messages: list[ChatMessage] = [
            {"role": "system", "content": f"{prompts.ROLE}\n\n{prompts.JSON_ONLY}"},
            {"role": "user", "content": user},
        ]
        problem = ""
        for _ in range(self.repair_attempts + 1):
            reply = await self.complete(messages, json_reply=True)
            try:
                return schema.model_validate(_extract_json(reply))
            except ValueError as exc:  # bad JSON, or JSON that does not fit the schema
                problem = str(exc)
                messages += [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": prompts.REPAIR.format(problem=problem)},
                ]
        raise RightHemisphereError(
            f"the right hemisphere returned a malformed {schema.__name__} "
            f"{self.repair_attempts + 1} times: {problem}"
        )


def _extract_json(text: str) -> Any:
    """Parse the JSON object in a reply, tolerating code fences or prose around it."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("the reply contains no JSON object")
    return json.loads(text[start : end + 1])
