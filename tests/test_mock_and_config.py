"""The offline left hemisphere, the settings, and the runnable example."""

import pytest
from pydantic import ValidationError

from hemisphere_harness import (
    ChoiceAnswer,
    ChoiceQuestion,
    HarnessConfig,
    MockLeftHemisphere,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
    Settings,
)
from hemisphere_harness.examples import simple_loop

ROUTE = ChoiceQuestion(
    instructions="Which action?",
    options={
        "issue_refund": "Refund the duplicate charge on the invoice",
        "reset_password": "Send a password reset link",
    },
)


# --- Mock left ---------------------------------------------------------------------------


async def test_heuristic_prefers_the_option_that_matches_the_state():
    result = await MockLeftHemisphere().decide(
        {"goal": "Customer was charged twice and wants the duplicate charge refunded"}, {"route": ROUTE}
    )

    answer = result.answers["route"]
    assert isinstance(answer, ChoiceAnswer)
    assert answer.choice == "issue_refund"
    assert sum(answer.probabilities.values()) == pytest.approx(1.0)


async def test_heuristic_cannot_tell_on_yes_no_and_score_questions():
    result = await MockLeftHemisphere().decide(
        "state",
        {"gate": NoulQuestion(instructions="Allowed?"), "rate": ScoreQuestion(instructions="How bad?", levels=["a", "b", "c"])},
    )

    gate, rate = result.answers["gate"], result.answers["rate"]
    assert isinstance(gate, NoulAnswer) and gate.probability == 0.5
    assert isinstance(rate, ScoreAnswer) and rate.score == pytest.approx(1.0)


async def test_script_is_consumed_in_order_and_then_the_heuristic_takes_over():
    pinned = ChoiceAnswer.from_probabilities({"issue_refund": 0.2, "reset_password": 0.8})
    left = MockLeftHemisphere([{"route": pinned}])

    first = await left.decide("refund the charge", {"route": ROUTE})
    second = await left.decide("refund the charge", {"route": ROUTE})

    assert first.answers["route"] == pinned
    assert second.answers["route"].choice == "issue_refund"  # type: ignore[union-attr]


def test_confidence_follows_the_documented_formulas():
    # Worked examples from https://docs.typesafe.ai/confidence.md and /api.
    assert ChoiceAnswer.from_probabilities({"a": 0.6, "b": 0.3, "c": 0.1}).confidence == pytest.approx(0.4)
    assert ChoiceAnswer.from_probabilities({"a": 0.6, "b": 0.2, "c": 0.2}).confidence == pytest.approx(0.4)

    score = ScoreAnswer.from_probabilities([0.33, 0.67, 0.0])
    assert score.score == pytest.approx(0.67)
    assert score.confidence == pytest.approx(0.5, abs=0.01)


# --- Settings ----------------------------------------------------------------------------


def test_env_values_reach_the_harness_config_and_blank_values_are_ignored(clean_env, tmp_path):
    (tmp_path / ".env").write_text("HARNESS_COMMIT_THRESHOLD=0.9\nHARNESS_POLICY=No refunds\nRIGHT_TEMPERATURE=\n")

    settings = Settings()

    assert settings.harness_config().commit_threshold == 0.9
    assert settings.harness_config().policy == "No refunds"
    assert settings.right_temperature is None


def test_permit_thresholds_must_be_ordered():
    with pytest.raises(ValidationError, match="permit_no_threshold must be below"):
        HarnessConfig(permit_yes_threshold=0.3, permit_no_threshold=0.7)


def test_env_finds_undeclared_variables_in_the_shell_and_in_the_env_file(clean_env, tmp_path):
    (tmp_path / ".env").write_text("ACME_API_KEY=from-file\nACME_REGION=eu\n")
    clean_env.setenv("ACME_API_KEY", "from-shell")

    settings = Settings()

    assert settings.env("ACME_API_KEY") == "from-shell"  # the process environment wins
    assert settings.env("ACME_REGION") == "eu"
    assert settings.env("ACME_MISSING") is None
    assert Settings(_env_file=None).env("ACME_REGION") is None  # no file was read, so none is consulted


def test_secrets_do_not_appear_when_settings_are_printed(clean_env):
    clean_env.setenv("RIGHT_API_KEY", "sk-very-secret")
    clean_env.setenv("JEV_API_KEY", "jev-very-secret")

    assert "very-secret" not in repr(Settings())


# --- Example -----------------------------------------------------------------------------


async def test_offline_example_runs_both_tickets_to_their_expected_ends(clean_env, capsys):
    assert await simple_loop.main([]) == 0

    out = capsys.readouterr().out
    assert "DONE after 2 step(s)" in out
    assert "NEEDS_HUMAN after 1 step(s)" in out
    assert "PROCEED: the left commits 'unlock_account'" in out
    assert "PROCEED: the right commits 'reply_and_wait'" in out


async def test_example_with_a_custom_goal_needs_a_live_right(clean_env, capsys):
    assert await simple_loop.main(["Do", "something"]) == 2
    assert "RIGHT_MODEL" in capsys.readouterr().out
