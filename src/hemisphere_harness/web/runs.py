"""Runs started from the web UI: launched in the background, streamed live, kept on disk.

Each run is one `Harness.run`, built from the settings as they are when it starts, so a
change saved in the UI applies to the next run without a restart. Actions are always
simulated: the UI has no actuator that touches a real system.
"""

import asyncio
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import HarnessConfig, Settings
from ..errors import ConfigError, HarnessError
from ..examples.simple_loop import LOCKOUT, REFUND, Ticket
from ..harness import Harness
from ..left import ExperimentalLLMLeft, LeftHemisphere, MockLeftHemisphere
from ..providers import build_left, build_right
from ..right import RightHemisphere, ScriptedRightHemisphere
from ..schemas import Option, WorldState
from ..trace import TraceEvent

log = logging.getLogger(__name__)

DEMOS: dict[str, Ticket] = {"lockout": LOCKOUT, "refund": REFUND}

Status = Literal["running", "done", "needs_human", "max_steps", "failed", "cancelled"]


class RunRequest(BaseModel):
    goal: str = Field(default="", max_length=20_000)
    context: str = Field(default="", max_length=50_000)
    policy: str | None = Field(default=None, max_length=5_000)
    """None uses HARNESS_POLICY. An empty string turns the policy gate off for this run."""
    demo: Literal["lockout", "refund"] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=50)


class Run(BaseModel):
    id: str
    created: datetime
    finished: datetime | None = None
    goal: str
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

    def summary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", include={"id", "created", "finished", "goal", "status", "demo", "offline"})


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


class RunManager:
    def __init__(self, store: RunStore, settings: Callable[[], Settings]) -> None:
        self.store = store
        self._settings = settings
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._listeners: dict[str, list[asyncio.Queue[tuple[int, TraceEvent] | None]]] = {}

    def start(self, request: RunRequest) -> Run:
        """Build both hemispheres and start the run. Raises ConfigError if they cannot be built."""
        settings = self._settings()
        ticket = DEMOS[request.demo] if request.demo else None
        goal = ticket.goal if ticket else request.goal.strip()
        context = ticket.context if ticket else request.context.strip()
        if not goal:
            raise ConfigError("Write a goal for the run.")

        config = settings.harness_config()
        if request.max_steps:
            config.max_steps = request.max_steps

        right: RightHemisphere
        left: LeftHemisphere
        if settings.right_model:
            right = build_right(settings)
            try:
                left = build_left(settings)
            except BaseException:
                asyncio.get_running_loop().create_task(right.aclose())
                raise
            offline = False
            right_label = f"{right.name} · {settings.right_model}"
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
            right = ScriptedRightHemisphere(**ticket.right)
            left = MockLeftHemisphere(script=ticket.left)
            offline = True
            right_label = "scripted (offline demo)"
            config.policy = config.policy or ticket.policy
        else:
            raise ConfigError(
                "No right hemisphere is set up. Choose a provider and a model in Settings, "
                "or try one of the offline demos."
            )

        left_model = settings.jev_model if left.name == "jev" else settings.left_model
        left_label = "mock (scripted)" if offline else left.name + (f" · {left_model}" if left_model else "")

        run = Run(
            id=datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3),
            created=datetime.now(UTC),
            goal=goal,
            context=context,
            policy=config.policy,
            demo=request.demo,
            offline=offline,
            right=right_label,
            left=left_label,
            left_experimental=isinstance(left, ExperimentalLLMLeft),
            left_is_mock=isinstance(left, MockLeftHemisphere),
            config=config,
        )
        self.store.runs[run.id] = run
        self.store.save(run)
        self._listeners[run.id] = []
        task = asyncio.get_running_loop().create_task(
            self._execute(run, right, left, config, ticket.outcomes if ticket else {})
        )
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
    ) -> None:
        async def simulate(option: Option, state: WorldState) -> str:
            return outcomes.get(option.id, f"(simulated) {option.description}")

        def on_event(event: TraceEvent) -> None:
            run.events.append(event)
            self._publish(run.id, (len(run.events) - 1, event))
            self.store.save(run)

        try:
            async with Harness(right, left, config=config, act=simulate, on_event=on_event) as harness:
                result = await harness.run(run.goal, context=run.context)
            run.status, run.report = result.status.value, result.report
        except asyncio.CancelledError:
            run.status, run.error = "cancelled", "Stopped from the web UI."
        except HarnessError as exc:
            run.status, run.error = "failed", str(exc)
        except Exception as exc:  # report any crash in the UI rather than losing the run
            log.exception("run %s crashed", run.id)
            run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"

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

    async def events(self, run_id: str, after: int = -1) -> AsyncIterator[tuple[int, TraceEvent]]:
        """Every event after index `after`, then live ones until the run ends."""
        run = self.store.runs[run_id]
        queue: asyncio.Queue[tuple[int, TraceEvent] | None] = asyncio.Queue()
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
                if item[0] > sent:
                    yield item
                    sent = item[0]
        finally:
            if listening and queue in self._listeners.get(run_id, []):
                self._listeners[run_id].remove(queue)

    def _publish(self, run_id: str, item: tuple[int, TraceEvent] | None) -> None:
        for queue in self._listeners.get(run_id, []):
            queue.put_nowait(item)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def run_json(run: Run) -> str:
    return json.dumps(run.model_dump(mode="json"))
