"""Finding local model servers, and the auto providers that prefer them."""

import json

import httpx
import pytest

import hemisphere_harness.local as local
from hemisphere_harness import ConfigError, JevLeftHemisphere, MockLeftHemisphere, OpenAICompatibleRight, Settings, build_left, build_right


def machine(*, ollama: dict[str, dict] | None = None, lmstudio: list[str] | None = None):
    """A pretend machine. `ollama` maps model names to {"decision": bool, "size": int}."""

    def handle(request: httpx.Request) -> httpx.Response:
        port, path = request.url.port, request.url.path
        if port == 11434 and ollama is not None:
            if path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": n, "size": m.get("size", 0)} for n, m in ollama.items()]})
            if path == "/api/show":
                name = json.loads(request.content)["model"]
                caps = ["completion", "decision"] if ollama[name].get("decision") else ["completion"]
                return httpx.Response(200, json={"capabilities": caps})
        if port == 1234 and lmstudio is not None and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in lmstudio]})
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handle)


def on(monkeypatch, transport: httpx.MockTransport) -> None:
    monkeypatch.setattr(local, "TRANSPORT", transport)


def use(env, **variables: str) -> Settings:
    for name, value in variables.items():
        env.setenv(name, value)
    return Settings()


# --- Discovery ---------------------------------------------------------------------------


def test_scan_reports_each_server_and_which_models_are_decision_models(monkeypatch):
    on(monkeypatch, machine(ollama={"gemma4:latest": {}, "tev1:latest": {"decision": True}}, lmstudio=["qwen3-8b"]))

    found = {f.server.provider: f for f in local.scan()}

    assert [m.id for m in found["ollama"].models] == ["tev1:latest", "gemma4:latest"]  # decision models first
    assert found["ollama"].models[0].decision
    assert found["lmstudio"].serves("qwen3-8b")
    assert found["llamacpp"].problem == "not running"
    assert not found["vllm"].up


@pytest.mark.parametrize(("wanted", "served", "same"), [
    ("gemma4", "gemma4:latest", True),
    ("Gemma4:latest", "gemma4:latest", True),
    ("gemma4:2b", "gemma4:latest", False),
    ("hf.co/org/model", "hf.co/org/model:latest", True),
])  # fmt: skip
def test_untagged_ollama_names_mean_latest(wanted, served, same):
    assert local.same_model(wanted, served) is same


@pytest.mark.parametrize(("host", "url"), [
    ("gpu-box", "http://gpu-box:11434"),
    ("gpu-box:9000", "http://gpu-box:9000"),
    ("https://ollama.lan:443/", "https://ollama.lan:443"),
])  # fmt: skip
def test_ollama_host_is_read_as_ollama_reads_it(monkeypatch, host, url):
    monkeypatch.setenv("OLLAMA_HOST", host)

    assert local.ollama_url() == url


# --- The auto right hemisphere -----------------------------------------------------------


def test_auto_right_uses_the_local_server_that_serves_the_model(clean_env, monkeypatch):
    on(monkeypatch, machine(ollama={"gemma4:latest": {}}, lmstudio=["qwen3-8b"]))

    right = build_right(use(clean_env, RIGHT_MODEL="qwen3-8b", OPENAI_API_KEY="k"))

    assert isinstance(right, OpenAICompatibleRight)
    assert (right.name, right.base_url) == ("lmstudio", "http://localhost:1234/v1")


def test_auto_right_finds_ollama_where_ollama_host_says(clean_env, monkeypatch):
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "gpu-box" and request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:latest"}]})
        raise httpx.ConnectError("refused", request=request)

    on(monkeypatch, httpx.MockTransport(handle))
    right = build_right(use(clean_env, OLLAMA_HOST="gpu-box", RIGHT_MODEL="gemma4"))

    assert (right.name, right.base_url) == ("ollama", "http://gpu-box:11434/v1")


def test_auto_right_says_what_is_running_when_nothing_serves_the_model(clean_env, monkeypatch):
    on(monkeypatch, machine(ollama={"gemma4:latest": {}}))

    with pytest.raises(ConfigError, match=r"not served by any local server \(ollama serves gemma4:latest\).*doctor"):
        build_right(use(clean_env, RIGHT_MODEL="gpt-5"))


def test_auto_right_with_a_base_url_is_that_server(clean_env):
    right = build_right(use(clean_env, RIGHT_MODEL="m", RIGHT_BASE_URL="http://box:8000/v1"))

    assert (right.name, right.base_url) == ("openai-compatible", "http://box:8000/v1")


def test_auto_is_the_default_right_provider(clean_env):
    assert Settings().right_provider == "auto"


# --- The auto left hemisphere ------------------------------------------------------------


def test_auto_left_prefers_the_largest_local_decision_model_even_with_a_jev_key(clean_env, monkeypatch):
    on(monkeypatch, machine(ollama={
        "tev1:0.8b": {"decision": True, "size": 900},
        "tev1:latest": {"decision": True, "size": 4600},
        "gemma4:latest": {"size": 9000},
    }))  # fmt: skip

    left = build_left(use(clean_env, JEV_API_KEY="k"))

    assert isinstance(left, JevLeftHemisphere)
    assert (left.name, left.model) == ("ollama", "tev1:latest")


def test_auto_left_with_left_model_uses_only_that_one(clean_env, monkeypatch):
    on(monkeypatch, machine(ollama={"tev1:0.8b": {"decision": True, "size": 900}, "tev1:latest": {"decision": True, "size": 4600}}))

    left = build_left(use(clean_env, LEFT_MODEL="tev1:0.8b"))

    assert isinstance(left, JevLeftHemisphere) and left.model == "tev1:0.8b"


def test_auto_left_never_picks_a_chat_model(clean_env, monkeypatch):
    on(monkeypatch, machine(ollama={"gemma4:latest": {}}))

    assert isinstance(build_left(use(clean_env, LEFT_MODEL="gemma4")), MockLeftHemisphere)


def test_auto_left_falls_back_to_jev_then_the_mock(clean_env):
    assert isinstance(build_left(Settings()), MockLeftHemisphere)
    left = build_left(use(clean_env, JEV_API_KEY="k"))
    assert (left.name, left.model) == ("jev", "jev-latest")
