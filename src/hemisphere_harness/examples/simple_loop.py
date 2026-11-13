"""The full cycle on two support tickets, with a trace of which hemisphere did what.

    python -m hemisphere_harness.examples.simple_loop
    python -m hemisphere_harness.examples.simple_loop "Your own goal or ticket text"

With nothing configured this runs offline: both hemispheres are scripted stand-ins, so
the trace shows the control flow, and no model is judging anything. Set RIGHT_MODEL (and
usually RIGHT_API_KEY) to put a live LLM in the right hemisphere, and JEV_API_KEY to put
Jev in the left. A goal of your own needs a live right hemisphere.

Nothing here touches a real system. Actions are simulated.
"""

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any

from hemisphere_harness import (
    Answer,
    ChoiceAnswer,
    Claim,
    Evaluation,
    Harness,
    HarnessError,
    LeftHemisphere,
    MockLeftHemisphere,
    NoulAnswer,
    Option,
    Plan,
    Proposal,
    Reintegration,
    RightHemisphere,
    RunResult,
    ScriptedRightHemisphere,
    Settings,
    Stakes,
    TracePrinter,
    Verdict,
    WorldState,
    build_left,
    build_right,
)

DEMO_POLICY = "Refunds or credits above $500 need human sign-off. Unlocking accounts and sending replies do not."


@dataclass
class Ticket:
    goal: str
    context: str = ""
    policy: str = ""  # used unless HARNESS_POLICY is set
    # The offline script: what each stand-in hemisphere will say. Unused with a live right.
    right: dict[str, Any] = field(default_factory=dict)
    left: list[dict[str, Answer]] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)


async def run_ticket(ticket: Ticket, settings: Settings) -> RunResult:
    right: RightHemisphere
    left: LeftHemisphere
    config = settings.harness_config()

    if settings.right_model:
        right, left = build_right(settings), build_left(settings)
        mode = f"right = {right.name} ({settings.right_model}) · left = {left.name}"
        # The heuristic mock answers every yes/no question 0.5, so a policy gate would send
        # every step back to the right, and every claim in the answer would look unsure. Use
        # the ticket's policy and the answer check only when something can judge them.
        if isinstance(left, MockLeftHemisphere):
            config.check_answers = False
        else:
            config.policy = config.policy or ticket.policy
    else:
        right = ScriptedRightHemisphere(**ticket.right)
        left = MockLeftHemisphere(script=ticket.left)
        mode = "offline demo: right = scripted · left = mock (scripted)"
        config.policy = config.policy or ticket.policy

    async def simulate(option: Option, state: WorldState) -> str:
        return ticket.outcomes.get(option.id, f"(simulated) {option.description}")

    print(f"\n{'═' * 100}\n{ticket.goal}\n{mode}\n")
    async with Harness(right, left, config=config, act=simulate, on_event=TracePrinter()) as harness:
        return await harness.run(ticket.goal, context=ticket.context)


async def main(argv: list[str]) -> int:
    settings = Settings()
    if argv:
        if not settings.right_model:
            print("A goal of your own needs a live right hemisphere: set RIGHT_MODEL (see .env.example).")
            return 2
        tickets = [Ticket(goal=" ".join(argv))]
    else:
        tickets = DEMO_TICKETS

    try:
        for ticket in tickets:
            await run_ticket(ticket, settings)
    except HarnessError as exc:
        print(f"\nRun failed: {exc}", file=sys.stderr)
        return 1
    return 0


# --- The two demo tickets, with their offline scripts ------------------------------------
#
# Ticket 1 is routine. Step 1 takes the fast path: the left is sure, so it commits. In
# step 2 the left cannot separate two similar replies, so the choice returns to the right,
# which settles it with context the left never saw.
#
# Ticket 2 is high-stakes. The left is confident, but may only advise. The right agrees
# with it, and the operator's policy still requires a human to sign off.

LOCKOUT = Ticket(
    goal="Resolve support ticket #4812: \"I reset my password yesterday and now I can't log in at all.\"",
    context=(
        "Customer: Dana R., Team plan, 3 seats. Account flags: locked_out=true (5 failed "
        "sign-ins since the reset). No billing issues. This is their second lockout this month."
    ),
    policy=DEMO_POLICY,
    right=dict(
        plan=Plan(
            situation=(
                "The customer is locked out after a password reset. The account carries a lockout "
                "flag, so the new password is probably fine and the lock is what blocks them."
            ),
            steps=["Remove whatever is blocking sign-in", "Tell the customer and make sure they are back in"],
        ),
        proposals=[
            Proposal(
                options=[
                    Option(id="unlock_account", description="Clear the lockout flag so the new password works"),
                    Option(id="send_reset_link", description="Email the customer a fresh password-reset link"),
                    Option(id="ask_for_details", description="Reply asking which error message the customer sees"),
                ],
                rationale="locked_out=true after five failed attempts points at the lock, not the password.",
                stakes=Stakes.LOW,
                open_ended=False,
            ),
            Proposal(
                options=[
                    Option(id="reply_and_close", description="Tell the customer the account is unlocked and close the ticket"),
                    Option(id="reply_and_wait", description="Tell the customer the account is unlocked and keep the ticket open until they confirm"),
                ],
                rationale="Access should be restored. What remains is telling them, and whether to wait for confirmation.",
                stakes=Stakes.LOW,
                open_ended=False,
            ),
        ],
        evaluations=[
            Evaluation(
                verdict=Verdict.COMMIT,
                option_id="reply_and_wait",
                reasoning=(
                    "The left sees two near-identical replies and leans towards closing. It is not "
                    "weighing that this is the customer's second lockout this month: closing before "
                    "they confirm risks a reopened ticket and a worse experience. Keep it open."
                ),
            )
        ],
        reintegrations=[
            Reintegration(
                situation=(
                    "The lock is cleared, so the customer's new password should now work. They have "
                    "not been told yet, and nothing confirms they are back in."
                ),
                goal_met=False,
            ),
            Reintegration(
                situation=(
                    "The account is unlocked and the customer has been told. The ticket stays open "
                    "until they confirm. Nothing more to do unless they report a further problem."
                ),
                goal_met=True,
            ),
        ],
        # The draft answer gets one fact wrong. The check catches it, and the revision fixes it.
        report=(
            "Ticket #4812: the account had been locked after five failed sign-ins following the "
            "reset. I cleared the lock and told the customer. This was their first lockout, so "
            "I closed the ticket."
        ),
        claims=[
            Claim(text="An account lockout after five failed sign-ins blocks a correct new password until the lock is cleared."),
            Claim(text="This is customer Dana R.'s first account lockout this month."),
        ],
        revision=(
            "Ticket #4812: the account had been locked after five failed sign-ins following the "
            "reset. I cleared the lock and told the customer. The ticket stays open until they "
            "confirm they are back in, since this is their second lockout this month."
        ),
    ),
    left=[
        {
            "select": ChoiceAnswer.from_probabilities(
                {"unlock_account": 0.89, "send_reset_link": 0.08, "ask_for_details": 0.03}
            ),
            "permit:unlock_account": NoulAnswer(probability=0.97),
            "permit:send_reset_link": NoulAnswer(probability=0.98),
            "permit:ask_for_details": NoulAnswer(probability=0.99),
        },
        {
            "select": ChoiceAnswer.from_probabilities({"reply_and_close": 0.56, "reply_and_wait": 0.44}),
            "permit:reply_and_close": NoulAnswer(probability=0.98),
            "permit:reply_and_wait": NoulAnswer(probability=0.98),
        },
        {  # the check of the draft answer: the context says this is the second lockout
            "claim:c1": NoulAnswer(probability=0.93),
            "claim:c2": NoulAnswer(probability=0.04),
        },
    ],
    outcomes={
        "unlock_account": "Lockout flag cleared; failed sign-in count reset to 0.",
        "reply_and_wait": "Reply sent; ticket set to 'waiting on customer'.",
    },
)

REFUND = Ticket(
    goal=(
        "Resolve support ticket #4907: \"Cancel my annual plan and refund all $2,400. I was charged "
        "after I asked to cancel. If this isn't fixed today I'm disputing the charge.\""
    ),
    context=(
        "Customer: Northwind Logistics. Annual plan, $2,400, renewed 9 days ago. A cancellation "
        "request was logged 11 days ago, two days before the renewal, and was never actioned. "
        "Refund terms: full refund within 14 days of renewal."
    ),
    policy=DEMO_POLICY,
    right=dict(
        plan=Plan(
            situation=(
                "We failed to action a cancellation request, and that led to a $2,400 renewal "
                "charge. The mistake is ours, and the customer is threatening a chargeback today."
            ),
            steps=["Decide the remedy", "Tell the customer and close the loop on the missed cancellation"],
        ),
        proposals=[
            Proposal(
                options=[
                    Option(id="full_refund", description="Cancel the plan and refund the full $2,400"),
                    Option(id="prorated_refund", description="Cancel the plan and refund the unused portion, about $2,340"),
                    Option(id="offer_credit", description="Keep the plan active and offer $2,400 in account credit"),
                ],
                rationale="Money leaves the company whichever way this goes, and the relationship is at risk.",
                stakes=Stakes.HIGH,
                open_ended=False,
            )
        ],
        evaluations=[
            Evaluation(
                verdict=Verdict.COMMIT,
                option_id="full_refund",
                reasoning=(
                    "The left is right about the remedy. The cancellation request predates the "
                    "renewal, so the charge was our error and the refund terms cover it. A partial "
                    "refund or a credit would compound the mistake."
                ),
            )
        ],
        reintegrations=[
            Reintegration(
                situation=(
                    "A full refund is the right remedy and is waiting on human sign-off, because it "
                    "is above the $500 limit. The customer has not been answered yet, and the "
                    "chargeback threat makes today a real deadline."
                ),
                goal_met=False,
            )
        ],
        report=(
            "Ticket #4907 needs your sign-off. I recommend cancelling the plan and refunding the "
            "full $2,400: the customer asked to cancel two days before the renewal and we missed "
            "it. The refund is above the $500 limit, so nothing has been sent. They have "
            "threatened a chargeback today."
        ),
        claims=[Claim(text="Northwind Logistics' refund terms give a full refund within 14 days of renewal.")],
    ),
    left=[
        {
            "select": ChoiceAnswer.from_probabilities(
                {"full_refund": 0.86, "prorated_refund": 0.11, "offer_credit": 0.03}
            ),
            "permit:full_refund": NoulAnswer(probability=0.03),
            "permit:prorated_refund": NoulAnswer(probability=0.04),
            "permit:offer_credit": NoulAnswer(probability=0.05),
        },
        {"claim:c1": NoulAnswer(probability=0.97)},
    ],
)

DEMO_TICKETS = [LOCKOUT, REFUND]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
