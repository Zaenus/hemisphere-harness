import pytest

OUR_VARIABLES = (
    "RIGHT_PROVIDER", "RIGHT_MODEL", "RIGHT_BASE_URL", "RIGHT_API_KEY",
    "RIGHT_TEMPERATURE", "RIGHT_JSON_MODE", "RIGHT_TIMEOUT_S",
    "LEFT_PROVIDER", "LEFT_MODEL", "LEFT_BASE_URL", "LEFT_API_KEY", "LEFT_TIMEOUT_S", "LEFT_REASONING_EFFORT",
    "JEV_API_KEY", "JEV_BASE_URL", "JEV_MODEL", "JEV_TIMEOUT_S",
    "TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL",
    "HARNESS_COMMIT_THRESHOLD", "HARNESS_PERMIT_YES_THRESHOLD", "HARNESS_PERMIT_NO_THRESHOLD",
    "HARNESS_MAX_STEPS", "HARNESS_POLICY",
    "OPENAI_API_KEY", "NVIDIA_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY",
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
