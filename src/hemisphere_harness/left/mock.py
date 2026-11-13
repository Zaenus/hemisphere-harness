"""An offline left hemisphere for development and tests."""

import json
import math
import re
from collections import deque
from collections.abc import Iterable, Mapping

from ..schemas import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    LeftResult,
    NoulAnswer,
    Question,
    ScoreAnswer,
    ScoreQuestion,
)
from .base import LeftHemisphere, LeftState


class MockLeftHemisphere(LeftHemisphere):
    """Answers typed questions without a network or a model.

    Two sources of answers, in this order:

    1. `script`: one {question id: answer} mapping per call to `decide`, consumed in order.
       Use it to pin exactly what the left says, in tests or a canned demo.
    2. A lexical heuristic for anything the script does not cover. A choice goes to the
       option whose text overlaps most with the state; a noul is 0.5 ("cannot tell"); a
       score is spread evenly across its levels.

    The heuristic is a stand-in so the loop can run. Its probabilities are not calibrated
    and say nothing about how a real decision model would answer.
    """

    name = "mock"

    def __init__(self, script: Iterable[Mapping[str, Answer]] = (), *, sharpness: float = 6.0) -> None:
        self._script = deque(script)
        self._sharpness = sharpness
        self.calls: list[tuple[LeftState, dict[str, Question]]] = []

    async def _decide(self, state: LeftState, questions: Mapping[str, Question]) -> LeftResult:
        self.calls.append((state, dict(questions)))
        scripted = self._script.popleft() if self._script else {}
        state_words = _words(state if isinstance(state, str) else json.dumps(state, default=str))
        answers = {
            qid: scripted[qid] if qid in scripted else self._guess(question, state_words)
            for qid, question in questions.items()
        }
        return LeftResult(answers=answers, model="mock")

    def _guess(self, question: Question, state_words: set[str]) -> Answer:
        if isinstance(question, ChoiceQuestion):
            weights = {
                key: math.exp(self._sharpness * _overlap(f"{key} {text or ''}", state_words))
                for key, text in question.options.items()
            }
            total = sum(weights.values())
            return ChoiceAnswer.from_probabilities({key: w / total for key, w in weights.items()})
        if isinstance(question, ScoreQuestion):
            n = len(question.levels)
            return ScoreAnswer.from_probabilities([1 / n] * n, question.levels)
        return NoulAnswer(probability=0.5)


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 3}


def _overlap(option_text: str, state_words: set[str]) -> float:
    """Fraction of the option's content words that also appear in the state."""
    words = _words(option_text)
    return len(words & state_words) / len(words) if words else 0.0
