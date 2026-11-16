"""HARNESS_LOCAL_ONLY: every endpoint off this machine and its network is refused."""

import sys
from pathlib import Path

import pytest

from hemisphere_harness import ConfigError, MockLeftHemisphere, Settings, build_left, build_right
from hemisphere_harness.local import is_local_url
from hemisphere_harness.lookup import searcher_from_settings
from hemisphere_harness.mcp_tools import MCPConfig, MCPToolbox

SERVER = str(Path(__file__).parent / "mcp_test_server.py")


def use(env, **variables: str) -> Settings:
    env.setenv("HARNESS_LOCAL_ONLY", "true")
    for name, value in variables.items():
        env.setenv(name, value)
    return Settings()


@pytest.mark.parametrize("url", [
    "http://localhost:11434", "http://127.0.0.1:8080/v1", "http://[::1]:1234", "http://192.168.1.20:11434",
    "http://10.0.0.5", "http://ollama:11434", "http://gpu-box.lan:8000/v1", "http://nas.local", "localhost:8888",
])  # fmt: skip
def test_this_machine_and_its_network_are_local(url):
    assert is_local_url(url)


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1", "https://api.anthropic.com", "http://8.8.8.8", "https://en.wikipedia.org",
    "https://localhost.evil.com", "", "not a url at all",
])  # fmt: skip
def test_anything_else_is_not(url):
    assert not is_local_url(url)


def test_a_local_right_and_left_are_allowed(clean_env):
    right = build_right(use(clean_env, RIGHT_PROVIDER="ollama", RIGHT_MODEL="gemma4"))
    left = build_left(use(clean_env, LEFT_PROVIDER="systemone", LEFT_BASE_URL="http://ollama:11434", LEFT_MODEL="tev1"))

    assert (right.name, left.name) == ("ollama", "systemone")


@pytest.mark.parametrize(("variables", "refused"), [
    ({"RIGHT_PROVIDER": "openai", "RIGHT_MODEL": "m", "OPENAI_API_KEY": "k"}, "api.openai.com"),
    ({"RIGHT_PROVIDER": "anthropic", "RIGHT_MODEL": "claude-opus-5-5", "ANTHROPIC_API_KEY": "k"}, "api.anthropic.com"),
    ({"RIGHT_PROVIDER": "ollama", "RIGHT_MODEL": "m", "RIGHT_BASE_URL": "https://ollama.example.com/v1"}, "ollama.example.com"),
    # auto falls back to OpenAI when nothing local serves the model: refused too.
    ({"RIGHT_MODEL": "gpt-5", "OPENAI_API_KEY": "k"}, "api.openai.com"),
])  # fmt: skip
def test_a_hosted_right_is_refused_and_says_where_it_would_send_data(clean_env, variables, refused):
    with pytest.raises(ConfigError, match=f"the right hemisphere .* would send data to {refused}"):
        build_right(use(clean_env, **variables))


def test_hosted_jev_is_refused_and_auto_without_a_local_model_is_the_mock(clean_env):
    with pytest.raises(ConfigError, match="left hemisphere \\(jev\\) would send data to api.typesafe.ai"):
        build_left(use(clean_env, LEFT_PROVIDER="jev", JEV_API_KEY="k"))
    with pytest.raises(ConfigError, match="api.typesafe.ai"):
        build_left(use(clean_env, LEFT_PROVIDER="auto"))  # auto picks jev: the key is still set

    clean_env.delenv("JEV_API_KEY")
    assert isinstance(build_left(use(clean_env)), MockLeftHemisphere)


def test_a_plugin_that_does_not_say_where_it_connects_is_refused(clean_env):
    from hemisphere_harness import LLMRightHemisphere, register_right

    class Opaque(LLMRightHemisphere):
        name = "opaque"

        async def complete(self, messages, *, json_reply=False):
            return "ok"

    register_right("opaque-test", lambda settings: Opaque(), replace=True)
    with pytest.raises(ConfigError, match="cannot be told where the right hemisphere \\(opaque\\) sends data"):
        build_right(use(clean_env, RIGHT_PROVIDER="opaque-test"))


@pytest.mark.parametrize(("variables", "refused"), [
    ({"HARNESS_LOOKUP": "wikipedia"}, "en.wikipedia.org"),
    ({"HARNESS_LOOKUP": "brave", "BRAVE_API_KEY": "k"}, "api.search.brave.com"),
    ({"HARNESS_LOOKUP": "searxng", "HARNESS_LOOKUP_URL": "https://searx.example.org"}, "searx.example.org"),
])  # fmt: skip
def test_lookups_off_this_machine_are_refused(clean_env, variables, refused):
    with pytest.raises(ConfigError, match=f"would send data to {refused}"):
        searcher_from_settings(use(clean_env, **variables))


def test_a_local_searxng_is_allowed(clean_env):
    searcher = searcher_from_settings(use(clean_env, HARNESS_LOOKUP="searxng", HARNESS_LOOKUP_URL="http://localhost:8888"))

    assert searcher is not None and searcher.name == "searxng"


async def test_remote_mcp_servers_are_skipped_and_local_ones_still_work():
    config = MCPConfig.model_validate({"mcpServers": {
        "shop": {"command": sys.executable, "args": [SERVER]},
        "cloud": {"url": "https://mcp.example.com/mcp"},
    }})  # fmt: skip

    box = await MCPToolbox.connect(config, local_only=True)
    try:
        assert "mcp.example.com" in box.errors["cloud"]
        assert box.get("shop.lookup_order") is not None
    finally:
        await box.aclose()
