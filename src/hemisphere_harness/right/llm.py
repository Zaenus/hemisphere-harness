"""A right hemisphere for any model that turns a chat into text."""

import json
import re
from abc import abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

from ..errors import RightHemisphereError
from ..schemas import (
    Claim,
    ClaimCheck,
    ClaimList,
    Decision,
    Evaluation,
    Plan,
    Proposal,
    Reintegration,
    StepRecord,
    WorldState,
)
from ..memory import Memo, Remembered
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
        return await self._text(self._free_text(prompt, state))

    async def stream(self, prompt: str, state: WorldState | None = None) -> AsyncIterator[str]:
        thinking = _ReasoningFilter()
        async for piece in self.complete_stream(self._free_text(prompt, state)):
            shown = thinking.feed(piece)
            if shown:
                yield shown
        rest = thinking.flush()
        if rest:
            yield rest

    async def _text(self, messages: list[ChatMessage]) -> str:
        """`complete` for text the person will read: the model's thinking left out."""
        text = strip_reasoning(await self.complete(messages))
        if not text:
            raise RightHemisphereError("the LLM's reply was all thinking and no answer")
        return text

    async def complete_stream(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        """`complete`, in pieces as the model writes. Override where the API can stream.

        The default yields the whole of `complete` as one piece.
        """
        yield await self.complete(messages)

    def _free_text(self, prompt: str, state: WorldState | None) -> list[ChatMessage]:
        user = prompt if state is None else f"{prompts.render_state(state)}\n\n{prompt}"
        return [{"role": "system", "content": prompts.ROLE}, {"role": "user", "content": user}]

    async def claims(self, answer: str, state: WorldState) -> list[Claim]:
        listed = await self._structured(ClaimList, prompts.claims_prompt(state, answer))
        return listed.claims[: prompts.MAX_CLAIMS]

    async def remember(self, answer: str, state: WorldState) -> list[Memo]:
        kept = await self._structured(Remembered, prompts.remember_prompt(state, answer))
        return kept.notes[: prompts.MAX_NEW_NOTES]

    async def revise(
        self, answer: str, checks: list[ClaimCheck], state: WorldState, *, preferences: Sequence[str] = ()
    ) -> str:
        return await self._text(
            [
                {"role": "system", "content": prompts.ROLE},
                {"role": "user", "content": prompts.revise_prompt(state, answer, checks, preferences)},
            ]
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


REASONING_TAGS = ("think", "thinking", "reasoning")
"""Tags that local reasoning models (Qwen3, DeepSeek-R1, …) wrap their thinking in, when the
server leaves it in the reply instead of a separate field."""

_REASONING = re.compile(rf"<({'|'.join(REASONING_TAGS)})>.*?</\1>", re.DOTALL | re.IGNORECASE)
_UNCLOSED = re.compile(rf"<(?:{'|'.join(REASONING_TAGS)})>.*", re.DOTALL | re.IGNORECASE)
_ORPHAN_CLOSE = re.compile(rf"^.*</(?:{'|'.join(REASONING_TAGS)})>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """The reply without the model's thinking.

    Removes closed blocks, a block that was never closed (the reply ran out inside it),
    and everything before a closing tag with no opening one, which some chat templates
    produce by starting the reply already inside the block.
    """
    text = _REASONING.sub("", text)
    text = _ORPHAN_CLOSE.sub("", text)
    return _UNCLOSED.sub("", text).strip()


def _extract_json(text: str) -> Any:
    """Parse the JSON object in a reply, tolerating thinking, code fences or prose around it.

    Returns the first object that decodes, so braces in prose before it do no harm.
    """
    text = strip_reasoning(text)
    decoder = json.JSONDecoder()
    start = text.find("{")
    error: json.JSONDecodeError | None = None
    while start != -1:
        try:
            found, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError as exc:
            error = error or exc
        else:
            if isinstance(found, dict):
                return found
        start = text.find("{", start + 1)
    if error is not None:
        raise ValueError(f"the reply contains no valid JSON object: {error}")
    raise ValueError("the reply contains no JSON object")


class _ReasoningFilter:
    """Drops thinking blocks from a stream whose tags may be split across pieces."""

    _opening = tuple(f"<{tag}>" for tag in REASONING_TAGS)
    _closing = tuple(f"</{tag}>" for tag in REASONING_TAGS)

    def __init__(self) -> None:
        self._held = ""
        self._inside = False

    def feed(self, piece: str) -> str:
        """The part of `piece` that can be shown now. Raises on a closing tag nothing opened."""
        text, shown = self._held + piece, []
        while text:
            tags = self._closing if self._inside else self._opening
            lowered = text.lower()
            at, tag = min(((lowered.find(t), t) for t in tags if t in lowered), default=(-1, ""))
            if not self._inside and at == -1 and any(t in lowered for t in self._closing):
                # The template started inside a block: what was shown was thinking.
                raise RightHemisphereError("the model's thinking reached the answer stream")
            if at == -1:
                keep = _partial_tag_length(lowered, (*self._opening, *self._closing))
                if not self._inside:
                    shown.append(text[: len(text) - keep])
                self._held = text[len(text) - keep :]
                return "".join(shown)
            if not self._inside:
                shown.append(text[:at])
            text = text[at + len(tag) :]
            self._inside = not self._inside
        self._held = ""
        return "".join(shown)


    def flush(self) -> str:
        """What was held back in case it began a tag, once the stream has ended."""
        held, self._held = self._held, ""
        return "" if self._inside else held


def _partial_tag_length(text: str, tags: Sequence[str]) -> int:
    """How many characters at the end of `text` could be the start of one of `tags`."""
    longest = max(len(tag) for tag in tags)
    for size in range(min(longest - 1, len(text)), 0, -1):
        if any(tag.startswith(text[-size:]) for tag in tags):
            return size
    return 0
