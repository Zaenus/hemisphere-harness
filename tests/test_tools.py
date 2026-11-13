"""Tools: options that call them, the sign-off rule, Python tools and MCP servers."""

import json
import sys
from pathlib import Path

import pytest

from hemisphere_harness import (
    ChoiceAnswer,
    ConfigError,
    Harness,
    HarnessConfig,
    HumanDecision,
    MockLeftHemisphere,
    NoulAnswer,
    Option,
    Outcome,
    Plan,
    Proposal,
    Reintegration,
    RunStatus,
    ScriptedRightHemisphere,
    Stakes,
    Evaluation,
    Verdict,
)
from hemisphere_harness.mcp_tools import MCPConfig, MCPToolbox, load_mcp_config
from hemisphere_harness.right.prompts import propose_prompt, render_state
from hemisphere_harness.tools import MAX_OUTPUT_CHARS, FunctionToolbox

SERVER = str(Path(__file__).with_name("mcp_test_server.py"))


def toolbox():
    box = FunctionToolbox()
    calls = []

    @box.tool(read_only=True)
    def lookup_order(order_id: str) -> str:
        """Find an order by its id."""
        calls.append(("lookup_order", order_id))
        return f"order {order_id}: shipped"

    @box.tool()
    async def refund(order_id: str, amount: float = 0.0) -> dict:
        """Refund an order."""
        calls.append(("refund", order_id, amount))
        return {"refunded": amount}

    box.calls = calls
    return box


# --- Python tools ----------------------------------------------------------------------------


async def test_a_function_becomes_a_tool_with_a_schema_from_its_signature():
    box = toolbox()
    lookup = box.get("lookup_order")
    assert lookup.description == "Find an order by its id."
    assert lookup.input_schema["required"] == ["order_id"]
    assert (lookup.read_only, lookup.needs_approval) == (True, False)
    assert box.get("refund").needs_approval is True  # writes, so it waits for a person

    assert (await box.call("lookup_order", {"order_id": "A1"})).output == "order A1: shipped"
    assert (await box.call("refund", {"order_id": "A1", "amount": 30})).output == '{"refunded": 30.0}'


async def test_a_tools_failures_are_results_not_crashes():
    box = toolbox()
    bad = await box.call("lookup_order", {})
    assert bad.ok is False and "order_id" in bad.output
    unknown = await box.call("delete_everything", {})
    assert unknown.ok is False and "no tool named 'delete_everything'" in unknown.output

    @box.tool(read_only=True)
    def huge() -> str:
        return "x" * (MAX_OUTPUT_CHARS + 50)

    long = await box.call("huge", {})
    assert long.output.endswith("[… 50 more characters]")


def test_approval_can_be_always_or_never():
    assert FunctionToolbox(approval="always").tool(read_only=True)(lambda: 1) and True
    always = FunctionToolbox(approval="always")
    always.add(lambda: 1, name="reader", read_only=True)
    never = FunctionToolbox(approval="never")
    never.add(lambda: 1, name="writer")
    assert always.get("reader").needs_approval is True
    assert never.get("writer").needs_approval is False


# --- The harness with tools ----------------------------------------------------------------


def proposal(*options, stakes=Stakes.LOW):
    return Proposal(options=list(options), rationale="r", stakes=stakes, open_ended=False)


LOOKUP = Option(id="look", description="Look up order A1", tool="lookup_order", arguments={"order_id": "A1"})
REFUND = Option(id="refund", description="Refund order A1 in full", tool="refund", arguments={"order_id": "A1", "amount": 30})
REPLY = Option(id="reply", description="Reply to the customer")


async def test_the_left_may_commit_a_tool_that_only_reads():
    box = toolbox()
    right = ScriptedRightHemisphere(
        plan=Plan(situation="s"), proposals=[proposal(LOOKUP, REPLY)],
        reintegrations=[Reintegration(situation="found it", goal_met=True)], report="Order A1 has shipped.",
    )  # fmt: skip
    left = MockLeftHemisphere(script=[{"select": ChoiceAnswer.from_probabilities({"look": 0.95, "reply": 0.05})}])
    result = await Harness(right, left, toolbox=box).run("Where is order A1?")

    step = result.state.history[0]
    assert step.decision.committed_by == "left"
    assert (step.action.tool, step.action.ok, step.action.output) == ("lookup_order", True, "order A1: shipped")
    assert box.calls == [("lookup_order", "A1")]
    assert "'look' (called lookup_order) -> done: order A1: shipped" in step.summary()


async def test_a_tool_that_writes_waits_for_a_person_whoever_chooses_it():
    box = toolbox()
    right = ScriptedRightHemisphere(
        plan=Plan(situation="s"), proposals=[proposal(REFUND, REPLY)],
        evaluations=[Evaluation(verdict=Verdict.COMMIT, option_id="refund", reasoning="it is owed")],
        reintegrations=[Reintegration(situation="waiting", goal_met=False), Reintegration(situation="refunded", goal_met=True)],
        report="Refunded.",
    )  # fmt: skip
    left = MockLeftHemisphere(script=[{"select": ChoiceAnswer.from_probabilities({"refund": 0.97, "reply": 0.03})}])
    harness = Harness(right, left, toolbox=box)
    stopped = await harness.run("Refund order A1")

    decision = stopped.state.history[0].decision
    assert stopped.status is RunStatus.NEEDS_HUMAN
    assert decision.outcome is Outcome.FLAG_HUMAN
    assert "'refund' calls refund, which needs a person's sign-off" in decision.reasons[0]  # the left could not commit it
    assert "which calls refund: that needs a person's sign-off" in decision.reasons[-1]  # nor could the right
    assert box.calls == []

    resumed = await harness.resume(stopped.state, HumanDecision(approve=True, option_id="refund"))
    assert box.calls == [("refund", "A1", 30.0)]
    assert resumed.state.history[-1].action.tool == "refund"
    assert resumed.status is RunStatus.DONE


async def test_an_invented_tool_counts_as_needing_sign_off_and_then_fails_cleanly():
    invented = Option(id="nuke", description="Delete the account", tool="delete_account", arguments={})
    right = ScriptedRightHemisphere(
        plan=Plan(situation="s"), proposals=[proposal(invented, REPLY)],
        evaluations=[Evaluation(verdict=Verdict.COMMIT, option_id="nuke", reasoning="r")],
        reintegrations=[Reintegration(situation="waiting", goal_met=False), Reintegration(situation="it failed", goal_met=True)],
    )  # fmt: skip
    left = MockLeftHemisphere(script=[{"select": ChoiceAnswer.from_probabilities({"nuke": 0.9, "reply": 0.1})}])
    harness = Harness(right, left, toolbox=toolbox())
    stopped = await harness.run("g")
    assert stopped.status is RunStatus.NEEDS_HUMAN
    resumed = await harness.resume(stopped.state, HumanDecision(approve=True, option_id="nuke"))
    action = resumed.state.history[-1].action
    assert action.ok is False and "no tool named 'delete_account'" in action.output


async def test_without_a_toolbox_a_tool_option_fails_instead_of_pretending():
    right = ScriptedRightHemisphere(
        plan=Plan(situation="s"), proposals=[proposal(LOOKUP, REPLY)],
        evaluations=[Evaluation(verdict=Verdict.COMMIT, option_id="look", reasoning="r")],
        reintegrations=[Reintegration(situation="waiting", goal_met=False), Reintegration(situation="r", goal_met=True)],
    )  # fmt: skip
    left = MockLeftHemisphere(script=[{"select": ChoiceAnswer.from_probabilities({"look": 0.9, "reply": 0.1})}])
    harness = Harness(right, left)
    stopped = await harness.run("g")  # unknown to the harness, so it needs sign-off
    resumed = await harness.resume(stopped.state, HumanDecision(approve=True, option_id="look"))
    assert resumed.state.history[-1].action.output == "no tools are connected"


async def test_the_right_sees_the_tools_and_the_left_sees_the_exact_call_it_is_asked_to_allow():
    box = toolbox()
    right = ScriptedRightHemisphere(
        plan=Plan(situation="s"), proposals=[proposal(REFUND, REPLY)],
        evaluations=[Evaluation(verdict=Verdict.ASK_HUMAN, reasoning="r")],
        reintegrations=[Reintegration(situation="r", goal_met=False)],
    )  # fmt: skip
    left = MockLeftHemisphere(script=[{"select": ChoiceAnswer.from_probabilities({"refund": 0.6, "reply": 0.4})}])
    result = await Harness(right, left, toolbox=box, config=HarnessConfig(policy="No refunds")).run("g")

    rendered = render_state(result.state)
    assert "TOOLS\n- lookup_order: Find an order by its id." in rendered
    assert "(needs the person's sign-off)" in rendered and "(may run without sign-off)" in rendered
    assert '"title"' not in rendered  # schemas are compacted
    assert "an option may call exactly one of the tools" in propose_prompt(result.state)

    _, questions = left.calls[0]
    permit = questions["permit:refund"].instructions
    assert 'Refund order A1 in full [calls the tool refund with {"order_id": "A1", "amount": 30}]' in permit


# --- MCP servers ---------------------------------------------------------------------------


def config(**servers):
    return MCPConfig.model_validate({"mcpServers": servers})


async def test_mcp_tools_are_listed_called_and_gated_by_their_read_only_flag(tmp_path):
    box = await MCPToolbox.connect(config(shop={"command": sys.executable, "args": [SERVER]}), log_dir=tmp_path)
    try:
        assert {tool.name for tool in box.tools} == {"shop.lookup_order", "shop.save_note", "shop.broken"}
        assert box.get("shop.lookup_order").needs_approval is False
        assert box.get("shop.save_note").needs_approval is True
        assert box.get("shop.lookup_order").input_schema["required"] == ["order_id"]

        found = await box.call("shop.lookup_order", {"order_id": "A1"})
        assert (found.ok, found.output) == (True, "order A1: 2 items, shipped")
        failed = await box.call("shop.broken", {})
        assert failed.ok is False and "broken" in failed.output  # the server keeps its internals to itself
    finally:
        await box.aclose()


async def test_a_server_can_be_limited_to_some_tools_and_its_approval_changed(tmp_path):
    box = await MCPToolbox.connect(
        config(shop={"command": sys.executable, "args": [SERVER], "tools": ["save_note"], "approval": "never"})
    )
    try:
        assert [(tool.name, tool.needs_approval) for tool in box.tools] == [("shop.save_note", False)]
    finally:
        await box.aclose()


async def test_one_broken_server_does_not_stop_the_others(tmp_path):
    box = await MCPToolbox.connect(
        config(
            shop={"command": sys.executable, "args": [SERVER]},
            gone={"command": str(tmp_path / "no-such-program")},
            off={"command": "anything", "disabled": True},
        )
    )
    try:
        assert "gone" in box.errors and "off" not in box.errors
        assert box.get("shop.lookup_order") is not None
    finally:
        await box.aclose()


def test_the_config_file_is_checked(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_mcp_config(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_mcp_config(tmp_path / "bad.json")
    (tmp_path / "both.json").write_text(json.dumps({"mcpServers": {"x": {"command": "a", "url": "http://b"}}}))
    with pytest.raises(ConfigError, match="either a command"):
        load_mcp_config(tmp_path / "both.json")
    (tmp_path / "ok.json").write_text(json.dumps({"mcpServers": {"x": {"url": "http://localhost:1/mcp", "approval": "always"}}}))
    assert load_mcp_config(tmp_path / "ok.json").servers["x"].approval == "always"


async def test_what_a_tool_returned_counts_as_evidence_when_the_answer_is_checked():
    from hemisphere_harness import Claim
    from hemisphere_harness.right.prompts import revise_prompt

    right = ScriptedRightHemisphere(
        plan=Plan(situation="s"), proposals=[proposal(LOOKUP, REPLY)],
        reintegrations=[Reintegration(situation="found it", goal_met=True)],
        report="Order A1 has shipped.", claims=[Claim(text="Order A1 has shipped.")],
    )  # fmt: skip
    left = MockLeftHemisphere(script=[
        {"select": ChoiceAnswer.from_probabilities({"look": 0.95, "reply": 0.05})},
        {"claim:c1": NoulAnswer(probability=0.93)},
    ])  # fmt: skip
    result = await Harness(right, left, toolbox=toolbox()).run("Where is order A1?")

    focus, questions = left.calls[-1]
    assert focus["tool_results"] == ["lookup_order returned: order A1: shipped"]
    assert "what the tools returned" in questions["claim:c1"].instructions
    assert "WHAT TOOLS RETURNED\n- lookup_order returned: order A1: shipped" in revise_prompt(result.state, "draft", [])
