"""Provider selection: presets, API keys, custom registration, and plugins."""

import json
import re
from importlib.metadata import EntryPoint

import httpx
import pytest

import hemisphere_harness.providers as providers
from hemisphere_harness import (
    ChatMessage,
    ChoiceAnswer,
    ConfigError,
    ExperimentalLLMLeft,
    ExperimentalWarning,
    Harness,
    JevLeftHemisphere,
    LeftHemisphere,
    LeftHemisphereError,
    LeftResult,
    LLMRightHemisphere,
    MockLeftHemisphere,
    NoulQuestion,
    OpenAICompatibleRight,
    Registry,
    RunStatus,
    Settings,
    build_left,
    build_right,
    left_providers,
    register_left,
    register_openai_compatible,
    register_right,
    right_providers,
)
from hemisphere_harness.cli import list_providers


@pytest.fixture
def wire(monkeypatch):
    """Stand in for the network: record every request a real transport would send."""
    sent: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return sent


def use(env, **variables: str) -> Settings:
    for name, value in variables.items():
        env.setenv(name, value)
    return Settings()


# --- Built-in right-hemisphere providers -------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "key_variable", "endpoint"),
    [
        ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions"),
        ("nvidia", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1/chat/completions"),
        ("xai", "XAI_API_KEY", "https://api.x.ai/v1/chat/completions"),
        ("gemini", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
        ("grok", "XAI_API_KEY", "https://api.x.ai/v1/chat/completions"),
        ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions"),
        ("NVIDIA", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1/chat/completions"),
    ],
)
async def test_hosted_provider_sends_its_own_key_to_its_own_endpoint(clean_env, wire, provider, key_variable, endpoint):
    settings = use(clean_env, RIGHT_PROVIDER=provider, RIGHT_MODEL="some/model", **{key_variable: "its-key"})

    right = build_right(settings)
    await right.generate("hello")

    assert str(wire[0].url) == endpoint
    assert wire[0].headers["authorization"] == "Bearer its-key"
    assert json.loads(wire[0].content)["model"] == "some/model"
    assert right.name == provider.lower().replace("grok", "xai")


@pytest.mark.parametrize(
    ("provider", "endpoint"),
    [
        ("ollama", "http://localhost:11434/v1/chat/completions"),
        ("lmstudio", "http://localhost:1234/v1/chat/completions"),
        ("vllm", "http://localhost:8000/v1/chat/completions"),
        ("llamacpp", "http://localhost:8080/v1/chat/completions"),
        ("llama.cpp", "http://localhost:8080/v1/chat/completions"),
    ],
)
async def test_local_provider_needs_no_key(clean_env, wire, provider, endpoint):
    right = build_right(use(clean_env, RIGHT_PROVIDER=provider, RIGHT_MODEL="llama"))
    await right.generate("hello")

    assert str(wire[0].url) == endpoint
    assert "authorization" not in wire[0].headers


def test_openai_is_the_default_provider(clean_env):
    right = build_right(use(clean_env, RIGHT_MODEL="m", OPENAI_API_KEY="k"))

    assert isinstance(right, OpenAICompatibleRight)
    assert (right.name, right.base_url) == ("openai", "https://api.openai.com/v1")


@pytest.mark.parametrize(("provider", "key_variable"), [("openai", "OPENAI_API_KEY"), ("nvidia", "NVIDIA_API_KEY"), ("grok", "XAI_API_KEY")])
def test_hosted_provider_without_a_key_fails_before_any_request(clean_env, provider, key_variable):
    with pytest.raises(ConfigError, match=f"{key_variable} or RIGHT_API_KEY"):
        build_right(use(clean_env, RIGHT_PROVIDER=provider, RIGHT_MODEL="m"))


def test_every_provider_needs_a_model(clean_env):
    with pytest.raises(ConfigError, match="RIGHT_MODEL"):
        build_right(use(clean_env, RIGHT_PROVIDER="ollama"))


async def test_provider_key_is_never_sent_to_a_different_endpoint(clean_env, wire):
    settings = use(clean_env, RIGHT_MODEL="m", OPENAI_API_KEY="sk-real-openai-key", RIGHT_BASE_URL="https://proxy.example/v1")

    await build_right(settings).generate("hello")

    assert str(wire[0].url) == "https://proxy.example/v1/chat/completions"
    assert "authorization" not in wire[0].headers


async def test_right_api_key_overrides_the_provider_key_and_follows_the_endpoint(clean_env, wire):
    settings = use(
        clean_env, RIGHT_PROVIDER="nvidia", RIGHT_MODEL="m", NVIDIA_API_KEY="hosted-key",
        RIGHT_BASE_URL="http://localhost:8000/v1", RIGHT_API_KEY="self-hosted-key",
    )  # fmt: skip

    await build_right(settings).generate("hello")

    assert str(wire[0].url) == "http://localhost:8000/v1/chat/completions"
    assert wire[0].headers["authorization"] == "Bearer self-hosted-key"


async def test_self_hosted_endpoint_of_a_hosted_provider_needs_no_key(clean_env, wire):
    settings = use(clean_env, RIGHT_PROVIDER="nvidia", RIGHT_MODEL="m", RIGHT_BASE_URL="http://localhost:8000/v1")

    await build_right(settings).generate("hello")

    assert "authorization" not in wire[0].headers


async def test_provider_key_can_live_in_the_env_file(clean_env, wire, tmp_path):
    (tmp_path / ".env").write_text("RIGHT_PROVIDER=xai\nRIGHT_MODEL=m\nXAI_API_KEY=key-from-file\n")

    await build_right(Settings()).generate("hello")

    assert wire[0].headers["authorization"] == "Bearer key-from-file"


async def test_generic_provider_takes_its_endpoint_from_the_environment(clean_env, wire):
    with pytest.raises(ConfigError, match="RIGHT_BASE_URL"):
        build_right(use(clean_env, RIGHT_PROVIDER="openai-compatible", RIGHT_MODEL="m"))

    await build_right(use(clean_env, RIGHT_BASE_URL="https://llm.internal/v1")).generate("hello")

    assert str(wire[0].url) == "https://llm.internal/v1/chat/completions"


def test_unknown_provider_names_the_known_ones(clean_env):
    with pytest.raises(ConfigError, match="RIGHT_PROVIDER=nvidea is not a known.*nvidia.*docs/providers.md"):
        build_right(use(clean_env, RIGHT_PROVIDER="nvidea", RIGHT_MODEL="m"))


# --- Adding a right-hemisphere provider --------------------------------------------------


async def test_an_openai_compatible_provider_is_one_registration(clean_env, wire):
    register_openai_compatible("acme-test", "https://llm.acme.example/v1", api_key_env="ACME_API_KEY", replace=True)

    right = build_right(use(clean_env, RIGHT_PROVIDER="acme-test", RIGHT_MODEL="acme-large", ACME_API_KEY="acme-key"))
    await right.generate("hello")

    assert str(wire[0].url) == "https://llm.acme.example/v1/chat/completions"
    assert wire[0].headers["authorization"] == "Bearer acme-key"
    assert "acme-test" in right_providers.names()


class CannedLLM(LLMRightHemisphere):
    """A provider with its own API needs one method. Here the 'API' is a lookup table."""

    name = "canned"

    def __init__(self, model: str) -> None:
        self.model = model
        self.chats: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage], *, json_reply: bool = False) -> str:
        self.chats.append(messages)
        if not json_reply:
            return "All done."
        shape = messages[-1]["content"].split("SHAPE")[-1]
        if '"claims"' in shape:
            return json.dumps({"claims": []})  # nothing worth checking
        if '"goal_met"' in shape:
            return json.dumps({"situation": "Finished.", "goal_met": True})
        if '"open_ended"' in shape:
            options = [{"id": "a", "description": "do a"}, {"id": "b", "description": "do b"}]
            return json.dumps({"options": options, "rationale": "r", "stakes": "low", "open_ended": False})
        return json.dumps({"situation": "Starting.", "steps": ["one"]})


async def test_a_provider_that_only_implements_complete_drives_the_whole_loop(clean_env):
    register_right("canned-test", lambda settings: CannedLLM(settings.right_model or "default"), replace=True)
    right = build_right(use(clean_env, RIGHT_PROVIDER="canned-test", RIGHT_MODEL="canned-1"))
    left = MockLeftHemisphere([{"select": ChoiceAnswer.from_probabilities({"a": 0.95, "b": 0.05})}])

    result = await Harness(right, left).run("goal")

    assert isinstance(right, CannedLLM) and right.model == "canned-1"
    assert result.status is RunStatus.DONE
    assert result.report == "All done."
    assert len(right.chats) == 5  # plan, propose, reintegrate, answer, and the answer's claims


# --- The registry ------------------------------------------------------------------------


def fresh() -> Registry[str]:
    registry: Registry[str] = Registry("right", "RIGHT_PROVIDER", "test.providers")
    registry.register("builtin", lambda settings: "built-in", summary="ships with the package", aliases=("b",))
    return registry


def plugin_factory(settings: Settings) -> str:
    return "from a plugin"


def install_plugins(monkeypatch, *plugins: EntryPoint) -> None:
    monkeypatch.setattr(providers, "entry_points", lambda *, group: [p for p in plugins if p.group == group])


def test_names_are_case_insensitive_and_aliases_resolve(clean_env):
    registry = fresh()

    assert registry.build(" Builtin ", Settings()) == "built-in"
    assert registry.get("B").name == "builtin"


def test_registering_a_taken_name_needs_replace(clean_env):
    registry = fresh()

    with pytest.raises(ConfigError, match="'builtin' already exists"):
        registry.register("builtin", lambda settings: "impostor")
    with pytest.raises(ConfigError, match="'b' already exists"):
        registry.register("b", lambda settings: "impostor")

    registry.register("builtin", lambda settings: "replacement", replace=True)
    assert registry.build("builtin", Settings()) == "replacement"


def test_plugin_is_listed_without_being_imported_and_loaded_when_asked_for(clean_env, monkeypatch):
    registry = fresh()
    broken = EntryPoint(name="broken", value="no_such_package.module:build", group="test.providers")
    good = EntryPoint(name="Extra", value=f"{__name__}:plugin_factory", group="test.providers")
    elsewhere = EntryPoint(name="other-group", value=f"{__name__}:plugin_factory", group="some.other.group")
    install_plugins(monkeypatch, broken, good, elsewhere)

    assert registry.names() == ["broken", "builtin", "extra"]  # listing imports nothing, so nothing fails
    assert ("extra", f"plugin: {__name__}:plugin_factory") in registry.describe()
    assert registry.build("extra", Settings()) == "from a plugin"
    with pytest.raises(ConfigError, match="plugin 'broken' .* failed to load"):
        registry.get("broken")


def test_plugin_cannot_take_over_a_registered_name_or_alias(clean_env, monkeypatch):
    registry = fresh()
    install_plugins(
        monkeypatch,
        EntryPoint(name="builtin", value=f"{__name__}:plugin_factory", group="test.providers"),
        EntryPoint(name="b", value=f"{__name__}:plugin_factory", group="test.providers"),
    )

    assert registry.names() == ["builtin"]
    assert registry.build("builtin", Settings()) == "built-in"
    assert registry.build("b", Settings()) == "built-in"


def test_the_real_registries_look_in_their_documented_entry_point_groups():
    assert right_providers.entry_point_group == "hemisphere_harness.right"
    assert left_providers.entry_point_group == "hemisphere_harness.left"


# --- Left-hemisphere providers -----------------------------------------------------------


def test_left_defaults_to_the_mock_and_switches_to_jev_when_a_key_appears(clean_env):
    assert isinstance(build_left(Settings()), MockLeftHemisphere)

    left = build_left(use(clean_env, JEV_API_KEY="k"))

    assert isinstance(left, JevLeftHemisphere)
    assert left.model == "jev-latest"


def test_typesafe_variable_names_are_accepted(clean_env):
    left = build_left(use(clean_env, TYPESAFE_API_KEY="k", TYPESAFE_DEFAULT_MODEL="jev-preview"))

    assert isinstance(left, JevLeftHemisphere) and left.model == "jev-preview"


def test_mock_can_be_forced_and_jev_without_a_key_is_an_error(clean_env):
    assert isinstance(build_left(use(clean_env, JEV_API_KEY="k", LEFT_PROVIDER="mock")), MockLeftHemisphere)

    clean_env.delenv("JEV_API_KEY")
    with pytest.raises(ConfigError, match="JEV_API_KEY"):
        build_left(use(clean_env, LEFT_PROVIDER="jev"))


def test_a_left_provider_can_be_registered_and_selected(clean_env):
    class FirstOption(LeftHemisphere):
        name = "first-option"

        async def _decide(self, state, questions):
            raise NotImplementedError

    register_left("first-option-test", lambda settings: FirstOption(), replace=True)

    assert isinstance(build_left(use(clean_env, LEFT_PROVIDER="first-option-test")), FirstOption)


def test_unknown_left_provider_names_the_known_ones(clean_env):
    with pytest.raises(ConfigError, match="LEFT_PROVIDER=jevv is not a known left-hemisphere provider.*jev"):
        build_left(use(clean_env, LEFT_PROVIDER="jevv"))


@pytest.fixture
def decision_wire(monkeypatch):
    """Stand in for a System One server: record requests, answer every question."""
    sent: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        sent.append(request)
        questions = json.loads(request.content)["questions"]
        answers = {qid: {"type": "noul", "noul": 0.9} for qid in questions}
        return httpx.Response(200, json={"model": "tev1:0.8b", "answers": answers, "usage": {}})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return sent


GATE = {"gate": NoulQuestion(instructions="Allowed?")}


async def test_ollama_serves_a_decision_model_locally_without_a_key(clean_env, decision_wire):
    left = build_left(use(clean_env, LEFT_PROVIDER="ollama", LEFT_MODEL="autotrust-jev"))
    await left.decide("state", GATE)

    assert isinstance(left, JevLeftHemisphere) and left.name == "ollama"
    assert str(decision_wire[0].url) == "http://localhost:11434/v1/systemone"
    assert "authorization" not in decision_wire[0].headers
    assert json.loads(decision_wire[0].content)["model"] == "autotrust-jev"


async def test_ollama_on_another_machine_and_a_base_url_ending_in_v1(clean_env, decision_wire):
    left = build_left(use(clean_env, LEFT_PROVIDER="ollama", LEFT_MODEL="nimble", LEFT_BASE_URL="http://gpu-box:11434/v1"))
    await left.decide("state", GATE)

    assert str(decision_wire[0].url) == "http://gpu-box:11434/v1/systemone"


def test_ollama_left_needs_a_model_and_says_what_kind(clean_env):
    with pytest.raises(ConfigError, match="LEFT_MODEL, a decision model such as autotrust-jev"):
        build_left(use(clean_env, LEFT_PROVIDER="ollama"))


async def test_any_system_one_server_can_be_the_left(clean_env, decision_wire):
    with pytest.raises(ConfigError, match="LEFT_BASE_URL"):
        build_left(use(clean_env, LEFT_PROVIDER="systemone", LEFT_MODEL="m"))

    left = build_left(use(clean_env, LEFT_BASE_URL="https://decisions.internal", LEFT_API_KEY="left-key"))
    await left.decide("state", GATE)

    assert str(decision_wire[0].url) == "https://decisions.internal/v1/systemone"
    assert decision_wire[0].headers["authorization"] == "Bearer left-key"


def test_hosted_jev_ignores_the_generic_left_variables(clean_env):
    left = build_left(use(clean_env, LEFT_PROVIDER="jev", JEV_API_KEY="k", LEFT_MODEL="tev1:0.8b", LEFT_BASE_URL="http://localhost:11434"))

    assert isinstance(left, JevLeftHemisphere)
    assert (left.model, left.base_url) == ("jev-latest", "https://api.typesafe.ai")


async def test_experimental_chat_left_is_opt_in_warns_and_defaults_to_ollama(clean_env, wire):
    with pytest.raises(ConfigError, match="LEFT_MODEL, a chat model"):
        build_left(use(clean_env, LEFT_PROVIDER="llm-experimental"))

    with pytest.warns(ExperimentalWarning, match="gemma4"):
        left = build_left(use(clean_env, LEFT_MODEL="gemma4"))

    assert isinstance(left, ExperimentalLLMLeft)
    assert left.base_url == "http://localhost:11434/v1"
    with pytest.raises(LeftHemisphereError, match="no token log-probabilities"):  # `wire` answers without them
        await left.decide("state", GATE)
    assert json.loads(wire[0].content)["reasoning_effort"] == "none"


async def test_experimental_chat_left_can_omit_reasoning_effort_and_use_another_server(clean_env, wire):
    settings = use(
        clean_env, LEFT_PROVIDER="llm-experimental", LEFT_MODEL="m", LEFT_REASONING_EFFORT="omit",
        LEFT_BASE_URL="http://localhost:8000/v1", LEFT_API_KEY="k",
    )  # fmt: skip
    with pytest.warns(ExperimentalWarning):
        left = build_left(settings)

    with pytest.raises(LeftHemisphereError):
        await left.decide("state", GATE)

    assert str(wire[0].url) == "http://localhost:8000/v1/chat/completions"
    assert wire[0].headers["authorization"] == "Bearer k"
    assert "reasoning_effort" not in json.loads(wire[0].content)


def test_nothing_experimental_is_chosen_automatically(clean_env):
    left = build_left(use(clean_env, LEFT_MODEL="gemma4"))  # LEFT_PROVIDER is still "auto"

    assert isinstance(left, MockLeftHemisphere)


# --- Listing -----------------------------------------------------------------------------


def test_listing_shows_every_provider_with_its_endpoint_and_key_variable(clean_env, capsys):
    list_providers()

    out = capsys.readouterr().out
    for expected in (
        "nvidia, nim", "https://integrate.api.nvidia.com/v1 · key: NVIDIA_API_KEY",
        "xai, grok", "https://api.x.ai/v1 · key: XAI_API_KEY",
        "ollama", "http://localhost:11434/v1 · no key",
        "LEFT_PROVIDER", "jev", "mock",
    ):  # fmt: skip
        assert expected in out
    assert re.search(r"^  ollama +a decision model served by Ollama", out, re.M)
    assert re.search(r"^  llm-experimental +EXPERIMENTAL, not calibrated", out, re.M)
