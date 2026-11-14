"""Replaying a past run's decisions with a different left hemisphere."""

import json

from hemisphere_harness import (
    ChoiceAnswer,
    Harness,
    HarnessConfig,
    MockLeftHemisphere,
    NoulAnswer,
    Option,
    Plan,
    Proposal,
    Reintegration,
    ScriptedRightHemisphere,
    Stakes,
)
from hemisphere_harness.cli import main
from hemisphere_harness.replay import format_replay, replay


def low(*ids):
    return Proposal(options=[Option(id=i, description=f"do {i}") for i in ids], rationale="r", stakes=Stakes.LOW, open_ended=False)


async def two_step_run():
    right = ScriptedRightHemisphere(
        plan=Plan(situation="at the start"),
        proposals=[low("a", "b"), low("c", "d")],
        reintegrations=[Reintegration(situation="after step 1", goal_met=False), Reintegration(situation="after step 2", goal_met=True)],
        report="Done.",
    )
    left = MockLeftHemisphere(script=[
        {"select": ChoiceAnswer.from_probabilities({"a": 0.95, "b": 0.05}), "permit:a": NoulAnswer(probability=0.9), "permit:b": NoulAnswer(probability=0.9)},
        {"select": ChoiceAnswer.from_probabilities({"c": 0.9, "d": 0.1}), "permit:c": NoulAnswer(probability=0.9), "permit:d": NoulAnswer(probability=0.9)},
    ])  # fmt: skip
    config = HarnessConfig(policy="Nothing over $100 without sign-off.", check_answers=False)
    return await Harness(right, left, config=config).run("Do the thing."), config


async def test_each_step_is_asked_again_as_it_was_asked_then():
    result, config = await two_step_run()
    other = MockLeftHemisphere(script=[
        {"select": ChoiceAnswer.from_probabilities({"a": 0.9, "b": 0.1}), "permit:a": NoulAnswer(probability=0.9), "permit:b": NoulAnswer(probability=0.9)},
        {"select": ChoiceAnswer.from_probabilities({"c": 0.3, "d": 0.7}), "permit:c": NoulAnswer(probability=0.9), "permit:d": NoulAnswer(probability=0.1)},
    ])  # fmt: skip
    replayed = await replay(other, result.state, config=config, events=result.trace, run_id="r1", left_before="mock")

    first, second = other.calls
    assert first[0]["situation"] == "at the start" and "recent_steps" not in first[0]
    assert second[0]["situation"] == "after step 1" and len(second[0]["recent_steps"]) == 1  # history as it really was
    assert set(second[1]) == {"select", "permit:c", "permit:d"}  # the policy questions too

    s1, s2 = replayed.steps
    assert not s1.choice_changed and not s1.commit_changed
    assert s2.before.choice == "c" and s2.before.left_commits
    assert s2.after.choice == "d" and not s2.after.left_commits  # below the commit threshold, and refused by the policy
    assert replayed.differing == [s2]
    text = format_replay(replayed)
    assert "1 of 2 step(s) would have gone differently." in text and "← differs" in text


def test_the_command_replays_a_saved_run(tmp_path, capsys, clean_env):
    import asyncio

    result, config = asyncio.run(two_step_run())
    runs = tmp_path / "runs"
    runs.mkdir()
    saved = {"id": "run-1", "state": result.state.model_dump(mode="json"), "events": [e.model_dump(mode="json") for e in result.trace],
             "config": config.model_dump(mode="json")}  # fmt: skip
    (runs / "run-1.json").write_text(json.dumps(saved))

    code = main(["replay", "run-1", "--data-dir", str(tmp_path), "--left-provider", "mock", "--env-file", str(tmp_path / "none.env"), "--json", str(tmp_path / "out.json")])
    out = capsys.readouterr().out
    assert code == 0
    assert "Run run-1: Do the thing." in out and "After: mock" in out
    assert json.loads((tmp_path / "out.json").read_text())["steps"][0]["step"] == 1


def test_a_missing_run_is_reported(tmp_path, capsys, clean_env):
    assert main(["replay", "nope", "--data-dir", str(tmp_path), "--env-file", str(tmp_path / "none.env")]) == 2
    assert "Cannot replay nope" in capsys.readouterr().err
