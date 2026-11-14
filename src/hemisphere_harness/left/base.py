"""The Emissary's contract."""

import time
from .. import usage
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from ..errors import LeftHemisphereError
from ..schemas import Answer, ChoiceAnswer, ChoiceQuestion, LeftResult, Question, ScoreAnswer, ScoreQuestion

LeftState = str | Mapping[str, Any]


class LeftHemisphere(ABC):
    """Fast, calibrated answers to typed questions over an option space someone else defined.

    The interface is the constraint. There is no method here that generates text, proposes
    an option, or touches the world state: a left hemisphere can only answer what it was
    asked, in the types it was asked in. Whether an answer becomes an action is decided
    elsewhere, by the harness and the right hemisphere.

    Implementations override `_decide`. `decide` wraps it so that every implementation,
    real or mock, is held to the questions it was given.
    """

    name: str = "left"

    async def decide(self, state: LeftState, questions: Mapping[str, Question]) -> LeftResult:
        """Answer every question against `state`, in one pass.

        Raises LeftHemisphereError on any failure, including an answer that strays outside
        the question (a missing answer, the wrong type, a choice that was never offered).
        """
        started = time.perf_counter()
        try:
            result = await self._decide(state, questions)
        finally:
            usage.record("left", seconds=time.perf_counter() - started)
        validate_answers(questions, result.answers)
        if result.latency_ms is None:
            result.latency_ms = (time.perf_counter() - started) * 1000
        return result

    @abstractmethod
    async def _decide(self, state: LeftState, questions: Mapping[str, Question]) -> LeftResult:
        """Produce one answer per question id. Raise LeftHemisphereError on failure."""

    async def aclose(self) -> None:
        """Release any network resources."""


def validate_answers(questions: Mapping[str, Question], answers: Mapping[str, Answer]) -> None:
    """Check that each answer stays inside the question it answers."""
    for qid, question in questions.items():
        answer = answers.get(qid)
        if answer is None:
            raise LeftHemisphereError(f"no answer for question '{qid}'")
        if answer.type != question.type:
            raise LeftHemisphereError(
                f"question '{qid}' is a {question.type} but the answer is a {answer.type}"
            )
        if isinstance(question, ChoiceQuestion) and isinstance(answer, ChoiceAnswer):
            if answer.choice not in question.options:
                raise LeftHemisphereError(
                    f"answer to '{qid}' picked '{answer.choice}', which was not among the options"
                )
        if isinstance(question, ScoreQuestion) and isinstance(answer, ScoreAnswer):
            if not 0 <= answer.score <= len(question.levels) - 1:
                raise LeftHemisphereError(
                    f"answer to '{qid}' scored {answer.score}, outside the {len(question.levels)} levels"
                )
