"""What a run cost: calls, tokens, time and, if you give prices, money, per hemisphere.

Clients report into whichever run is current, through a context variable, so several runs
sharing one client (as in the web UI) each count only their own calls.

    RIGHT_PRICE_INPUT=3       dollars per million input tokens
    RIGHT_PRICE_OUTPUT=15     dollars per million output tokens

Prices are yours to set because they change faster than any table here would.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

Side = Literal["right", "left"]


class HemisphereUsage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    """Time spent waiting for this hemisphere's model, summed over its calls."""

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Usage(BaseModel):
    right: HemisphereUsage = Field(default_factory=HemisphereUsage)
    left: HemisphereUsage = Field(default_factory=HemisphereUsage)
    seconds: float = 0.0
    """Wall-clock time of the run."""
    cost: float | None = None
    """In dollars, for the right's tokens, when prices are set."""
    _started: float = PrivateAttr(default_factory=time.perf_counter)

    def so_far(self) -> "Usage":
        """A copy with the time elapsed until now, for reporting while the run is still open."""
        copy = self.model_copy(deep=True)
        copy.seconds = time.perf_counter() - self._started
        return copy

    def price(self, input_per_million: float | None, output_per_million: float | None) -> None:
        if input_per_million is None and output_per_million is None:
            return
        self.cost = (
            self.right.input_tokens * (input_per_million or 0.0) + self.right.output_tokens * (output_per_million or 0.0)
        ) / 1_000_000

    def add(self, other: "Usage") -> None:
        for side in ("right", "left"):
            mine, theirs = getattr(self, side), getattr(other, side)
            mine.calls += theirs.calls
            mine.input_tokens += theirs.input_tokens
            mine.output_tokens += theirs.output_tokens
            mine.seconds += theirs.seconds
        self.seconds += other.seconds
        if other.cost is not None:
            self.cost = (self.cost or 0.0) + other.cost

    def describe(self) -> str:
        parts = []
        if self.right.tokens:
            parts.append(f"{_count(self.right.tokens)} tokens")
        if self.cost is not None:
            parts.append(f"${self.cost:.4f}" if self.cost < 0.01 else f"${self.cost:.2f}")
        parts.append(f"{self.seconds:.0f} s (right {self.right.seconds:.0f} s, left {self.left.seconds:.1f} s)")
        return " · ".join(parts)


_current: ContextVar[Usage | None] = ContextVar("hemisphere_usage", default=None)


def record(
    side: Side, *, seconds: float, input_tokens: int | None = None, output_tokens: int | None = None, calls: int = 1
) -> None:
    """Count one model call towards the current run. Does nothing outside a run."""
    usage = _current.get()
    if usage is None:
        return
    target = usage.right if side == "right" else usage.left
    target.calls += calls
    target.input_tokens += input_tokens or 0
    target.output_tokens += output_tokens or 0
    target.seconds += seconds


@contextmanager
def tracking() -> Iterator[Usage]:
    """Count the calls made inside the block. Nested blocks each count their own."""
    usage = Usage()
    token = _current.set(usage)
    try:
        yield usage
    finally:
        usage.seconds = time.perf_counter() - usage._started
        _current.reset(token)


def current() -> Usage | None:
    """The usage of the run in progress, if any."""
    return _current.get()


def _count(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)
