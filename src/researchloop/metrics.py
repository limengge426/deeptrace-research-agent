"""Run metrics: LLM usage per purpose, tool calls per tool, time per stage.

Metrics live in the run state, so they are checkpointed with it and keep
accumulating across crashes and resumes. ``summarize`` aggregates them across
runs for the API's /metrics endpoint.
"""

from __future__ import annotations

import statistics
from typing import Any, Iterable

from .models import RunState


def _bucket(table: dict[str, dict[str, float]], key: str, fields: tuple[str, ...]) -> dict[str, float]:
    return table.setdefault(key, {f: 0 for f in fields})


class RunMetrics:
    LLM_FIELDS = ("calls", "tokens", "seconds", "errors")
    TOOL_FIELDS = ("calls", "cache_hits", "seconds", "errors")
    STAGE_FIELDS = ("steps", "seconds")

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self.llm: dict[str, dict[str, float]] = {k: dict(v) for k, v in data.get("llm", {}).items()}
        self.tools: dict[str, dict[str, float]] = {k: dict(v) for k, v in data.get("tools", {}).items()}
        self.stages: dict[str, dict[str, float]] = {k: dict(v) for k, v in data.get("stages", {}).items()}

    def record_llm(self, purpose: str, *, tokens: int, seconds: float, error: bool = False) -> None:
        b = _bucket(self.llm, purpose, self.LLM_FIELDS)
        b["calls"] += 1
        b["tokens"] += tokens
        b["seconds"] = round(b["seconds"] + seconds, 3)
        b["errors"] += int(error)

    def record_tool(self, tool: str, *, cached: bool, seconds: float = 0.0, error: bool = False) -> None:
        b = _bucket(self.tools, tool, self.TOOL_FIELDS)
        if cached:
            b["cache_hits"] += 1
        else:
            b["calls"] += 1
            b["seconds"] = round(b["seconds"] + seconds, 3)
        b["errors"] += int(error)

    def record_stage(self, stage: str, seconds: float) -> None:
        b = _bucket(self.stages, stage, self.STAGE_FIELDS)
        b["steps"] += 1
        b["seconds"] = round(b["seconds"] + seconds, 3)

    def to_dict(self) -> dict[str, Any]:
        return {"llm": self.llm, "tools": self.tools, "stages": self.stages}


def run_metrics(state: RunState) -> dict[str, Any]:
    """Metrics of one run plus a few derived numbers."""
    m = RunMetrics(state.metrics)
    lookups = sum(t["calls"] + t["cache_hits"] for t in m.tools.values())
    hits = sum(t["cache_hits"] for t in m.tools.values())
    final_faithfulness = state.faithfulness[-1] if state.faithfulness else None
    return {
        "status": state.status,
        "usage": state.usage,
        **m.to_dict(),
        "derived": {
            "tool_cache_hit_rate": round(hits / lookups, 4) if lookups else None,
            "evidence": len(state.evidence),
            "tasks": len(state.plan.tasks),
            "replans": state.replans,
            "repairs": state.repairs,
            "support_rate_first_draft": state.faithfulness[0]["support_rate"] if state.faithfulness else None,
            "support_rate_final": final_faithfulness["support_rate"] if final_faithfulness else None,
        },
    }


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 3)
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1], 3)


def summarize(states: Iterable[RunState]) -> dict[str, Any]:
    """Aggregate metrics across runs."""
    states = list(states)
    by_status: dict[str, int] = {}
    llm: dict[str, dict[str, float]] = {}
    tools: dict[str, dict[str, float]] = {}
    for st in states:
        by_status[st.status] = by_status.get(st.status, 0) + 1
        m = RunMetrics(st.metrics)
        for table, source in ((llm, m.llm), (tools, m.tools)):
            for key, fields in source.items():
                agg = table.setdefault(key, {})
                for f, v in fields.items():
                    agg[f] = round(agg.get(f, 0) + v, 3)

    done = [st for st in states if st.status == "done"]
    seconds = [st.usage.get("seconds", 0.0) for st in done]
    tokens = [st.usage.get("tokens", 0.0) for st in done]
    lookups = sum(t.get("calls", 0) + t.get("cache_hits", 0) for t in tools.values())
    first = [st.faithfulness[0]["support_rate"] for st in done if st.faithfulness]
    final = [st.faithfulness[-1]["support_rate"] for st in done if st.faithfulness]
    return {
        "runs": len(states),
        "by_status": by_status,
        "done": {
            "seconds_p50": _pct(seconds, 50),
            "seconds_p95": _pct(seconds, 95),
            "tokens_p50": _pct(tokens, 50),
            "tokens_p95": _pct(tokens, 95),
            "support_rate_first_draft_mean": round(statistics.mean(first), 4) if first else None,
            "support_rate_final_mean": round(statistics.mean(final), 4) if final else None,
        },
        "llm": llm,
        "tools": tools,
        "tool_cache_hit_rate": round(sum(t.get("cache_hits", 0) for t in tools.values()) / lookups, 4)
        if lookups
        else None,
    }


def to_prometheus(summary: dict[str, Any]) -> str:
    """Render ``summarize`` output in the Prometheus text exposition format."""
    lines = [
        "# HELP researchloop_runs Runs by status.",
        "# TYPE researchloop_runs gauge",
        *[f'researchloop_runs{{status="{s}"}} {n}' for s, n in sorted(summary["by_status"].items())],
        "# HELP researchloop_llm_tokens_total LLM tokens by call purpose.",
        "# TYPE researchloop_llm_tokens_total counter",
        *[f'researchloop_llm_tokens_total{{purpose="{p}"}} {v["tokens"]}' for p, v in sorted(summary["llm"].items())],
        "# HELP researchloop_llm_calls_total LLM calls by call purpose.",
        "# TYPE researchloop_llm_calls_total counter",
        *[f'researchloop_llm_calls_total{{purpose="{p}"}} {v["calls"]}' for p, v in sorted(summary["llm"].items())],
        "# HELP researchloop_tool_calls_total Executed tool calls (cache misses) by tool.",
        "# TYPE researchloop_tool_calls_total counter",
        *[f'researchloop_tool_calls_total{{tool="{t}"}} {v["calls"]}' for t, v in sorted(summary["tools"].items())],
        "# HELP researchloop_tool_cache_hits_total Tool calls served from the idempotency cache.",
        "# TYPE researchloop_tool_cache_hits_total counter",
        *[f'researchloop_tool_cache_hits_total{{tool="{t}"}} {v["cache_hits"]}'
          for t, v in sorted(summary["tools"].items())],
    ]
    for key in ("seconds_p50", "seconds_p95", "support_rate_final_mean"):
        value = summary["done"][key]
        if value is not None:
            lines += [f"# TYPE researchloop_done_{key} gauge", f"researchloop_done_{key} {value}"]
    return "\n".join(lines) + "\n"
