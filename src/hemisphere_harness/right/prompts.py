"""Prompts for an LLM-backed right hemisphere.

Each structured task shows the model a filled-in instance of the Pydantic model it must
return. The instances are built from the real schemas, so a prompt cannot drift from the
type it asks for.
"""

from pydantic import BaseModel

from ..schemas import (
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
the last word."""

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
        "back to you before anything is done.",
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
        lines.append(f"The {decision.committed_by} hemisphere committed to '{decision.option_id}'.")
        lines.append(f"The action {result}: {step.action.output}")
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


def render_state(state: WorldState) -> str:
    """The right hemisphere's view: everything. Compare WorldState.focus(), which the left gets."""
    sections = [("GOAL", state.goal)]
    if state.context:
        sections.append(("CONTEXT", state.context))
    if state.situation:
        sections.append(("SITUATION (your current reading)", state.situation))
    if state.plan:
        sections.append(("PLAN", "\n".join(f"{i}. {step}" for i, step in enumerate(state.plan, 1))))
    if state.history:
        sections.append(("HISTORY", "\n".join(f"- {step.summary()}" for step in state.history)))
    return "\n\n".join(f"{title}\n{body}" for title, body in sections)


def _render_options(proposal: Proposal, decision: Decision) -> str:
    lines = ["Options:"]
    for option in proposal.options:
        line = f"- {option.id}: {option.description}"
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
