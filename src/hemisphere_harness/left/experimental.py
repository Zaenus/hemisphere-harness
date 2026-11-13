"""EXPERIMENTAL: a chat model standing in for a decision model.

This exists for people with no decision model to hand. Where you can, use a real one:
TypeSafe's hosted Jev, or a local decision model through Ollama (LEFT_PROVIDER=ollama).

How it works
    A decision model scores options directly. A chat model only generates, so this
    provider asks it to reply with a single label (a letter, a digit, or Yes/No), stops
    it after one token, and reads the probability the model gave each label. Nothing
    the model writes is used. That needs a server that returns token log-probabilities.

What is known to be wrong with it
    The numbers are a chat model's token probabilities. They are not calibrated, and
    the harness's thresholds assume calibration.

    Chat models favour some labels whatever they stand for. In testing, one model put
    100% on whichever of two options was labelled "B". Choices are therefore asked once
    per rotation of the options and averaged, which turns that kind of answer into the
    50/50 it deserves. Rotation removes position bias and nothing else.

    Yes/no and score questions have no such correction. A model asked something
    unknowable tends to answer a confident "No" where 0.5 would be honest.

    A reasoning model has to have thinking switched off, or its first token is the
    start of its reasoning. `reasoning_effort` is sent for that; not every server
    accepts it.
"""

import asyncio
import json
import math
import string
import warnings
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from .._http import post_json
from ..errors import LeftHemisphereError
from ..schemas import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    LeftResult,
    NoulAnswer,
    NoulQuestion,
    Question,
    ScoreAnswer,
    ScoreQuestion,
)
from .base import LeftHemisphere, LeftState

DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama, the only server this was tested with
LETTERS = string.ascii_uppercase
TOP_LOGPROBS = 20

CHOICE_SYSTEM = (
    "You answer a multiple-choice question about a situation. "
    "Reply with exactly one character: the letter of the best option. No other text."
)
NOUL_SYSTEM = (
    "You answer a yes/no question about a situation. "
    "Reply with exactly one word: Yes or No. No other text."
)
SCORE_SYSTEM = (
    "You rate a situation against a scale. "
    "Reply with exactly one digit: the number of the level that fits best. No other text."
)


class ExperimentalWarning(UserWarning):
    """Issued whenever an experimental provider is constructed."""


class ExperimentalLLMLeft(LeftHemisphere):
    """EXPERIMENTAL. Reads a chat model's token probabilities as if it were a decision model.

    Talks to POST {base_url}/chat/completions with `logprobs` switched on. See the
    module docstring for how it works and what is known to be wrong with it.
    """

    name = "llm-experimental"

    def __init__(
        self,
        model: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        reasoning_effort: str | None = "none",
        max_rotations: int = 4,
        min_label_mass: float = 0.5,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        backoff_s: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        warnings.warn(
            f"The left hemisphere is '{model}' through the experimental chat-model provider. "
            "Its probabilities are not calibrated, so the commit threshold does not mean "
            "what it means for a decision model. See docs/providers.md.",
            ExperimentalWarning,
            stacklevel=2,
        )
        self.model = model
        self.base_url = base_url
        self._reasoning_effort = reasoning_effort
        self._max_rotations = max_rotations
        self._min_label_mass = min_label_mass
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout_s,
            transport=transport,
        )

    async def _decide(self, state: LeftState, questions: Mapping[str, Question]) -> LeftResult:
        situation = state if isinstance(state, str) else json.dumps(state, indent=2, default=str)
        answers = await asyncio.gather(
            *(self._answer(situation, question) for question in questions.values())
        )
        return LeftResult(
            answers=dict(zip(questions, answers)), model=f"{self.model}, experimental"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _answer(self, situation: str, question: Question) -> Answer:
        if isinstance(question, ChoiceQuestion):
            return await self._choice(situation, question)
        if isinstance(question, ScoreQuestion):
            return await self._score(situation, question)
        return await self._noul(situation, question)

    async def _choice(self, situation: str, question: ChoiceQuestion) -> ChoiceAnswer:
        keys = list(question.options)
        n = len(keys)
        if n == 1:
            return ChoiceAnswer.from_probabilities({keys[0]: 1.0})
        if n > len(LETTERS):
            raise LeftHemisphereError(
                f"the experimental LLM left labels options A to Z, so it cannot take {n} of them"
            )

        async def ask(shift: int) -> dict[str, float]:
            order = keys[shift:] + keys[:shift]
            labels = LETTERS[:n]
            listing = "\n".join(
                f"{label}: {_option_text(key, question.options[key])}"
                for label, key in zip(labels, order)
            )
            by_label = await self._label_probabilities(
                CHOICE_SYSTEM, _prompt(situation, question.instructions, "OPTIONS", listing), labels
            )
            return {key: by_label[label] for label, key in zip(labels, order)}

        # One round per rotation, so that each option takes a turn under each letter and
        # the model's preference for particular letters averages out.
        rounds = min(n, self._max_rotations)
        shifts = [index * n // rounds for index in range(rounds)]
        results = await asyncio.gather(*(ask(shift) for shift in shifts))
        averaged = {key: sum(result[key] for result in results) / rounds for key in keys}
        return ChoiceAnswer.from_probabilities(averaged)

    async def _noul(self, situation: str, question: NoulQuestion) -> NoulAnswer:
        guidance = [
            f"Answer {label} when: {text}"
            for label, text in (("Yes", question.yes_when), ("No", question.no_when))
            if text
        ]
        by_label = await self._label_probabilities(
            NOUL_SYSTEM,
            _prompt(situation, question.instructions, "GUIDANCE", "\n".join(guidance)),
            ("Yes", "No"),
        )
        return NoulAnswer(probability=by_label["Yes"])

    async def _score(self, situation: str, question: ScoreQuestion) -> ScoreAnswer:
        labels = [str(index) for index in range(len(question.levels))]
        listing = "\n".join(f"{label}: {level}" for label, level in zip(labels, question.levels))
        by_label = await self._label_probabilities(
            SCORE_SYSTEM, _prompt(situation, question.instructions, "LEVELS", listing), labels
        )
        return ScoreAnswer.from_probabilities([by_label[label] for label in labels], question.levels)

    async def _label_probabilities(
        self, system: str, user: str, labels: Sequence[str]
    ) -> dict[str, float]:
        """The model's probability for each label as its first token, renormalised over the labels."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 1,
            "temperature": 0,
            "logprobs": True,
            "top_logprobs": TOP_LOGPROBS,
        }
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        body = await post_json(
            self._client,
            "/chat/completions",
            payload,
            service=f"the experimental LLM left ({self.model})",
            error=LeftHemisphereError,
            max_retries=self._max_retries,
            backoff_s=self._backoff_s,
        )

        try:
            candidates = body["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
            wanted = {label.casefold(): label for label in labels}
            mass = dict.fromkeys(labels, 0.0)
            for candidate in candidates:
                label = wanted.get(_normalise(candidate["token"]))
                if label is not None:
                    mass[label] += math.exp(candidate["logprob"])
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LeftHemisphereError(
                "the server returned no token log-probabilities. The experimental LLM left "
                "needs a server and a model that support `logprobs`"
            ) from exc

        total = sum(mass.values())
        if total < self._min_label_mass:
            raise LeftHemisphereError(
                f"only {total:.0%} of the probability fell on the expected labels "
                f"({', '.join(labels)}), so the model was not answering directly. A reasoning "
                "model must have thinking switched off (LEFT_REASONING_EFFORT), or use another model"
            )
        return {label: value / total for label, value in mass.items()}


def _prompt(situation: str, instructions: str, heading: str, body: str) -> str:
    # The situation comes first so that every question about it shares a prompt prefix,
    # which local servers cache.
    parts = [f"SITUATION\n{situation}", f"QUESTION\n{instructions}"]
    if body:
        parts.append(f"{heading}\n{body}")
    parts.append("Answer:")
    return "\n\n".join(parts)


def _option_text(key: str, description: str | None) -> str:
    return f"{key} - {description}" if description else key


def _normalise(token: str) -> str:
    """'B', ' b', 'B.' and '"B"' are all the label B."""
    return token.strip().strip(".:;,()[]'\"*`").casefold()
