"""Left hemisphere backed by a System One decision model.

The protocol is the one TypeSafe AI defined for Jev. Other servers implement it too:
Ollama serves it locally at the same path for decision models such as autotrust-jev,
nimble and tev1 (https://docs.ollama.com/api/systemone), so this one client covers them
all. Only the base URL, the model name and whether a key is needed differ.

Wire format, per https://docs.typesafe.ai/api:

    POST {base_url}/v1/systemone          Authorization: Bearer <key>
    {"model": "jev-latest",
     "state": <string | object | array>,
     "questions": {"<id>": {"type": "noul" | "choice" | "score",
                            "instructions": "...",
                            "criteria": ...}}}
    ->
    {"model": "jev-1.13.0",
     "answers": {"<id>": {"type": "noul", "noul": 0.97}
                       | {"type": "choice", "choice": "...", "confidence": 0.8,
                          "probabilities": {"<option>": 0.9, ...}}
                       | {"type": "score", "score": 1.4, "confidence": 0.6,
                          "legend": {"0": "..."}, "probabilities": {"0": 0.1, ...}}},
     "usage": {"input_tokens": 0, "output_tokens": 0}}

`criteria` is an {option: description} map for a choice, a lowest-first list of level
descriptions for a score, and an optional {"true": ..., "false": ...} map for a noul.
All questions in a request are answered independently against the same state.

This module is the only place that knows any of the above. Image assets and box
questions are not supported.
"""

from collections.abc import Mapping
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

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
ENDPOINT = "/v1/systemone"


class JevLeftHemisphere(LeftHemisphere):
    """Talks to POST {base_url}/v1/systemone.

    `api_key` may be None for a server that needs none, such as a local Ollama.
    `base_url` is the server's root. A trailing /v1 is accepted and ignored, so the same
    value works here and for an OpenAI-compatible client pointed at the same server.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        name: str = "jev",
        timeout_s: float = 10.0,
        max_retries: int = 2,
        backoff_s: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.name = name
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout_s,
            transport=transport,
        )

    async def _decide(self, state: LeftState, questions: Mapping[str, Question]) -> LeftResult:
        payload = {
            "model": self.model,
            "state": state,
            "questions": {qid: _to_wire(question) for qid, question in questions.items()},
        }
        body = await post_json(
            self._client,
            ENDPOINT,
            payload,
            service=f"the decision model ({self.name})",
            error=LeftHemisphereError,
            max_retries=self._max_retries,
            backoff_s=self._backoff_s,
        )

        raw_answers = body.get("answers")
        if not isinstance(raw_answers, dict):
            raise LeftHemisphereError("the decision model's response has no 'answers' object")
        answers: dict[str, Answer] = {}
        for qid in questions:
            try:
                answers[qid] = _from_wire(raw_answers.get(qid))
            except ValueError as exc:  # includes pydantic's ValidationError
                raise LeftHemisphereError(f"malformed answer for '{qid}': {exc}") from exc

        model = body.get("model")
        return LeftResult(answers=answers, model=model if isinstance(model, str) else self.model)

    async def aclose(self) -> None:
        await self._client.aclose()


def _to_wire(question: Question) -> dict[str, Any]:
    wire: dict[str, Any] = {"type": question.type, "instructions": question.instructions}
    if isinstance(question, ChoiceQuestion):
        wire["criteria"] = dict(question.options)
    elif isinstance(question, ScoreQuestion):
        wire["criteria"] = list(question.levels)
    elif isinstance(question, NoulQuestion):
        criteria = {"true": question.yes_when, "false": question.no_when}
        criteria = {key: value for key, value in criteria.items() if value}
        if criteria:
            wire["criteria"] = criteria
    return wire


def _from_wire(raw: Any) -> Answer:
    match raw:
        case {"type": "noul", "noul": probability}:
            return NoulAnswer(probability=probability)
        case {"type": "choice", "choice": choice, "confidence": confidence, "probabilities": probabilities}:
            return ChoiceAnswer(choice=choice, confidence=confidence, probabilities=probabilities)
        case {"type": "score", "score": score, "confidence": confidence, "probabilities": probabilities}:
            return ScoreAnswer(
                score=score,
                confidence=confidence,
                probabilities=probabilities,
                legend=raw.get("legend") or {},
            )
        case None:
            raise ValueError("the answer is missing")
        case _:
            raise ValueError(f"unrecognised answer shape: {str(raw)[:200]}")
