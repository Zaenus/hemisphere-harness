"""Your sign-offs, kept as evaluation scenarios.

Every approval is a real decision with a known answer: the option you approved was the
right one. Whether the agent needed to ask is yours to say. By default it did, since that
is what the policy gate asked for; tick "it may do this alone next time" when it did not.

    hemisphere-harness eval --signoffs          runs the left over them

They are kept next to the run history, in signoffs.json, in the same format as the
scenarios that ship with the package. Rejections are not kept: they say that no option
was right, and a scenario needs one that is.
"""

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path

from ..schemas import HumanDecision, WorldState
from ..trace import TraceEvent
from .runner import Scenario, ScenarioSet

DESCRIPTION = "Decisions a person signed off on, recorded by hemisphere-harness."
MAX_SCENARIOS = 500


def scenario_from_signoff(
    state: WorldState,
    decision: HumanDecision,
    *,
    policy: str,
    run_id: str,
    events: Sequence[TraceEvent] = (),
) -> tuple[Scenario, str] | None:
    """The scenario an approval makes, and the key of its policy. None for a rejection, or
    for a step with fewer than two options, which is no choice to evaluate."""
    if not decision.approve or not decision.option_id or not state.history:
        return None
    step = state.history[-1]
    options = {option.id: option.as_action() for option in step.proposal.options}
    if decision.option_id not in options or not 2 <= len(options) <= 4:
        return None
    reserved = [] if decision.alone_next_time else [decision.option_id]
    allowed = [decision.option_id] if decision.alone_next_time else []
    key = "policy-" + hashlib.sha256(policy.encode()).hexdigest()[:8] if policy else "none"
    scenario = Scenario(
        id=f"signoff-{run_id.replace('-', '')}",
        policy=key,
        goal=state.goal,
        context=state.context,
        situation=_situation_before(state, events),
        options=options,
        best=decision.option_id,
        needs_signoff=reserved,
        allowed=allowed,
    )
    return scenario, key


def record_signoff(path: str | os.PathLike[str], scenario: Scenario, policy_key: str, policy: str) -> None:
    """Add the scenario to the file, replacing one with the same id. The file stays private."""
    path = Path(path)
    if path.exists():
        kept = ScenarioSet.model_validate(json.loads(path.read_text(encoding="utf-8")))
    else:
        kept = ScenarioSet(description=DESCRIPTION, policies={}, scenarios=[])
    kept.policies[policy_key] = policy
    kept.scenarios = [s for s in kept.scenarios if s.id != scenario.id] + [scenario]
    kept.scenarios = kept.scenarios[-MAX_SCENARIOS:]
    used = {s.policy for s in kept.scenarios}
    kept.policies = {k: v for k, v in kept.policies.items() if k in used}
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".json.partial")
    partial.write_text(kept.model_dump_json(indent=1, exclude_defaults=True), encoding="utf-8")
    os.chmod(partial, 0o600)
    os.replace(partial, path)


def _situation_before(state: WorldState, events: Sequence[TraceEvent]) -> str:
    """The situation as the left saw it when it decided the step that stopped."""
    if len(state.history) >= 2 and state.history[-2].reintegration is not None:
        return state.history[-2].reintegration.situation
    plan = next((e for e in events if e.phase == "plan"), None)
    if plan is not None:
        situation = plan.data.get("plan", {}).get("situation")
        if isinstance(situation, str):
            return situation
    return ""
