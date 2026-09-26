"""Run budgets: hard ceilings on tokens, tool calls, wall-clock time and replans."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .llm import LLM, Completion
from .metrics import RunMetrics


class BudgetExceeded(RuntimeError):
    def __init__(self, resource: str, used: float, limit: float) -> None:
        super().__init__(f"{resource} budget exhausted ({used:g} / {limit:g})")
        self.resource = resource


@dataclass
class Budget:
    max_tokens: int = 250_000
    max_tool_calls: int = 60
    max_seconds: float = 1800
    max_replans: int = 2
    max_repairs: int = 1


class BudgetMeter:
    """Tracks consumption against a ``Budget``.

    Usage is persisted in the run state, so limits hold across resumes: the
    wall-clock counter adds this session's elapsed time to what earlier
    sessions already spent.
    """

    def __init__(
        self, budget: Budget, usage: dict[str, float] | None = None, metrics: dict[str, Any] | None = None
    ) -> None:
        self.budget = budget
        self.metrics = RunMetrics(metrics)
        usage = usage or {}
        self.tokens = int(usage.get("tokens", 0))
        self.tool_calls = int(usage.get("tool_calls", 0))
        self.llm_calls = int(usage.get("llm_calls", 0))
        self._prior_seconds = float(usage.get("seconds", 0.0))
        self._started = time.monotonic()

    @property
    def seconds(self) -> float:
        return self._prior_seconds + (time.monotonic() - self._started)

    def snapshot(self) -> dict[str, float]:
        return {
            "tokens": self.tokens,
            "tool_calls": self.tool_calls,
            "llm_calls": self.llm_calls,
            "seconds": round(self.seconds, 2),
        }

    def check(self) -> None:
        b = self.budget
        if self.tokens >= b.max_tokens:
            raise BudgetExceeded("token", self.tokens, b.max_tokens)
        if self.tool_calls >= b.max_tool_calls:
            raise BudgetExceeded("tool call", self.tool_calls, b.max_tool_calls)
        if self.seconds >= b.max_seconds:
            raise BudgetExceeded("time", round(self.seconds), b.max_seconds)

    def charge_tool_call(self) -> None:
        self.check()
        self.tool_calls += 1


class MeteredLLM:
    """Wraps an LLM so every call is checked against and charged to the budget."""

    def __init__(self, inner: LLM, meter: BudgetMeter) -> None:
        self.inner = inner
        self.meter = meter

    async def complete(self, system: str, user: str, *, purpose: str, json_mode: bool = False) -> Completion:
        self.meter.check()
        started = time.monotonic()
        try:
            result = await self.inner.complete(system, user, purpose=purpose, json_mode=json_mode)
        except Exception:
            self.meter.metrics.record_llm(purpose, tokens=0, seconds=time.monotonic() - started, error=True)
            raise
        self.meter.tokens += result.tokens
        self.meter.llm_calls += 1
        self.meter.metrics.record_llm(purpose, tokens=result.tokens, seconds=time.monotonic() - started)
        return result
