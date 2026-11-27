"""Request-scoped deadlines and measurements for the serial data coordinator."""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


class RequestDeadlineExceeded(TimeoutError):
    code = "scan_deadline_exceeded"


@dataclass(frozen=True)
class RequestDeadline:
    expires_at: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def from_timeout(
        cls, seconds: float, *, clock: Callable[[], float] = time.monotonic
    ) -> RequestDeadline:
        if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Deadline timeout must be positive and finite.")
        return cls(clock() + seconds, clock)

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self.clock())

    def check(self, stage: str = "operation") -> None:
        if self.remaining() <= 0:
            raise RequestDeadlineExceeded(f"Scan deadline reached before {stage} completed.")


class RuntimeMetrics:
    def __init__(self, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self.clock = clock
        self._counters: dict[str, float | int] = {}
        self._timings: dict[str, float] = {}
        self._lock = threading.Lock()

    def increment(self, name: str, value: float | int = 1) -> None:
        if not math.isfinite(value):
            raise ValueError("Metric values must be finite.")
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        started = self.clock()
        try:
            yield
        finally:
            duration = max(0.0, self.clock() - started)
            with self._lock:
                self._timings[name] = self._timings.get(name, 0.0) + duration

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "timings_seconds": dict(self._timings),
            }

    def merge(self, values: RuntimeMetrics | dict[str, Any]) -> None:
        payload = values.to_dict() if isinstance(values, RuntimeMetrics) else values
        with self._lock:
            for name, value in payload.get("counters", {}).items():
                self._counters[name] = self._counters.get(name, 0) + value
            for name, value in payload.get("timings_seconds", {}).items():
                self._timings[name] = self._timings.get(name, 0.0) + value


def metrics_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        category: {
            name: value - before.get(category, {}).get(name, 0)
            for name, value in after.get(category, {}).items()
        }
        for category in ("counters", "timings_seconds")
    }


_context: ContextVar[tuple[RequestDeadline | None, RuntimeMetrics | None]] = ContextVar(
    "signal_data_operation", default=(None, None)
)


def current_operation() -> tuple[RequestDeadline | None, RuntimeMetrics | None]:
    return _context.get()


@contextmanager
def operation_context(
    *, deadline: RequestDeadline | None = None, metrics: RuntimeMetrics | None = None
) -> Iterator[None]:
    inherited_deadline, inherited_metrics = current_operation()
    selected_deadline = deadline if deadline is not None else inherited_deadline
    if inherited_deadline is not None and deadline is not None:
        # An inner operation must never extend its enclosing request budget.
        if inherited_deadline.remaining() < deadline.remaining():
            selected_deadline = inherited_deadline
    token = _context.set((selected_deadline, metrics if metrics is not None else inherited_metrics))
    try:
        if selected_deadline is not None:
            selected_deadline.check()
        yield
    finally:
        _context.reset(token)
