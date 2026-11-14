"""Memory across conversations: the store, recall by the left, remembering by the right."""

import json
import os
import stat

from hemisphere_harness import (
    ChoiceAnswer,
    Harness,
    LeftHemisphereError,
    MockLeftHemisphere,
    NoulAnswer,
    Option,
    Plan,
    Proposal,
    Reintegration,
    RightHemisphereError,
    ScriptedRightHemisphere,
    Stakes,
)
from hemisphere_harness.memory import MAX_NOTE_CHARS, MemoryStore
from hemisphere_harness.right.prompts import remember_prompt, render_state


def test_the_store_skips_duplicates_trims_and_keeps_the_file_private(tmp_path):
    store = MemoryStore(tmp_path / "m" / "memory.json")
    assert store.notes == []
    added = store.add(["Prefers short answers.", "prefers   SHORT answers.", "x" * 1000, "  "], source="a request")
    assert [n.text for n in added] == ["Prefers short answers.", "x" * MAX_NOTE_CHARS]
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.add(["Prefers short answers."]) == []

    assert store.delete(added[0].id) is True and store.delete("missing") is False
    assert [n.text for n in store.notes] == ["x" * MAX_NOTE_CHARS]
    store.clear()
    assert store.notes == []
    store.path.write_text("{not json")
    assert store.notes == []  # a damaged file is treated as empty, not as a crash


def one_step_right(cls=ScriptedRightHemisphere):
    return cls(
        plan=Plan(situation="s"),
        proposals=[Proposal(options=[Option(id="a", description="a"), Option(id="b", description="b")], rationale="r", stakes=Stakes.LOW, open_ended=False)],
        reintegrations=[Reintegration(situation="r", goal_met=True)],
        report="Here is the summary, in three bullets.",
    )


SURE = {"select": ChoiceAnswer.from_probabilities({"a": 0.95, "b": 0.05})}


class Recording(ScriptedRightHemisphere):
    async def plan(self, state):
        self.planned_with = state.model_copy(deep=True)
        return await super().plan(state)


async def test_the_left_recalls_only_relevant_notes_and_only_the_right_sees_them(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    short, birthday = store.add(["Prefers short answers with bullet points.", "Their dog's birthday is in May."])
    left = MockLeftHemisphere(script=[
        {f"recall:{short.id}": NoulAnswer(probability=0.9), f"recall:{birthday.id}": NoulAnswer(probability=0.1)},
        SURE,
    ])  # fmt: skip
    right = one_step_right(Recording)
    result = await Harness(right, left, memory=store).run("Summarise the quarterly report")

    assert right.planned_with.memory == ["Prefers short answers with bullet points."]
    assert "WHAT YOU REMEMBER FROM EARLIER CONVERSATIONS\n- Prefers short answers" in render_state(right.planned_with)
    recall_view, recall_questions = left.calls[0]
    assert recall_view == {"request": "Summarise the quarterly report"}
    assert "Would this note about the person help" in recall_questions[f"recall:{short.id}"].instructions
    decide_view, _ = left.calls[1]
    assert "memory" not in decide_view and "Prefers short answers" not in json.dumps(decide_view)
    recall = next(e for e in result.trace if e.phase == "recall")
    assert recall.summary.startswith("recalled 0 preference(s) and 1 of 2 fact(s)")


def test_the_answer_is_told_to_follow_what_is_remembered():
    from hemisphere_harness import RunStatus, WorldState
    from hemisphere_harness.harness import answer_prompt

    assert "Follow what you remember" not in answer_prompt(RunStatus.DONE, WorldState(goal="g"))
    prompt = answer_prompt(RunStatus.DONE, WorldState(goal="g", memory=["Prefers short answers."]))
    assert "Follow what you remember about this person, above all how they like to be answered" in prompt


async def test_a_preference_is_always_recalled_without_asking_the_left(tmp_path):
    from hemisphere_harness.memory import Memo

    store = MemoryStore(tmp_path / "memory.json")
    store.add([Memo(text="Prefers short answers in plain words.", kind="preference")])
    left = MockLeftHemisphere(script=[SURE])
    right = one_step_right(Recording)
    await Harness(right, left, memory=store).run("What is proof of stake?")

    assert right.planned_with.memory == ["Prefers short answers in plain words."]
    assert not any(q.startswith("recall:") for _, questions in left.calls for q in questions)  # nothing to ask


async def test_without_the_left_the_most_recent_notes_are_recalled(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    store.add([f"note {i}" for i in range(8)])

    class NoRecall(MockLeftHemisphere):
        async def _decide(self, state, questions):
            if any(q.startswith("recall:") for q in questions):
                raise LeftHemisphereError("down")
            return await super()._decide(state, questions)

    right = one_step_right(Recording)
    await Harness(right, NoRecall(script=[SURE]), memory=store).run("g")
    assert right.planned_with.memory == ["note 3", "note 4", "note 5", "note 6", "note 7"]


async def test_the_right_keeps_what_is_worth_keeping_and_a_failure_keeps_nothing(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")

    class Keeps(ScriptedRightHemisphere):
        async def remember(self, answer, state):
            return ["Works in finance and reports to the CFO."]

    result = await Harness(one_step_right(Keeps), MockLeftHemisphere(script=[SURE]), memory=store).run("Summarise the report")
    assert [n.text for n in store.notes] == ["Works in finance and reports to the CFO."]
    assert store.notes[0].source == "Summarise the report"
    assert [e.phase for e in result.trace][-2:] == ["remember", "end"]

    class Fails(ScriptedRightHemisphere):
        async def remember(self, answer, state):
            raise RightHemisphereError("malformed")

    other = MemoryStore(tmp_path / "other.json")
    result = await Harness(one_step_right(Fails), MockLeftHemisphere(script=[SURE]), memory=other).run("g")
    assert other.notes == [] and result.report  # the run still answers


async def test_an_llm_right_is_told_what_not_to_keep_and_keeps_at_most_three():
    from hemisphere_harness import LLMRightHemisphere, WorldState

    class Fake(LLMRightHemisphere):
        prompts = []

        async def complete(self, messages, *, json_reply=False):
            Fake.prompts.append(messages[-1]["content"])
            return json.dumps({"notes": [{"text": t, "kind": k} for t, k in (("a", "preference"), ("b", "fact"), ("c", "fact"), ("d", "fact"))]})

    kept = await Fake().remember("The answer.", WorldState(goal="g", memory=["Already known."]))
    assert [(m.text, m.kind) for m in kept] == [("a", "preference"), ("b", "fact"), ("c", "fact")]
    prompt = Fake.prompts[-1]
    assert "Usually nothing is" in prompt and "nothing sensitive" in prompt
    assert '"preference" for how they like to be answered' in prompt
    assert "ALREADY REMEMBERED\n- Already known." in prompt
    assert remember_prompt(WorldState(goal="g"), "x").count("ALREADY REMEMBERED") == 0


async def test_the_web_lists_and_forgets_notes(clean_env, tmp_path):
    from test_web import AUTH, client, make_app

    store = MemoryStore(tmp_path / "data" / "memory.json")  # the default: beside the run history
    first, second = store.add(["one", "two"])
    async with client(make_app(tmp_path)) as http:
        listed = (await http.get("/api/memory", headers=AUTH)).json()
        assert listed["enabled"] is False
        assert [n["text"] for n in listed["notes"]] == ["two", "one"]  # newest first
        assert (await http.delete(f"/api/memory/{first.id}", headers=AUTH)).status_code == 200
        assert (await http.delete("/api/memory/nope", headers=AUTH)).status_code == 404
        await http.delete("/api/memory", headers=AUTH)
        assert (await http.get("/api/memory", headers=AUTH)).json()["notes"] == []


# --- Checking the answer against remembered preferences ------------------------------------


async def test_an_answer_that_ignores_a_remembered_preference_is_rewritten(tmp_path):
    from hemisphere_harness.memory import Memo

    store = MemoryStore(tmp_path / "memory.json")
    store.add([Memo(text="Prefers short answers in plain words.", kind="preference")])
    left = MockLeftHemisphere(script=[SURE, {"style:0": NoulAnswer(probability=0.04)}])
    right = one_step_right()
    right._revision = "Short version."
    result = await Harness(right, left, memory=store).run("What is proof of stake?")

    assert result.report == "Short version."
    assert right.revised_for == ["Prefers short answers in plain words."]
    question = left.calls[-1][1]["style:0"]
    assert "Here is the summary, in three bullets." in question.instructions and "(7 words, 1 lines)" in question.instructions
    assert result.check.preferences[0].followed is False and result.check.revised
    style = next(e for e in result.trace if e.phase == "style")
    assert "follows 0 of 1 preference(s)" in style.summary
    revise = next(e for e in result.trace if e.phase == "revise")
    assert revise.summary == "revised the answer: 1 preference(s) not followed"


async def test_only_a_clear_miss_counts(tmp_path):
    from hemisphere_harness.memory import Memo

    store = MemoryStore(tmp_path / "memory.json")
    store.add([Memo(text="Prefers short answers.", kind="preference"), Memo(text="Uses Django.", kind="fact")])
    left = MockLeftHemisphere(script=[{"recall:" + store.notes[1].id: NoulAnswer(probability=0.9)}, SURE, {"style:0": NoulAnswer(probability=0.45)}])
    right = one_step_right()
    result = await Harness(right, left, memory=store).run("What is proof of stake?")

    assert "revise" not in right.calls
    assert result.check.preferences[0].followed
    assert set(left.calls[-1][1]) == {"style:0"}  # facts are not preferences: nothing to check them against


def test_the_revision_is_told_which_preferences_were_missed():
    from hemisphere_harness import WorldState
    from hemisphere_harness.right.prompts import revise_prompt

    plain = revise_prompt(WorldState(goal="g"), "draft", [])
    assert "HOW THIS PERSON ASKED" not in plain and "Keep the same voice and format" in plain
    prompt = revise_prompt(WorldState(goal="g"), "draft", [], ["Prefers short answers."])
    assert "HOW THIS PERSON ASKED TO BE ANSWERED" in prompt and "- Prefers short answers." in prompt
    assert "Change the form wherever their preferences above require it." in prompt
