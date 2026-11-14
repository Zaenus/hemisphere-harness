"""The Jev client against a mock transport: wire format, parsing, retries, failures."""

import json

import httpx
import pytest

from hemisphere_harness import (
    ChoiceAnswer,
    ChoiceQuestion,
    JevLeftHemisphere,
    LeftHemisphereError,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
)

QUESTIONS = {
    "route": ChoiceQuestion(
        instructions="Which team?", options={"billing": "Charges and refunds", "technical": None}
    ),
    "urgent": NoulQuestion(instructions="Is this urgent?", yes_when="Service is down"),
    "severity": ScoreQuestion(instructions="How severe?", levels=["Cosmetic", "Degraded", "Outage"]),
}

# The shapes below follow the examples in https://docs.typesafe.ai/api.
ANSWERS = {
    "route": {
        "type": "choice",
        "choice": "technical",
        "confidence": 0.8,
        "probabilities": {"billing": 0.1, "technical": 0.9},
    },
    "urgent": {"type": "noul", "noul": 0.97},
    "severity": {
        "type": "score",
        "score": 1.43,
        "confidence": 0.36,
        "legend": {"0": "Cosmetic", "1": "Degraded", "2": "Outage"},
        "probabilities": {"0": 0.0, "1": 0.57, "2": 0.43},
    },
}


def jev(handler, **kwargs) -> JevLeftHemisphere:
    return JevLeftHemisphere("test-key", transport=httpx.MockTransport(handler), backoff_s=0, **kwargs)


def ok(answers=ANSWERS) -> httpx.Response:
    return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers, "usage": {}})


async def test_request_matches_the_jev_wire_format():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok()

    await jev(handler, model="jev-latest").decide({"ticket": "The API returns 500s"}, QUESTIONS)

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "model": "jev-latest",
        "state": {"ticket": "The API returns 500s"},
        "questions": {
            "route": {
                "type": "choice",
                "instructions": "Which team?",
                "criteria": {"billing": "Charges and refunds", "technical": None},
            },
            "urgent": {
                "type": "noul",
                "instructions": "Is this urgent?",
                "criteria": {"true": "Service is down"},
            },
            "severity": {
                "type": "score",
                "instructions": "How severe?",
                "criteria": ["Cosmetic", "Degraded", "Outage"],
            },
        },
    }


async def test_noul_without_criteria_omits_the_field():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return ok({"q": {"type": "noul", "noul": 0.5}})

    await jev(handler).decide("state", {"q": NoulQuestion(instructions="Yes?")})

    assert seen[0]["questions"]["q"] == {"type": "noul", "instructions": "Yes?"}


async def test_answers_are_parsed_into_typed_models():
    result = await jev(lambda request: ok()).decide("state", QUESTIONS)

    route, urgent, severity = (result.answers[k] for k in ("route", "urgent", "severity"))
    assert isinstance(route, ChoiceAnswer)
    assert (route.choice, route.probability, route.confidence) == ("technical", 0.9, 0.8)
    assert isinstance(urgent, NoulAnswer) and urgent.probability == 0.97
    assert isinstance(severity, ScoreAnswer)
    assert severity.probabilities == {0: 0.0, 1: 0.57, 2: 0.43}  # wire keys are strings
    assert severity.legend[2] == "Outage"
    assert result.model == "jev-1.13.0"
    assert result.latency_ms is not None


async def test_base_url_can_point_at_a_gateway():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return ok()

    await jev(handler, base_url="https://gateway.example/proxy/typesafe").decide("state", QUESTIONS)

    assert seen == ["https://gateway.example/proxy/typesafe/v1/systemone"]


async def test_a_server_that_needs_no_key_gets_no_authorization_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok()

    local = JevLeftHemisphere(None, base_url="http://localhost:11434", model="tev1:0.8b", name="ollama", transport=httpx.MockTransport(handler))
    result = await local.decide("state", QUESTIONS)

    assert str(seen[0].url) == "http://localhost:11434/v1/systemone"
    assert "authorization" not in seen[0].headers
    assert json.loads(seen[0].content)["model"] == "tev1:0.8b"
    assert local.name == "ollama" and "route" in result.answers


@pytest.mark.parametrize("base_url", ["http://localhost:11434", "http://localhost:11434/", "http://localhost:11434/v1", "http://localhost:11434/v1/"])
async def test_base_url_may_be_given_with_or_without_v1(base_url):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return ok()

    await jev(handler, base_url=base_url).decide("state", QUESTIONS)

    assert seen == ["http://localhost:11434/v1/systemone"]


async def test_ollama_style_error_body_is_surfaced():
    not_a_decision_model = httpx.Response(400, json={"error": 'model "gemma4:latest" is not a llama.cpp decision model'})

    with pytest.raises(LeftHemisphereError, match="HTTP 400.*is not a llama.cpp decision model"):
        await jev(lambda request: not_a_decision_model).decide("state", QUESTIONS)


@pytest.mark.parametrize("status", [429, 500, 529])
async def test_transient_failures_are_retried(status):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, json={"detail": "busy"}) if attempts < 3 else ok()

    result = await jev(handler, max_retries=2).decide("state", QUESTIONS)

    assert attempts == 3
    assert "route" in result.answers


async def test_gives_up_after_the_retry_budget():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(LeftHemisphereError, match="unavailable after 3 attempts"):
        await jev(handler, max_retries=2).decide("state", QUESTIONS)
    assert attempts == 3


async def test_rejected_requests_are_not_retried():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"detail": "Invalid API key"})

    with pytest.raises(LeftHemisphereError, match="check the API key.*Invalid API key"):
        await jev(handler).decide("state", QUESTIONS)
    assert attempts == 1


async def test_validation_error_detail_is_surfaced():
    detail = [{"loc": ["body", "state"], "msg": "Field required", "type": "missing"}]

    with pytest.raises(LeftHemisphereError, match="HTTP 422.*Field required"):
        await jev(lambda request: httpx.Response(422, json={"detail": detail})).decide("state", QUESTIONS)


@pytest.mark.parametrize(
    ("answers", "problem"),
    [
        ({k: v for k, v in ANSWERS.items() if k != "urgent"}, "the answer is missing"),
        ({**ANSWERS, "urgent": {"type": "noul"}}, "unrecognised answer shape"),
        ({**ANSWERS, "urgent": {"type": "noul", "noul": 1.7}}, "less than or equal to 1"),
        ({**ANSWERS, "urgent": ANSWERS["route"]}, "is a noul but the answer is a choice"),
        (
            {**ANSWERS, "route": {**ANSWERS["route"], "choice": "sales", "probabilities": {"sales": 1.0}}},
            "not among the options",
        ),
    ],
    ids=["missing", "wrong-shape", "out-of-range", "wrong-type", "choice-not-offered"],
)
async def test_malformed_answers_raise(answers, problem):
    with pytest.raises(LeftHemisphereError, match=problem):
        await jev(lambda request: ok(answers)).decide("state", QUESTIONS)


async def test_non_json_body_raises():
    with pytest.raises(LeftHemisphereError, match="not JSON"):
        await jev(lambda request: httpx.Response(200, text="<html>gateway</html>")).decide("state", QUESTIONS)


async def test_a_rate_limit_backs_off_for_seconds(monkeypatch):
    import hemisphere_harness._http as http

    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(http, "RATE_LIMIT_BACKOFF_S", 5.0)
    monkeypatch.setattr(http.asyncio, "sleep", sleep)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"detail": "slow down"}) if attempts < 3 else ok()

    await jev(handler, max_retries=2).decide("state", QUESTIONS)
    assert 5.0 <= waits[0] <= 6.25 and 10.0 <= waits[1] <= 12.5
