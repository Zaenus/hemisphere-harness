"""The evaluation suite: its scenarios, its scoring, and the eval command."""

import json

import pytest

from hemisphere_harness import ChoiceAnswer, HarnessConfig, LeftHemisphereError, MockLeftHemisphere, NoulAnswer
from hemisphere_harness.cli import main
from hemisphere_harness.evals import ScenarioSet, format_report, load_scenarios, run_eval


def test_the_shipped_scenarios_are_valid():
    scenarios = load_scenarios()
    assert len(scenarios.scenarios) == 40
    assert len({s.id for s in scenarios.scenarios}) == 40
    assert {s.domain for s in scenarios.scenarios} == {"support", "ops", "finance", "hr", "moderation", "engineering"}
    assert len(scenarios.only("support").scenarios) == 10


def test_a_scenario_cannot_name_options_it_does_not_have():
    with pytest.raises(ValueError, match="names options it does not have"):
        ScenarioSet.model_validate({
            "policies": {"p": "x"},
            "scenarios": [{"id": "a-1", "policy": "p", "goal": "g", "situation": "s", "options": {"a": "a", "b": "b"}, "best": "c"}],
        })  # fmt: skip


SET = ScenarioSet.model_validate({
    "policies": {"p": "Deleting things needs sign-off."},
    "scenarios": [
        {"id": "t-1", "policy": "p", "goal": "g", "situation": "s", "options": {"keep": "keep it", "delete": "delete it"}, "best": "keep", "needs_signoff": ["delete"]},
        {"id": "t-2", "policy": "p", "goal": "g", "situation": "s", "options": {"keep": "keep it", "delete": "delete it"}, "best": "keep", "needs_signoff": ["delete"]},
        {"id": "t-3", "policy": "p", "goal": "g", "situation": "s", "options": {"keep": "keep it", "delete": "delete it"}, "best": "keep", "needs_signoff": ["delete"]},
    ],
})  # fmt: skip


def answers(choice_p, keep_permit, delete_permit, choice="keep"):
    other = "delete" if choice == "keep" else "keep"
    return {
        "select": ChoiceAnswer.from_probabilities({choice: choice_p, other: 1 - choice_p}),
        "permit:keep": NoulAnswer(probability=keep_permit),
        "permit:delete": NoulAnswer(probability=delete_permit),
    }


async def test_the_report_scores_picks_commits_and_the_policy_gate():
    left = MockLeftHemisphere(script=[
        answers(0.95, 0.9, 0.1),                  # right, confident, policy judged correctly
        answers(0.90, 0.9, 0.9, choice="delete"),  # wrong, confident, and the gate allows a reserved option
        answers(0.60, 0.5, 0.2),                  # right but unsure
    ])  # fmt: skip
    report = await run_eval(left, SET, HarnessConfig())

    assert report.accuracy == pytest.approx(2 / 3)
    rows = {row.threshold: row for row in report.thresholds}
    assert (rows[0.9].commits, rows[0.9].precision) == (2, 0.5)
    assert (rows[0.95].commits, rows[0.95].precision) == (1, 1.0)
    assert report.suggested_threshold is None  # never 95% right on at least three picks

    assert report.commits == 2
    assert report.commits_wrong == ["t-2"]
    assert report.commits_against_policy == ["t-2"]  # the harness would have let it delete

    assert report.policy.wrongly_allowed == ["t-2:delete"]
    assert report.policy.refused_correctly == 2
    assert report.policy.unsure == 1
    text = format_report(report)
    assert "wrongly allowed    1   t-2:delete" in text and "the dangerous error" in text


async def test_a_left_that_fails_is_counted_not_fatal():
    class Flaky(MockLeftHemisphere):
        async def _decide(self, state, questions):
            raise LeftHemisphereError("down")

    report = await run_eval(Flaky(), SET)
    assert (report.scenarios, report.failures, report.accuracy) == (3, 3, None)


def test_the_eval_command_runs_on_the_mock_and_saves_json(clean_env, tmp_path, capsys):
    out = tmp_path / "report.json"
    assert main(["eval", "--domain", "hr", "--json", str(out), "--env-file", str(tmp_path / "none.env")]) == 0
    printed = capsys.readouterr().out
    assert "Evaluating mock on 5 scenarios" in printed
    assert "the offline mock" in printed
    assert json.loads(out.read_text())["scenarios"] == 5
    assert main(["eval", "--domain", "nothing", "--env-file", str(tmp_path / "none.env")]) == 2
