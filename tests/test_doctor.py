"""`hemisphere-harness doctor`: the report, the next steps, and --setup."""

import json

import httpx
import pytest

import hemisphere_harness.local as local
from hemisphere_harness import Settings
from hemisphere_harness.doctor import gather, next_steps, print_report, setup


def machine(models: dict[str, list[str]] | None):
    """Ollama with these models and capabilities, or nothing running at all."""

    def handle(request: httpx.Request) -> httpx.Response:
        if models is None or request.url.port != 11434:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n, "size": 4_600_000_000} for n in models]})
        name = json.loads(request.content)["model"]
        return httpx.Response(200, json={"capabilities": models[name]})

    return httpx.MockTransport(handle)


OLLAMA = {"gemma4:latest": ["completion"], "tev1:latest": ["completion", "decision"], "nomic-embed:latest": ["embedding"]}


def report_text(settings: Settings, tmp_path) -> tuple[str, list[str]]:
    report = gather(settings)
    lines: list[str] = []
    print_report(report, settings, tmp_path / ".env", lines.append)
    return "\n".join(lines), next_steps(report, settings)


def test_a_fresh_machine_with_ollama_is_told_to_choose_a_right(clean_env, monkeypatch, tmp_path):
    monkeypatch.setattr(local, "TRANSPORT", machine(OLLAMA))

    text, steps = report_text(Settings(), tmp_path)

    assert "decision  tev1:latest · 4.6 GB" in text
    assert "chat      gemma4:latest" in text and "nomic-embed" not in text.split("chat")[1].splitlines()[0]
    assert "left    ✓ ollama · tev1:latest" in text
    assert "No model is chosen" in text
    assert any("--setup" in step and "gemma4:latest" in step for step in steps)


def test_nothing_running_points_at_ollama_or_a_hosted_provider(clean_env, tmp_path):
    text, steps = report_text(Settings(), tmp_path)

    assert "Ollama     http://localhost:11434     not running" in text
    assert "Nothing is sent anywhere" in text
    assert any("Install Ollama" in step for step in steps)
    assert any("left hemisphere is the offline mock" in step for step in steps)


def test_a_hosted_right_is_shown_as_leaving_the_machine(clean_env, monkeypatch, tmp_path):
    monkeypatch.setattr(local, "TRANSPORT", machine(OLLAMA))
    clean_env.setenv("RIGHT_PROVIDER", "openai")
    clean_env.setenv("RIGHT_MODEL", "gpt-5")
    clean_env.setenv("OPENAI_API_KEY", "k")
    clean_env.setenv("HARNESS_LOOKUP", "wikipedia")

    text, _ = report_text(Settings(), tmp_path)

    assert "Data leaves this machine and its network, to:" in text
    assert "the right hemisphere: https://api.openai.com/v1" in text
    assert "lookups (wikipedia): https://en.wikipedia.org" in text
    assert "the left hemisphere" not in text.split("Data leaves")[1]  # tev1 is local


def test_all_local_suggests_making_it_a_rule(clean_env, monkeypatch, tmp_path):
    monkeypatch.setattr(local, "TRANSPORT", machine(OLLAMA))
    clean_env.setenv("RIGHT_MODEL", "gemma4")

    text, steps = report_text(Settings(), tmp_path)

    assert "Everything stays on this machine or its network." in text
    assert steps == ["Set HARNESS_LOCAL_ONLY=true to keep it that way."]


def test_setup_writes_the_chosen_local_models_and_local_only(clean_env, monkeypatch, tmp_path):
    monkeypatch.setattr(local, "TRANSPORT", machine(OLLAMA))
    settings = Settings()
    answers = iter(["9", "1", "1", "y"])  # an invalid number first, then gemma4, tev1, and local-only
    shown: list[str] = []

    changes = setup(gather(settings), settings, tmp_path / ".env", ask=lambda prompt: next(answers), write=shown.append)

    assert changes == {
        "RIGHT_PROVIDER": "auto", "RIGHT_MODEL": "gemma4:latest",
        "LEFT_PROVIDER": "auto", "LEFT_MODEL": "tev1:latest", "HARNESS_LOCAL_ONLY": "true",
    }  # fmt: skip
    assert "That is not one of the numbers above." in "\n".join(shown)
    written = Settings(_env_file=tmp_path / ".env")
    assert (written.right_model, written.left_model, written.harness_local_only) == ("gemma4:latest", "tev1:latest", True)


def test_setup_leaves_everything_alone_when_nothing_is_chosen(clean_env, monkeypatch, tmp_path):
    monkeypatch.setattr(local, "TRANSPORT", machine(OLLAMA))
    settings = Settings()

    changes = setup(gather(settings), settings, tmp_path / ".env", ask=lambda prompt: "", write=lambda line: None)

    assert changes == {}
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("argv", [["doctor"], ["doctor", "--env-file", "custom.env"]])
def test_the_command_exits_non_zero_until_both_hemispheres_are_ready(clean_env, capsys, argv):
    from hemisphere_harness.cli import main

    assert main(argv) == 1
    assert "On this machine" in capsys.readouterr().out
