"""A record of which hemisphere did what, and a printer for it."""

import os
import shutil
import sys
import textwrap
from collections.abc import Callable
from enum import StrEnum
from typing import Any, TextIO

from pydantic import BaseModel, Field


class Actor(StrEnum):
    RIGHT = "right"
    LEFT = "left"
    HARNESS = "harness"


class TraceEvent(BaseModel):
    actor: Actor
    phase: str
    summary: str
    details: list[str] = Field(default_factory=list)
    step: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    """The same event as structured data (the proposal, the decision, ...), for interfaces
    that draw it rather than print it. JSON-ready. The printer ignores it."""


EventSink = Callable[[TraceEvent], None]


class Trace:
    """Collects events for one run and forwards each to an optional sink as it happens."""

    def __init__(self, sink: EventSink | None = None) -> None:
        self.events: list[TraceEvent] = []
        self._sink = sink

    def emit(
        self,
        actor: Actor,
        phase: str,
        summary: str,
        *details: str,
        step: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        event = TraceEvent(
            actor=actor, phase=phase, summary=summary, details=list(details), step=step, data=data or {}
        )
        self.events.append(event)
        if self._sink is not None:
            self._sink(event)


class TracePrinter:
    """An event sink that prints an aligned transcript, coloured by actor on a terminal."""

    _COLOR = {Actor.RIGHT: "35", Actor.LEFT: "36", Actor.HARNESS: "33"}
    _DIM = "2"

    def __init__(
        self, stream: TextIO | None = None, *, color: bool | None = None, width: int | None = None
    ) -> None:
        self._stream = stream or sys.stdout
        if color is None:
            color = self._stream.isatty() and "NO_COLOR" not in os.environ
        self._color = color
        self._width = width or min(shutil.get_terminal_size((100, 24)).columns, 110)
        self._step: int | None = None

    def __call__(self, event: TraceEvent) -> None:
        if event.step is not None and event.step != self._step:
            self._step = event.step
            rule = f"── step {event.step} "
            self._write(self._paint(rule + "─" * max(0, self._width - len(rule)), self._DIM))

        head = f"{event.actor.upper():<8} {event.phase:<12}"
        indent = " " * len(head)
        room = max(30, self._width - len(indent))
        first, *rest = textwrap.wrap(event.summary, room) or [""]
        self._write(self._paint(head, f"1;{self._COLOR[event.actor]}") + first)
        for line in rest:
            self._write(indent + line)
        for detail in event.details:
            for line in textwrap.wrap(detail, room, subsequent_indent="  "):
                self._write(indent + self._paint(line, self._DIM))

    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._color else text

    def _write(self, line: str) -> None:
        print(line.rstrip(), file=self._stream)
