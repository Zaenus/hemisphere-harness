"""Prompts for an LLM-backed right hemisphere.

Each structured task shows the model a filled-in instance of the Pydantic model it must
return. The instances are built from the real schemas, so a prompt cannot drift from the
type it asks for.
"""

import json
from collections.abc import Sequence

from pydantic import BaseModel

from ..memory import Memo, Remembered
from ..schemas import (
    Claim,
    ClaimCheck,
    ClaimList,
    Decision,
    Evaluation,
    Option,
    Plan,
    Proposal,
    Reintegration,
    Stakes,
    StepRecord,
    Verdict,
    WorldState,
)

ROLE = """\
You are the right hemisphere of a two-part agent: the Master.

You hold the whole picture: the goal, its context, what has happened so far, and what is \
still implicit or uncertain. You are the only part of the agent that generates plans, \
options and language.

You are paired with a left hemisphere, the Emissary: a fast decision model that chooses \
among options you define and reports calibrated probabilities. It cannot generate anything. \
It sees only a narrow slice of the situation, and it has no sense of what lies outside the \
options it is handed. Treat its verdicts as evidence to weigh in context. They are never \
the last word.

The person you work for talks to one agent, not to its parts. Whenever you write for them, \
speak as that one agent, in the first person, and keep the division of labour out of it."""

JSON_ONLY = "Reply with a single JSON object and nothing else: no prose around it, no code fences."

REPAIR = (
    "That reply could not be used: {problem}\n\n"
    "Reply again with a single valid JSON object in the shape requested, and nothing else."
)

_PLAN_SHAPE = Plan(
    situation="What is really going on here, in two or three sentences",
    steps=["A high-level step", "Another high-level step"],
)

_PROPOSAL_SHAPE = Proposal(
    options=[
        Option(
            id="short_snake_case_id",
            description="One concrete action, understandable on its own",
            rationale="Why this could be the right move",
        ),
        Option(
            id="another_id",
            description="A genuinely different action",
            rationale="Why this could be the right move instead",
        ),
    ],
    rationale="How you read the situation, and why these are the live options",
    stakes=Stakes.LOW,
    open_ended=False,
)

_EVALUATION_SHAPE = Evaluation(
    verdict=Verdict.COMMIT,
    option_id="one_of_the_option_ids",
    reasoning="What the context adds to the left's verdict, and why that settles it",
)

_REINTEGRATION_SHAPE = Reintegration(
    situation="The whole picture as it now stands, in two or three sentences",
    goal_met=False,
)


def plan_prompt(state: WorldState) -> str:
    return _task(
        render_state(state),
        task="Read the situation and sketch a plan.",
        fields=[
            "situation: your reading of what is really going on, including what the goal implies "
            "but does not say. Two or three sentences.",
            "steps: a short high-level plan, five steps at most. Stay at the level of intent. "
            "The concrete next action is chosen one step at a time, later.",
        ],
        shape=_PLAN_SHAPE,
    )


def propose_prompt(state: WorldState) -> str:
    return _task(
        render_state(state),
        task="Propose the candidate next actions. The left hemisphere will choose among them.",
        fields=[
            "options: two to four candidate actions. Make them concrete, mutually exclusive, and "
            "each understandable without the others: the left sees only each id and description. "
            "Do not repeat an action that the history shows is already done.",
            "rationale: how you read the situation, and why these are the live options.",
            'stakes: "high" if a wrong choice would be costly, hard to undo, or would commit '
            'someone else (money, deletion, a sensitive message, legal or safety exposure). '
            'Otherwise "low".',
            "open_ended: true if this situation is novel, or you are not confident these options "
            "cover what could reasonably be done. false if it is a familiar kind of decision.",
        ],
        shape=_PROPOSAL_SHAPE,
        note="If stakes are high or the decision is open-ended, the left's choice will come "
        "back to you before anything is done." + (_TOOLS_NOTE if state.tools else ""),
    )


_TOOLS_NOTE = (
    "\n\nTOOLS: an option may call exactly one of the tools listed under TOOLS. Set its "
    "\"tool\" to the tool's name and \"arguments\" to an object that fits the tool's "
    "arguments. Then the description must say in plain words what the call will do, because "
    "the left judges the option from its description. An option that is not a tool call, "
    "such as answering the person or asking them something, leaves \"tool\" null. Only "
    "use tools that are listed, and never invent their results: you will see what a tool "
    "returned in the history."
)


def evaluate_prompt(state: WorldState, proposal: Proposal, decision: Decision) -> str:
    if decision.left_choice is None:
        verdict = "The left hemisphere gave no verdict."
    else:
        verdict = (
            f"The left hemisphere picked '{decision.left_choice}' "
            f"with probability {decision.probability:.2f}."
        )
    returned = "\n".join(
        [
            "DECISION RETURNED TO YOU",
            f"Your rationale for these options: {proposal.rationale}",
            _render_options(proposal, decision),
            verdict,
            "It came back to you because: " + "; ".join(decision.reasons),
        ]
    )
    return _task(
        render_state(state),
        returned,
        task="This decision is yours now. Settle it in context, before anything is done.",
        fields=[
            'verdict: "commit" to act on one of the options (it need not be the left\'s pick); '
            '"revise" if none of them is right and you want to propose again; '
            '"ask_human" if a person should make this call.',
            "option_id: the id of the option to act on. Required for commit, omitted otherwise.",
            "reasoning: what you can see that the left could not, and why that settles it.",
        ],
        shape=_EVALUATION_SHAPE,
    )


def reintegrate_prompt(state: WorldState, step: StepRecord) -> str:
    decision = step.decision
    lines = ["THE STEP JUST FINISHED", _render_options(step.proposal, decision)]
    if step.action is not None:
        result = "succeeded" if step.action.ok else "FAILED"
        if decision.committed_by == "human":
            lines.append(f"A human signed off '{decision.option_id}', and it was carried out.")
        else:
            lines.append(f"The {decision.committed_by} hemisphere committed to '{decision.option_id}'.")
        lines.append(f"The action {result}: {step.action.shown()}")
    else:
        lines.append(f"Nothing was done. Outcome: {decision.outcome}.")
    if step.evaluation is not None:
        lines.append(f"Your evaluation at the time: {step.evaluation.reasoning}")
    if decision.reasons:
        lines.append("Notes from the harness: " + "; ".join(decision.reasons))
    return _task(
        render_state(state),
        "\n".join(lines),
        task="Fold this step back into the whole picture.",
        fields=[
            "situation: rewrite your reading of the situation as it now stands. Describe what is "
            "now true, what changed, and what is still open. Write the current picture, not a "
            "log entry.",
            "goal_met: true only if the goal has actually been achieved.",
        ],
        shape=_REINTEGRATION_SHAPE,
    )


MAX_CLAIMS = 8

_CLAIMS_SHAPE = ClaimList(
    claims=[
        Claim(text="A single factual statement from the reply, understandable on its own", query="three to eight search keywords"),
        Claim(text="Another one", query="keywords for that one"),
    ]
)


def claims_prompt(state: WorldState, answer: str) -> str:
    return _task(
        _request(state),
        f"YOUR DRAFT REPLY\n{answer}",
        task=(
            "List the factual claims in your draft that could be wrong, so that they can be "
            "checked independently before the person sees the reply."
        ),
        fields=[
            f"claims: at most {MAX_CLAIMS}, most important first. Each one a single statement of fact: "
            "a number, name, date, definition, or cause and effect. Phrase each so it can be judged "
            "without the rest of the reply: name what it is about instead of writing 'it' or 'this'.",
            "Leave out opinions, recommendations, hedged or obviously true statements, and anything "
            "about what you did during this task. If nothing qualifies, return an empty list.",
            "query: for each claim, three to eight keywords a search engine or Wikipedia would need "
            "to find evidence for or against it. Name the subject; leave out the numbers being checked.",
        ],
        shape=_CLAIMS_SHAPE,
    )


def revise_prompt(state: WorldState, answer: str, checks: list[ClaimCheck], preferences: Sequence[str] = ()) -> str:
    form = (
        "Keep the same voice: one agent, first person, answering the person directly. Change "
        "the form wherever their preferences above require it."
        if preferences
        else "Keep the same voice and format: one agent, first person, answering the person directly."
    )
    preferred = (
        ["HOW THIS PERSON ASKED TO BE ANSWERED\nThe draft clearly does not follow these, which "
         "they told you before. Follow them; they matter as much as the facts.\n"
         + "\n".join(f"- {p}" for p in preferences)]
        if preferences
        else []
    )  # fmt: skip
    lines = []
    for check in checks:
        lines.append(f"- [{check.verdict} {check.probability:.2f}] {check.claim.text}")
        for source in check.sources:
            lines.append(f"  evidence from {source.title} ({source.url}): {source.snippet}")
    return "\n\n".join(
        [
            _request(state),
            f"YOUR DRAFT REPLY\n{answer}",
            *preferred,
            "INDEPENDENT CHECK OF ITS CLAIMS\n"
            "Each claim was judged on its own, with only the request, its context and what the "
            "tools returned, and given a probability that it holds.\n" + ("\n".join(lines) or "(no claims to check)"),
            "TASK\nRewrite the reply in the light of this check. Correct a doubtful claim if you "
            "know the right fact; otherwise remove it, or say plainly that it is uncertain. Qualify "
            "an unsure claim. Keep claims that hold. The check is evidence, not the last word: if "
            "you are certain a doubted claim is right, you may keep it, stated carefully.\n"
            "Do not add specifics that were not in the draft: no new numbers, names, dates or "
            "sources. Nothing will check them. Prefer a vaguer true statement to a precise one you "
            "are unsure of. The one exception is evidence listed above: you may use what it says, "
            "and name where it comes from.\n"
            + form
            + " Do not mention the check, the claims, or any probabilities. Reply with the "
            "revised reply only.",
        ]
    )


MAX_NEW_NOTES = 3


def remember_prompt(state: WorldState, answer: str) -> str:
    return _task(
        _request(state),
        f"YOUR REPLY\n{answer}",
        *([f"ALREADY REMEMBERED\n" + "\n".join(f"- {note}" for note in state.memory)] if state.memory else []),
        task=(
            "Decide whether anything from this exchange is worth remembering in later, separate "
            "conversations with this person. Usually nothing is."
        ),
        fields=[
            f"notes: at most {MAX_NEW_NOTES}, each a short, self-contained sentence. Keep only what will still "
            "be true and useful later: the person's stated preferences, lasting facts about them or their "
            "work, and decisions they made. Not the details of this one request, not anything you said "
            "or did, nothing already remembered, and nothing sensitive (health, finances, passwords, "
            "other people's private details). Return an empty list if nothing qualifies.",
            'kind: "preference" for how they like to be answered or worked with, which applies to every '
            'future request; "fact" for anything else, which matters only when a request touches it.',
        ],
        shape=Remembered(notes=[
            Memo(text="The person prefers short answers with a bullet-point summary.", kind="preference"),
            Memo(text="The person's team deploys on Fridays only.", kind="fact"),
        ]),  # fmt: skip
    )


def _request(state: WorldState) -> str:
    sections = []
    if state.conversation:
        sections.append(f"CONVERSATION SO FAR (oldest first)\n{render_conversation(state)}")
    sections.append(f"REQUEST\n{state.goal}")
    if observed := state.observations():
        sections.append("WHAT TOOLS RETURNED\n" + "\n".join(f"- {line}" for line in observed))
    if state.context:
        sections.append(f"CONTEXT\n{state.context}")
    return "\n\n".join(sections)


MAX_TURNS = 8
"""Earlier exchanges shown to the right. Older ones drop out of its view."""
MAX_ANSWER_CHARS = 2_000


def render_conversation(state: WorldState) -> str:
    """Earlier exchanges, oldest first, with long answers shortened."""
    lines = []
    shown = state.conversation[-MAX_TURNS:]
    if len(state.conversation) > len(shown):
        lines.append(f"({len(state.conversation) - len(shown)} earlier exchange(s) not shown)")
    for turn in shown:
        answer = turn.answer
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[:MAX_ANSWER_CHARS].rstrip() + " […]"
        lines.append(f"Person: {turn.request}\nYou: {answer}")
    return "\n\n".join(lines)


def render_state(state: WorldState) -> str:
    """The right hemisphere's view: everything. Compare WorldState.focus(), which the left gets."""
    sections = []
    if state.conversation:
        sections.append(("CONVERSATION SO FAR (oldest first)", render_conversation(state)))
    sections.append(("GOAL (the person's latest message)" if state.conversation else "GOAL", state.goal))
    if state.context:
        sections.append(("CONTEXT", state.context))
    if state.policy:
        sections.append((
            "OPERATOR POLICY (binds you as well)",
            f"{state.policy}\nAn action this policy reserves for a person needs their sign-off: "
            "ask for it instead of committing it. The harness enforces this either way.",
        ))  # fmt: skip
    if state.situation:
        sections.append(("SITUATION (your current reading)", state.situation))
    if state.plan:
        sections.append(("PLAN", "\n".join(f"{i}. {step}" for i, step in enumerate(state.plan, 1))))
    if state.memory:
        sections.append(("WHAT YOU REMEMBER FROM EARLIER CONVERSATIONS", "\n".join(f"- {note}" for note in state.memory)))
    if state.tools:
        sections.append(("TOOLS", _render_tools(state)))
    if state.history:
        sections.append(("HISTORY", "\n".join(f"- {step.summary()}" for step in state.history)))
    return "\n\n".join(f"{title}\n{body}" for title, body in sections)


def _render_tools(state: WorldState) -> str:
    lines = []
    for tool in state.tools:
        schema = json.dumps(_compact(tool.input_schema), separators=(",", ":"))
        approval = "needs the person's sign-off" if tool.needs_approval else "may run without sign-off"
        lines.append(f"- {tool.name}: {tool.description.strip() or '(no description)'}\n  arguments: {schema}\n  ({approval})")
    return "\n".join(lines)


def _compact(schema: object) -> object:
    if isinstance(schema, dict):
        return {key: _compact(value) for key, value in schema.items() if key != "title"}
    return schema


def _render_options(proposal: Proposal, decision: Decision) -> str:
    lines = ["Options:"]
    for option in proposal.options:
        line = f"- {option.id}: {option.as_action()}"
        notes = []
        if option.id in decision.probabilities:
            notes.append(f"left p={decision.probabilities[option.id]:.2f}")
        if option.id in decision.permits:
            notes.append(f"allowed without sign-off p={decision.permits[option.id]:.2f}")
        lines.append(f"{line}  [{', '.join(notes)}]" if notes else line)
    return "\n".join(lines)


def _task(*blocks: str, task: str, fields: list[str], shape: BaseModel, note: str = "") -> str:
    parts = [
        *blocks,
        f"TASK\n{task}",
        "FIELDS\n" + "\n".join(f"- {field}" for field in fields),
        "SHAPE (values are placeholders)\n" + shape.model_dump_json(indent=2),
    ]
    if note:
        parts.append(note)
    return "\n\n".join(parts)
