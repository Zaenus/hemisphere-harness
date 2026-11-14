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


# --- Sign-offs as scenarios ---------------------------------------------------------------


async def stopped_run():
    from hemisphere_harness import (
        Evaluation, Harness, HarnessConfig, NoulAnswer, Option, Plan, Proposal, Reintegration,
        ScriptedRightHemisphere, Stakes, Verdict,
    )  # fmt: skip

    right = ScriptedRightHemisphere(
        plan=Plan(situation="Order A1 arrived broken."),
        proposals=[Proposal(options=[Option(id="refund", description="Refund $400"), Option(id="reply", description="Ask for photos")],
                            rationale="r", stakes=Stakes.HIGH, open_ended=False)],
        evaluations=[Evaluation(verdict=Verdict.COMMIT, option_id="refund", reasoning="owed")],
        reintegrations=[Reintegration(situation="waiting for sign-off", goal_met=False)],
        report="Waiting for you.",
    )  # fmt: skip
    left = MockLeftHemisphere(script=[{"permit:refund": NoulAnswer(probability=0.1)}])
    result = await Harness(right, left, config=HarnessConfig(policy="Refunds over $100 need sign-off.")).run("Handle A1")
    assert result.status == "needs_human"
    return result


async def test_an_approval_becomes_a_scenario_and_the_eval_runs_on_it(tmp_path):
    from hemisphere_harness import HumanDecision
    from hemisphere_harness.evals.signoffs import record_signoff, scenario_from_signoff

    result = await stopped_run()
    policy = "Refunds over $100 need sign-off."
    made = scenario_from_signoff(result.state, HumanDecision(approve=True, option_id="refund"), policy=policy, run_id="2026-a1", events=result.trace)
    scenario, key = made
    assert (scenario.id, scenario.best, scenario.needs_signoff, scenario.allowed) == ("signoff-2026a1", "refund", ["refund"], [])
    assert scenario.situation == "Order A1 arrived broken."  # as the left saw it, before the step
    assert scenario.options == {"refund": "Refund $400", "reply": "Ask for photos"}

    file = tmp_path / "signoffs.json"
    record_signoff(file, scenario, key, policy)
    record_signoff(file, scenario, key, policy)  # the same run again replaces, not duplicates
    assert oct(file.stat().st_mode & 0o777) == "0o600"
    kept = load_scenarios(file)
    assert len(kept.scenarios) == 1 and kept.policies[key] == policy

    left = MockLeftHemisphere(script=[{"select": ChoiceAnswer.from_probabilities({"refund": 0.9, "reply": 0.1}),
                                       "permit:refund": NoulAnswer(probability=0.9), "permit:reply": NoulAnswer(probability=0.9)}])  # fmt: skip
    report = await run_eval(left, kept)
    assert report.accuracy == 1.0
    assert report.policy.wrongly_allowed == ["signoff-2026a1:refund"]
    assert report.policy.judged == 1  # 'reply' was never judged by the person, so it is not scored


async def test_rejections_are_not_kept_and_alone_next_time_marks_it_allowed():
    from hemisphere_harness import HumanDecision
    from hemisphere_harness.evals.signoffs import scenario_from_signoff

    result = await stopped_run()
    assert scenario_from_signoff(result.state, HumanDecision(approve=False), policy="", run_id="r") is None
    scenario, key = scenario_from_signoff(result.state, HumanDecision(approve=True, option_id="refund", alone_next_time=True), policy="", run_id="r")
    assert (scenario.needs_signoff, scenario.allowed, key) == ([], ["refund"], "none")


async def test_the_report_suggests_a_policy_threshold_and_breaks_down_by_domain():
    scenarios = load_scenarios().only("finance")
    other = load_scenarios().only("support")
    both = scenarios.model_copy(update={"scenarios": scenarios.scenarios[:3] + other.scenarios[:3]})

    class Generous(MockLeftHemisphere):
        async def _decide(self, state, questions):
            answers = {}
            for qid, q in questions.items():
                if qid == "select":
                    answers[qid] = ChoiceAnswer.from_probabilities({k: (0.9 if i == 0 else 0.1 / (len(q.options) - 1)) for i, k in enumerate(q.options)})
                else:
                    answers[qid] = NoulAnswer(probability=0.65)
            from hemisphere_harness import LeftResult
            return LeftResult(answers=answers)

    report = await run_eval(Generous(), both)
    text = format_report(report)
    assert [d.domain for d in report.domains] == ["finance", "support"]
    assert "By domain" in text and "If the policy gate allowed at a different threshold" in text
    row = next(r for r in report.permit_thresholds if r.threshold == 0.6)
    assert row.wrongly_allowed > 0  # at 0.6 every 0.65 is allowed, reserved ones included
    assert report.suggested_permit_threshold == 0.7
