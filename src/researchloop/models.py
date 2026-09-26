"""Core data types shared by every stage of a research run.

Everything here is a plain dataclass that round-trips through JSON, so a run
can be checkpointed to SQLite after any stage and rebuilt exactly on resume.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

CITATION_RE = re.compile(r"\[(E\d+)\]")
TERMINAL = frozenset({"done", "failed", "cancelled"})
# Statuses a worker must not pick up: finished, or waiting for a human.
NOT_CLAIMABLE = ("done", "failed", "cancelled", "halted", "paused", "awaiting_approval")


class PlanError(ValueError):
    """The planner produced a task graph that cannot be executed."""


@dataclass
class Task:
    id: str
    question: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | done | failed
    round: int = 0


@dataclass
class Plan:
    tasks: list[Task] = field(default_factory=list)

    def get(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)

    def validate(self) -> None:
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise PlanError(f"duplicate task ids: {ids}")
        known = set(ids)
        for task in self.tasks:
            missing = [d for d in task.depends_on if d not in known]
            if missing:
                raise PlanError(f"task {task.id} depends on unknown tasks {missing}")
            if task.id in task.depends_on:
                raise PlanError(f"task {task.id} depends on itself")
        self.waves()  # raises on cycles

    def waves(self) -> list[list[Task]]:
        """Group pending tasks into layers that can run concurrently.

        A task is ready once all of its dependencies have left the pending
        state; failed dependencies still unblock it, and the executor passes
        along whatever (possibly empty) findings they produced.
        """
        settled = {t.id for t in self.tasks if t.status != "pending"}
        pending = [t for t in self.tasks if t.status == "pending"]
        waves: list[list[Task]] = []
        while pending:
            ready = [t for t in pending if all(d in settled for d in t.depends_on)]
            if not ready:
                cycle = ", ".join(t.id for t in pending)
                raise PlanError(f"dependency cycle among: {cycle}")
            waves.append(ready)
            settled.update(t.id for t in ready)
            pending = [t for t in pending if t.id not in settled]
        return waves


@dataclass
class Evidence:
    id: str
    url: str
    title: str
    content: str
    task_id: str
    query: str

    def card(self, max_chars: int = 600) -> str:
        """Compact, citable view of the evidence for use inside prompts."""
        text = " ".join(self.content.split())
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return f"[{self.id}] {self.title} ({self.url})\n{text}"


@dataclass
class Finding:
    task_id: str
    summary: str
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class Section:
    heading: str
    body: str

    def citations(self) -> list[str]:
        return CITATION_RE.findall(self.body)


@dataclass
class Report:
    title: str
    sections: list[Section] = field(default_factory=list)

    def citations(self) -> list[str]:
        """Every cited evidence id, in first-appearance order, without repeats."""
        seen: dict[str, None] = {}
        for section in self.sections:
            for cid in section.citations():
                seen.setdefault(cid, None)
        return list(seen)


@dataclass
class Issue:
    kind: str  # "report" issues are fixed by rewriting; "evidence" issues by replanning
    code: str
    detail: str  # instruction for the model that repairs the report
    note: str | None = None  # reader-facing wording for the Limitations section, if different


@dataclass
class RunState:
    question: str
    stage: str = "plan"  # plan | execute | report | verify | done | failed | cancelled
    plan: Plan = field(default_factory=Plan)
    findings: dict[str, Finding] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    report: Report | None = None
    issues: list[Issue] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    replans: int = 0
    repairs: int = 0
    usage: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    faithfulness: list[dict[str, float]] = field(default_factory=list)  # one summary per judged draft
    hold: str | None = None  # "paused" | "awaiting_approval": not claimable until released
    approve_plan: bool = False  # stop after planning until a human approves (or edits) the plan
    metrics: dict = field(default_factory=dict)  # see metrics.RunMetrics

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        report = data.get("report")
        return cls(
            question=data["question"],
            stage=data["stage"],
            plan=Plan([Task(**t) for t in data["plan"]["tasks"]]),
            findings={k: Finding(**v) for k, v in data["findings"].items()},
            evidence=[Evidence(**e) for e in data["evidence"]],
            report=Report(report["title"], [Section(**s) for s in report["sections"]]) if report else None,
            issues=[Issue(**i) for i in data["issues"]],
            gaps=list(data["gaps"]),
            replans=data["replans"],
            repairs=data["repairs"],
            usage=dict(data["usage"]),
            error=data.get("error"),
            faithfulness=list(data.get("faithfulness", [])),
            hold=data.get("hold"),
            approve_plan=bool(data.get("approve_plan", False)),
            metrics=dict(data.get("metrics", {})),
        )

    @property
    def status(self) -> str:
        """Externally visible status: a terminal stage, a hold, "halted", or the current stage."""
        if self.stage in TERMINAL:
            return self.stage
        if self.hold:
            return self.hold
        if self.error:
            return "halted"
        return self.stage
