import httpx
import pytest

OUR_VARIABLES = (
    "RIGHT_PROVIDER", "RIGHT_MODEL", "RIGHT_BASE_URL", "RIGHT_API_KEY",
    "RIGHT_TEMPERATURE", "RIGHT_JSON_MODE", "RIGHT_TIMEOUT_S",
    "LEFT_PROVIDER", "LEFT_MODEL", "LEFT_BASE_URL", "LEFT_API_KEY", "LEFT_TIMEOUT_S", "LEFT_REASONING_EFFORT",
    "JEV_API_KEY", "JEV_BASE_URL", "JEV_MODEL", "JEV_TIMEOUT_S",
    "TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL",
    "HARNESS_COMMIT_THRESHOLD", "HARNESS_PERMIT_YES_THRESHOLD", "HARNESS_PERMIT_NO_THRESHOLD",
    "HARNESS_MAX_STEPS", "HARNESS_POLICY",
    "OPENAI_API_KEY", "NVIDIA_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
    "RIGHT_EFFORT", "HARNESS_CHECK_ANSWERS", "HARNESS_MCP_CONFIG", "HARNESS_LOOKUP", "HARNESS_LOOKUP_URL",
    "HARNESS_LOOKUP_LANGUAGE", "BRAVE_API_KEY", "HARNESS_MEMORY", "HARNESS_MEMORY_FILE",
    "HARNESS_NOTIFY_URL", "HARNESS_NOTIFY_ON", "HARNESS_SCREEN_TOOL_OUTPUT", "RIGHT_PRICE_INPUT", "RIGHT_PRICE_OUTPUT",
    "HARNESS_LOCAL_ONLY", "HARNESS_WEB_SEARCH", "HARNESS_FETCH_URL", "OLLAMA_HOST",
)  # fmt: skip


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """An empty working directory and none of our variables, whatever the developer's shell has set.

    Returns monkeypatch, so a test can set exactly the variables it means to.
    """
    monkeypatch.chdir(tmp_path)
    for name in OUR_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture(autouse=True)
def no_rate_limit_waits(monkeypatch):
    """Rate-limit backoff is seconds long in real use; tests that hit a 429 need not wait."""
    monkeypatch.setattr("hemisphere_harness._http.RATE_LIMIT_BACKOFF_S", 0.0)


def nothing_running(request):
    raise httpx.ConnectError("connection refused", request=request)


@pytest.fixture(autouse=True)
def no_local_servers(monkeypatch):
    """Nothing runs on this machine, as far as the auto providers and doctor can tell.

    Tests that need a local server set hemisphere_harness.local.TRANSPORT themselves.
    """
    monkeypatch.setattr("hemisphere_harness.local.TRANSPORT", httpx.MockTransport(nothing_running))
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
