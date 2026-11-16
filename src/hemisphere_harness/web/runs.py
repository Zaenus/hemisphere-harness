"""Runs started from the web UI: launched in the background, streamed live, kept on disk.

Each run is one `Harness.run`, or one `Harness.resume` after a person's sign-off, built
from the settings as they are when it starts, so a change saved in the UI applies to the
next run without a restart. Runs are grouped into conversations: a reply is a new run that
carries the earlier exchanges. Actions are always simulated: the UI has no actuator that
touches a real system.
"""

import asyncio
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import httpx
from pydantic import BaseModel, Field, model_validator

from .. import local
from ..builtin_tools import builtin_toolbox
from ..config import HarnessConfig, Settings
from ..errors import ConfigError, HarnessError
from ..evals.signoffs import record_signoff, scenario_from_signoff
from ..examples.simple_loop import LOCKOUT, REFUND, Ticket
from ..harness import Harness
from ..left import ExperimentalLLMLeft, LeftHemisphere, MockLeftHemisphere
from ..providers import build_left, build_right
from ..right import RightHemisphere, ScriptedRightHemisphere
from ..left.base import LeftState
from ..schemas import (
    Decision,
    Evaluation,
    HumanDecision,
    LeftResult,
    Option,
    Plan,
    Proposal,
    Question,
    Reintegration,
    RunResult,
    StepRecord,
    Turn,
    WorldState,
)
from ..lookup import Searcher, searcher_from_settings
from ..mcp_tools import toolbox_from_settings
from ..memory import MemoryStore
from ..notify import notify, should_notify
from ..tools import CompositeToolbox
from ..trace import Actor, TraceEvent

log = logging.getLogger(__name__)

DEMOS: dict[str, Ticket] = {"lockout": LOCKOUT, "refund": REFUND}

Status = Literal["running", "done", "needs_human", "max_steps", "failed", "cancelled"]


class ConflictError(ConfigError):
    """The request is valid, but not now: something is running, or nothing is waiting."""


class RunRequest(BaseModel):
    goal: str = Field(default="", max_length=20_000)
    context: str = Field(default="", max_length=50_000)
    policy: str | None = Field(default=None, max_length=5_000)
    """None uses HARNESS_POLICY. An empty string turns the policy gate off for this run."""
    demo: Literal["lockout", "refund"] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=50)
    conversation_id: str | None = None
    """Set to reply in an existing conversation. Its context and policy carry over."""


class ResumeRequest(BaseModel):
    """A person's decision on a run that stopped for a human."""

    approve: bool
    option_id: str | None = None
    note: str = Field(default="", max_length=5_000)
    alone_next_time: bool = False
    """The person's view that this did not need their sign-off. Kept with the sign-off."""


class Run(BaseModel):
    id: str
    conversation_id: str = ""
    """The conversation this run belongs to. A first message starts one with its own id."""
    created: datetime
    finished: datetime | None = None
    goal: str
    message: str = ""
    """What the person said for this run: the goal, a reply, or their sign-off."""
    resumed_from: str | None = None
    """The stopped run this one continues, if it continues one."""
    context: str = ""
    policy: str = ""
    demo: str | None = None
    offline: bool
    right: str
    left: str
    left_experimental: bool = False
    left_is_mock: bool = False
    config: HarnessConfig
    status: Status = "running"
    error: str | None = None
    report: str = ""
    events: list[TraceEvent] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    """The tools this run had, if any were connected."""
    state: WorldState | None = None
    """The final world state, kept so that a stopped run can be resumed."""

    @model_validator(mode="after")
    def _defaults(self) -> Self:
        # Runs saved before conversations existed are conversations of one.
        self.conversation_id = self.conversation_id or self.id
        self.message = self.message or self.goal
        return self

    @property
    def can_resume(self) -> bool:
        """Whether a person's sign-off can continue this run. Runs saved before resuming existed cannot."""
        return self.status == "needs_human" and self.state is not None and bool(self.state.history)

    def summary(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            include={"id", "conversation_id", "created", "finished", "goal", "message", "status", "demo", "offline"},
        )


class RunStore:
    """Runs in memory, each also saved as one JSON file so history survives a restart."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.runs: dict[str, Run] = {}
        if directory.is_dir():
            for file in directory.glob("*.json"):
                try:
                    run = Run.model_validate_json(file.read_text(encoding="utf-8"))
                except ValueError as exc:
                    log.warning("skipping unreadable run file %s: %s", file, exc)
                    continue
                if run.status == "running":  # the server stopped while it ran
                    run.status, run.error = "failed", "The server stopped before this run finished."
                self.runs[run.id] = run

    def save(self, run: Run) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{run.id}.json"
        partial = target.with_suffix(".json.partial")
        partial.write_text(run.model_dump_json(indent=1), encoding="utf-8")
        os.replace(partial, target)

    def delete(self, run_id: str) -> None:
        self.runs.pop(run_id, None)
        (self.directory / f"{run_id}.json").unlink(missing_ok=True)

    def newest_first(self) -> list[Run]:
        return sorted(self.runs.values(), key=lambda run: run.created, reverse=True)

    def conversation(self, conversation_id: str) -> list[Run]:
        """Every run in a conversation, oldest first."""
        runs = [run for run in self.runs.values() if run.conversation_id == conversation_id]
        return sorted(runs, key=lambda run: run.created)


class PacedRight(ScriptedRightHemisphere):
    """The offline demo's right, slowed to a watchable pace, its answer streamed word by word."""

    def __init__(self, pace: float, **script: Any) -> None:
        super().__init__(**script)
        self._pace = pace

    async def plan(self, state: WorldState) -> Plan:
        await asyncio.sleep(self._pace)
        return await super().plan(state)

    async def propose(self, state: WorldState) -> Proposal:
        await asyncio.sleep(self._pace)
        return await super().propose(state)

    async def evaluate(self, state: WorldState, proposal: Proposal, decision: Decision) -> Evaluation:
        await asyncio.sleep(self._pace)
        return await super().evaluate(state, proposal, decision)

    async def reintegrate(self, state: WorldState, step: StepRecord) -> Reintegration:
        await asyncio.sleep(self._pace)
        return await super().reintegrate(state, step)

    async def stream(self, prompt: str, state: WorldState | None = None) -> AsyncIterator[str]:
        text = await self.generate(prompt, state)
        for word in text.split(" "):
            await asyncio.sleep(self._pace / 12)
            yield word + " "


class PacedLeft(MockLeftHemisphere):
    def __init__(self, pace: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pace = pace

    async def _decide(self, state: LeftState, questions: Mapping[str, Question]) -> LeftResult:
        await asyncio.sleep(self._pace / 2)
        return await super()._decide(state, questions)


def _tools_event(toolbox: CompositeToolbox) -> TraceEvent:
    names = [tool.name for tool in toolbox.tools]
    problems = toolbox.problems
    summary = f"{len(names)} tool(s) available"
    if toolbox.errors:
        summary += f"; {len(toolbox.errors)} server(s) failed"
    if problems:
        summary += f"; {len(problems)} built-in(s) left out"
    details = [f"{server}: {error}" for server, error in toolbox.errors.items()]
    details += [f"{name} left out: {why}" for name, why in problems.items()]
    return TraceEvent(
        actor=Actor.HARNESS,
        phase="tools",
        summary=summary,
        details=details,
        data={
            "tools": [tool.model_dump(mode="json") for tool in toolbox.tools],
            "errors": toolbox.errors,
            "problems": problems,
        },
    )


def _new_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


class RunManager:
    def __init__(
        self,
        store: RunStore,
        settings: Callable[[], Settings],
        *,
        demo_pace_s: float = 0.0,
        base_url: str = "http://127.0.0.1:8765/",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self._settings = settings
        self._pace = demo_pace_s
        self._base_url = base_url
        self._transport = transport
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._listeners: dict[str, list[asyncio.Queue[tuple[int | None, TraceEvent] | None]]] = {}

    def memory(self, settings: Settings | None = None) -> MemoryStore:
        """The memory file: HARNESS_MEMORY_FILE, or memory.json next to the run history."""
        settings = settings or self._settings()
        return MemoryStore(settings.harness_memory_file or self.store.directory.parent / "memory.json")

    def _memory_for_run(self, settings: Settings, offline: bool) -> MemoryStore | None:
        return None if offline or not settings.harness_memory else self.memory(settings)

    def start(self, request: RunRequest) -> Run:
        """Build both hemispheres and start a run: a first message, or a reply in a conversation.

        Raises ConfigError if the hemispheres cannot be built or the request makes no sense.
        """
        settings = self._settings()
        if request.conversation_id:
            return self._reply(request, settings)

        ticket = DEMOS[request.demo] if request.demo else None
        goal = ticket.goal if ticket else request.goal.strip()
        context = ticket.context if ticket else request.context.strip()
        if not goal:
            raise ConfigError("Write a goal for the run.")

        config = settings.harness_config()
        if request.max_steps:
            config.max_steps = request.max_steps

        if settings.right_model:
            right, left = self._build_live(settings)
            if ticket:
                # As in the command-line example: a mock left answers every policy question
                # 0.5, so the demo's policy applies only when something can judge it.
                if not isinstance(left, MockLeftHemisphere):
                    config.policy = config.policy or ticket.policy
            else:
                config.policy = settings.harness_policy if request.policy is None else request.policy.strip()
            if isinstance(left, MockLeftHemisphere):
                config.check_answers = False  # it would call every claim unsure
        elif ticket:
            right = PacedRight(self._pace, **ticket.right)
            left = PacedLeft(self._pace, script=ticket.left)
            config.policy = config.policy or ticket.policy
        else:
            raise ConfigError(
                "No right hemisphere is set up. Choose a provider and a model in Settings, "
                "or try one of the offline demos."
            )

        run = self._new_run(
            settings, right, left, config, goal=goal, context=context, demo=request.demo, offline=not settings.right_model
        )
        outcomes = ticket.outcomes if ticket else {}
        tools = None if run.offline else settings.harness_mcp_config
        searcher = None if run.offline else searcher_from_settings(settings)
        memory = self._memory_for_run(settings, run.offline)
        return self._launch(run, right, left, config, outcomes, lambda harness: harness.run(goal, context=context), tools, searcher, memory)

    def _reply(self, request: RunRequest, settings: Settings) -> Run:
        earlier = self.store.conversation(request.conversation_id or "")
        if not earlier:
            raise ConfigError("No such conversation.")
        self._ensure_idle(earlier)
        message = request.goal.strip()
        if not message:
            raise ConfigError("Write a reply.")
        if not settings.right_model:
            raise ConfigError("Replies need a right hemisphere. Choose a provider and a model in Settings.")

        first = earlier[0]
        config = first.config.model_copy()
        if request.max_steps:
            config.max_steps = request.max_steps
        right, left = self._build_live(settings)
        config.check_answers = settings.harness_check_answers and not isinstance(left, MockLeftHemisphere)
        turns = [Turn(request=run.message, answer=run.report) for run in earlier if run.report]

        run = self._new_run(
            settings, right, left, config, goal=message, context=first.context, demo=None, offline=False,
            conversation_id=first.conversation_id,
        )  # fmt: skip
        return self._launch(
            run, right, left, config, {}, lambda harness: harness.run(message, context=first.context, conversation=turns),
            settings.harness_mcp_config, searcher_from_settings(settings), self._memory_for_run(settings, False),
        )

    def resume(self, run_id: str, request: ResumeRequest) -> Run:
        """Continue a run that stopped for a human, as a new run in the same conversation."""
        stopped = self.store.runs.get(run_id)
        if stopped is None:
            raise ConfigError("No such run.")
        conversation = self.store.conversation(stopped.conversation_id)
        self._ensure_idle(conversation)
        if stopped.status != "needs_human" or stopped.state is None or not stopped.state.history:
            raise ConflictError("This run is not waiting for a decision.")
        if conversation[-1].id != stopped.id:
            raise ConflictError("The conversation has moved on since this run stopped.")

        options = stopped.state.history[-1].proposal.options
        option = next((o for o in options if o.id == request.option_id), None)
        if request.approve and option is None:
            raise ConfigError(f"'{request.option_id}' is not one of: {', '.join(o.id for o in options)}")
        decision = HumanDecision(
            approve=request.approve, option_id=request.option_id if request.approve else None, note=request.note,
            alone_next_time=request.approve and request.alone_next_time,
        )  # fmt: skip
        if not stopped.demo:
            self._keep_signoff(stopped, decision)
        said = f"Approved: {option.description}" if option is not None and request.approve else "Rejected those options"
        if decision.note.strip():
            said += f" — {decision.note.strip()}"

        settings = self._settings()
        ticket = DEMOS.get(stopped.demo or "")
        config = stopped.config.model_copy()
        if settings.right_model:
            right, left = self._build_live(settings)
            if isinstance(left, MockLeftHemisphere):
                config.check_answers = False
        elif ticket and ticket.after_signoff and request.approve:
            right, left = PacedRight(self._pace, **ticket.after_signoff), PacedLeft(self._pace)
        elif ticket:
            raise ConfigError(
                "The offline demo is scripted only for an approval. Set up a right hemisphere in "
                "Settings to see what happens after a rejection."
            )
        else:
            raise ConfigError("Continuing needs a right hemisphere. Choose a provider and a model in Settings.")

        state = stopped.state
        run = self._new_run(
            settings, right, left, config, goal=stopped.goal, context=stopped.context, demo=stopped.demo,
            offline=not settings.right_model, conversation_id=stopped.conversation_id, message=said, resumed_from=stopped.id,
        )  # fmt: skip
        outcomes = ticket.outcomes if ticket else {}
        tools = None if run.offline else settings.harness_mcp_config
        searcher = None if run.offline else searcher_from_settings(settings)
        memory = self._memory_for_run(settings, run.offline)
        return self._launch(run, right, left, config, outcomes, lambda harness: harness.resume(state, decision), tools, searcher, memory)

    @property
    def signoffs_file(self) -> Path:
        return self.store.directory.parent / "signoffs.json"

    def _keep_signoff(self, stopped: Run, decision: HumanDecision) -> None:
        """Keep an approval as an evaluation scenario. Best-effort: never blocks the sign-off."""
        assert stopped.state is not None
        made = scenario_from_signoff(stopped.state, decision, policy=stopped.config.policy, run_id=stopped.id, events=stopped.events)
        if made is None:
            return
        try:
            record_signoff(self.signoffs_file, made[0], made[1], stopped.config.policy)
        except (OSError, ValueError):
            pass

    def _ensure_idle(self, conversation: list[Run]) -> None:
        if any(self.is_running(run.id) for run in conversation):
            raise ConflictError("Wait for the current run to finish, or stop it, before replying.")

    def _build_live(self, settings: Settings) -> tuple[RightHemisphere, LeftHemisphere]:
        right = build_right(settings)
        try:
            left = build_left(settings)
        except BaseException:
            asyncio.get_running_loop().create_task(right.aclose())
            raise
        return right, left

    def _new_run(
        self,
        settings: Settings,
        right: RightHemisphere,
        left: LeftHemisphere,
        config: HarnessConfig,
        *,
        goal: str,
        context: str,
        demo: str | None,
        offline: bool,
        conversation_id: str = "",
        message: str = "",
        resumed_from: str | None = None,
    ) -> Run:
        left_model = settings.jev_model if left.name == "jev" else settings.left_model
        return Run(
            id=_new_id(),
            conversation_id=conversation_id,
            created=datetime.now(UTC),
            goal=goal,
            message=message,
            resumed_from=resumed_from,
            context=context,
            policy=config.policy,
            demo=demo,
            offline=offline,
            right="scripted (offline demo)" if offline else f"{right.name} · {settings.right_model}",
            left="mock (scripted)" if offline else left.name + (f" · {left_model}" if left_model else ""),
            left_experimental=isinstance(left, ExperimentalLLMLeft),
            left_is_mock=isinstance(left, MockLeftHemisphere),
            config=config,
        )

    def _launch(
        self,
        run: Run,
        right: RightHemisphere,
        left: LeftHemisphere,
        config: HarnessConfig,
        outcomes: dict[str, str],
        go: Callable[[Harness], Awaitable[RunResult]],
        tools: str | None = None,
        searcher: Searcher | None = None,
        memory: MemoryStore | None = None,
    ) -> Run:
        self.store.runs[run.id] = run
        self.store.save(run)
        self._listeners[run.id] = []
        task = asyncio.get_running_loop().create_task(self._execute(run, right, left, config, outcomes, go, tools, searcher, memory))
        self._tasks[run.id] = task
        # A done-callback, not a `finally`: a task cancelled before it first runs never
        # enters its coroutine, and the run would otherwise stay "running" for ever.
        task.add_done_callback(lambda done: self._finish(run, right, left))
        return run

    async def _execute(
        self,
        run: Run,
        right: RightHemisphere,
        left: LeftHemisphere,
        config: HarnessConfig,
        outcomes: dict[str, str],
        go: Callable[[Harness], Awaitable[RunResult]],
        tools: str | None = None,
        searcher: Searcher | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        async def simulate(option: Option, state: WorldState) -> str:
            return outcomes.get(option.id, f"(simulated) {option.description}")

        def on_event(event: TraceEvent) -> None:
            if event.transient:  # a live update, such as a piece of the answer: shown, not kept
                self._publish(run.id, (None, event))
                return
            run.events.append(event)
            self._publish(run.id, (len(run.events) - 1, event))
            self.store.save(run)

        # The outcome is recorded only after the tools are disconnected: a run that says it
        # is finished must not still hold a server process.
        outcome: dict[str, Any]
        box: CompositeToolbox | None = None
        try:
            # Tools are connected for this run only, inside its task, and closed when it ends.
            # An offline run passes no config and gets no built-ins: the script alone.
            current = self._settings()
            mcp = await toolbox_from_settings(tools, log_dir=self.store.directory.parent, local_only=current.harness_local_only)
            builtins = None if run.offline else builtin_toolbox(current, searcher, transport=self._transport)
            box = CompositeToolbox(mcp, builtins)
            if box.parts:
                run.tools = [tool.name for tool in box.tools]
                on_event(_tools_event(box))
            async with Harness(
                right, left, config=config, act=simulate, on_event=on_event,
                toolbox=box if box.parts else None, searcher=searcher, memory=memory,
            ) as harness:
                result = await go(harness)
            outcome = {"status": result.status.value, "report": result.report, "state": result.state}
        except asyncio.CancelledError:
            outcome = {"status": "cancelled", "error": "Stopped from the web UI."}
        except HarnessError as exc:
            outcome = {"status": "failed", "error": str(exc)}
        except Exception as exc:  # report any crash in the UI rather than losing the run
            log.exception("run %s crashed", run.id)
            outcome = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if box is not None:
                await box.aclose()  # the MCP servers and the built-ins; never the shared searcher
            if searcher is not None:
                await searcher.aclose()
        for key, value in outcome.items():
            setattr(run, key, value)
        await self._notify(run)

    async def _notify(self, run: Run) -> None:
        settings = self._settings()
        if not settings.harness_notify_url or not should_notify(settings.harness_notify_on, run.status):
            return
        if settings.harness_local_only and not local.is_local_url(settings.harness_notify_url):
            log.warning("HARNESS_LOCAL_ONLY is on, so the notification webhook, which is not local, was not called")
            return
        link = f"{self._base_url}#/c/{run.conversation_id}"
        await notify(settings.harness_notify_url, run_id=run.id, status=run.status, goal=run.goal, link=link, transport=self._transport)

    def _finish(self, run: Run, right: RightHemisphere, left: LeftHemisphere) -> None:
        if run.status == "running":  # stopped before the harness started, so nothing closed them
            run.status, run.error = "cancelled", "Stopped from the web UI."
            loop = asyncio.get_running_loop()
            for hemisphere in (right, left):
                loop.create_task(hemisphere.aclose())
        run.finished = datetime.now(UTC)
        self.store.save(run)
        self._publish(run.id, None)
        self._listeners.pop(run.id, None)
        self._tasks.pop(run.id, None)

    def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None:
            return False
        task.cancel()
        return True

    def is_running(self, run_id: str) -> bool:
        return run_id in self._tasks

    async def events(self, run_id: str, after: int = -1) -> AsyncIterator[tuple[int | None, TraceEvent]]:
        """Every event after index `after`, then live ones until the run ends.

        Live updates that are not kept (index None) come through only while connected.
        """
        run = self.store.runs[run_id]
        queue: asyncio.Queue[tuple[int | None, TraceEvent] | None] = asyncio.Queue()
        listening = run_id in self._listeners
        if listening:
            self._listeners[run_id].append(queue)
        try:
            sent = after
            for index, event in enumerate(list(run.events)):
                if index > sent:
                    yield index, event
                    sent = index
            while listening:
                item = await queue.get()
                if item is None:
                    break
                position = item[0]
                if position is None:
                    yield item
                elif position > sent:
                    yield item
                    sent = position
        finally:
            if listening and queue in self._listeners.get(run_id, []):
                self._listeners[run_id].remove(queue)

    def _publish(self, run_id: str, item: tuple[int | None, TraceEvent] | None) -> None:
        for queue in self._listeners.get(run_id, []):
            queue.put_nowait(item)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def run_json(run: Run) -> str:
    return json.dumps(run.model_dump(mode="json"))
