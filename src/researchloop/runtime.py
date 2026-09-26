"""The research loop: Plan -> Execute -> Report -> Verify -> (Repair | Replan | Done).

Every stage transition, and every finished wave of tasks, is checkpointed to
SQLite. A crashed run resumes from its last checkpoint, and cached tool calls
mean already-searched queries are not searched again.

A run is only driven while holding its lease. A background heartbeat renews the
lease; if the process dies, the lease expires and any other worker can take the
run over. Checkpoints are fenced by the lease token, so a stalled worker that
lost its lease cannot overwrite the new owner's progress.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from dataclasses import dataclass

from . import planner, reporter, verifier
from .budget import Budget, BudgetExceeded, BudgetMeter, MeteredLLM
from .executor import TaskExecutor
from .ledger import EvidenceLedger
from .llm import LLM
from .models import Plan, RunState
from .search import SearchProvider
from .store import Lease, LeaseLost, RunStore

TERMINAL = {"done", "failed"}


class RunLocked(RuntimeError):
    """Another live worker holds this run's lease."""


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


@dataclass
class RunResult:
    run_id: str
    state: RunState
    markdown: str | None

    @property
    def status(self) -> str:
        return "halted" if self.state.error and self.state.stage not in TERMINAL else self.state.stage


@dataclass
class _Session:
    """Per-drive objects rebuilt from the checkpoint each time a run is (re)started."""

    run_id: str
    state: RunState
    lease: Lease
    llm: MeteredLLM
    meter: BudgetMeter
    ledger: EvidenceLedger
    executor: TaskExecutor


class ResearchRuntime:
    def __init__(
        self,
        llm: LLM,
        search: SearchProvider,
        store: RunStore,
        *,
        budget: Budget | None = None,
        max_tasks: int = 5,
        queries_per_task: int = 2,
        hits_per_query: int = 4,
        concurrency: int = 3,
        critic: bool = False,
        owner: str | None = None,
        lease_ttl: float = 30.0,
        max_attempts: int = 3,
    ) -> None:
        self.llm = llm
        self.search = search
        self.store = store
        self.budget = budget or Budget()
        self.max_tasks = max_tasks
        self.queries_per_task = queries_per_task
        self.hits_per_query = hits_per_query
        self.concurrency = concurrency
        self.critic = critic
        self.owner = owner or default_owner()
        self.lease_ttl = lease_ttl
        self.max_attempts = max_attempts

    # -- public API --------------------------------------------------------

    def create(self, question: str) -> str:
        """Register a run without executing it; any worker can then claim it."""
        run_id = self.store.create_run(RunState(question=question))
        self.store.log(run_id, "run_created", question=question)
        return run_id

    async def start(self, question: str) -> RunResult:
        return await self.resume(self.create(question))

    async def resume(self, run_id: str) -> RunResult:
        """Drive a run to completion (or a halt), holding its lease throughout."""
        state = self.store.load(run_id)
        if state.stage in TERMINAL:
            return self._result(run_id, state, EvidenceLedger(state.evidence))
        lease = self.store.acquire(run_id, self.owner, self.lease_ttl)
        if lease is None:
            holder = self.store.lease_holder(run_id)
            detail = f" by {holder[0]} (lease expires in {holder[1]:.0f}s)" if holder else ""
            raise RunLocked(f"run {run_id} is being driven{detail}")
        state = self.store.load(run_id)  # re-read: the previous owner may have progressed
        if state.stage in TERMINAL:
            self.store.release(lease)
            return self._result(run_id, state, EvidenceLedger(state.evidence))
        state.error = None
        self.store.log(run_id, "run_claimed", owner=self.owner, token=lease.token, stage=state.stage)

        heartbeat = asyncio.create_task(self._heartbeat(lease))
        try:
            return await self._drive(run_id, state, lease)
        except Exception as exc:
            # A persistent error (bad API key, provider outage, ...) must not make
            # workers retry the run forever: give up after max_attempts claims.
            if not isinstance(exc, LeaseLost) and self._attempts(run_id) >= self.max_attempts:
                failed = self.store.load(run_id)
                failed.stage, failed.error = "failed", f"gave up after {self.max_attempts} attempts: {exc}"
                self.store.checkpoint(run_id, failed, status="failed", lease=lease)
                self.store.log(run_id, "run_finished", status="failed", error=failed.error)
            raise
        finally:
            heartbeat.cancel()
            self.store.release(lease)

    def _attempts(self, run_id: str) -> int:
        return sum(1 for e in self.store.events(run_id) if e.kind == "run_claimed")

    def result(self, run_id: str) -> RunResult:
        """The run's current state and, once it is done, its rendered report."""
        state = self.store.load(run_id)
        return self._result(run_id, state, EvidenceLedger(state.evidence))

    async def _heartbeat(self, lease: Lease) -> None:
        while True:
            await asyncio.sleep(self.lease_ttl / 3)
            if not self.store.renew(lease, self.lease_ttl):
                self.store.log(lease.run_id, "lease_lost", owner=lease.owner, token=lease.token)
                return

    # -- loop --------------------------------------------------------------

    async def _drive(self, run_id: str, state: RunState, lease: Lease) -> RunResult:
        meter = BudgetMeter(self.budget, state.usage)
        llm = MeteredLLM(self.llm, meter)
        ledger = EvidenceLedger(state.evidence)
        executor = TaskExecutor(
            llm,
            self.search,
            ledger,
            self.store,
            meter,
            run_id,
            queries_per_task=self.queries_per_task,
            hits_per_query=self.hits_per_query,
            concurrency=self.concurrency,
        )
        s = _Session(run_id, state, lease, llm, meter, ledger, executor)

        try:
            while state.stage not in TERMINAL:
                before = state.stage
                await getattr(self, f"_{state.stage}")(s)
                self._checkpoint(s)
                if state.stage != before:
                    self.store.log(run_id, "stage", frm=before, to=state.stage)
        except BudgetExceeded as exc:
            # Halt without losing progress: the run can be resumed with a larger budget.
            state.error = str(exc)
            self._checkpoint(s)
            self.store.log(run_id, "run_halted", stage=state.stage, reason=str(exc))
        except LeaseLost:
            # Another worker owns the run now; stop without touching its state.
            self.store.log(run_id, "run_abandoned", owner=self.owner, token=lease.token)
            raise
        except BaseException as exc:
            # Leave the last checkpoint untouched; the in-memory state may be mid-stage.
            self.store.log(run_id, "run_interrupted", stage=state.stage, error=f"{type(exc).__name__}: {exc}")
            raise

        if state.stage in TERMINAL:
            self.store.log(run_id, "run_finished", status=state.stage, usage=meter.snapshot())
        return self._result(run_id, state, ledger)

    def _checkpoint(self, s: _Session) -> None:
        s.state.evidence = s.ledger.items()
        s.state.usage = s.meter.snapshot()
        self.store.checkpoint(s.run_id, s.state, status=self._status(s.state), lease=s.lease)

    @staticmethod
    def _status(state: RunState) -> str:
        return "halted" if state.error and state.stage not in TERMINAL else state.stage

    def _result(self, run_id: str, state: RunState, ledger: EvidenceLedger) -> RunResult:
        markdown = None
        if state.stage == "done" and state.report:
            notes = [i.detail for i in state.issues if i.code != "unknown_citation"]
            markdown = reporter.render_markdown(state.report, ledger, notes=notes)
        return RunResult(run_id, state, markdown)

    # -- stages ------------------------------------------------------------

    async def _plan(self, s: _Session) -> None:
        tasks = await planner.initial_plan(s.llm, s.state.question, max_tasks=self.max_tasks)
        s.state.plan = Plan(tasks)
        s.state.stage = "execute"
        self.store.log(s.run_id, "plan", tasks=[{"id": t.id, "q": t.question, "deps": t.depends_on} for t in tasks])

    async def _execute(self, s: _Session) -> None:
        """Run one wave per call so each finished wave is checkpointed."""
        waves = s.state.plan.waves()
        if waves:
            s.state.findings.update(await s.executor.run_wave(waves[0], s.state.findings))
            return
        if not any(f.evidence_ids for f in s.state.findings.values()):
            s.state.stage = "failed"
            s.state.error = "no evidence was found for any sub-question"
            return
        s.state.stage = "report"

    async def _report(self, s: _Session) -> None:
        problems = [i for i in s.state.issues if i.kind == "report"]
        s.state.report = await reporter.write_report(
            s.llm, s.state.question, s.state.plan, s.state.findings, s.ledger, problems=problems or None
        )
        s.state.stage = "verify"

    async def _verify(self, s: _Session) -> None:
        state = s.state
        assert state.report is not None
        verdict = verifier.check_report(
            state.report, state.plan, state.findings, s.ledger, addressed_gaps=state.gaps
        )
        if verdict.passed and self.critic and state.replans < self.budget.max_replans:
            markdown = reporter.render_markdown(state.report, s.ledger)
            verdict.gaps += [g for g in await verifier.critic_gaps(s.llm, state.question, markdown) if g not in state.gaps]
        state.issues = verdict.issues
        self.store.log(
            s.run_id,
            "verify",
            passed=verdict.passed,
            issues=[i.code for i in verdict.issues],
            gaps=verdict.gaps,
        )

        if verdict.report_issues and state.repairs < self.budget.max_repairs:
            state.repairs += 1
            state.stage = "report"
            return

        if verdict.gaps and state.replans < self.budget.max_replans:
            new_tasks = await planner.replan(
                s.llm, state.question, state.plan, state.findings, verdict.gaps, round_=state.replans + 1
            )
            state.replans += 1
            state.plan.tasks.extend(new_tasks)
            state.gaps += verdict.gaps
            state.issues = []
            state.stage = "execute"
            self.store.log(s.run_id, "replan", round=state.replans, tasks=[t.id for t in new_tasks])
            return

        # Out of repairs/replans (or nothing left to fix): finish, and surface what is
        # still wrong as limitations rather than pretending the report is flawless.
        state.report = reporter.strip_unknown_citations(state.report, s.ledger)
        state.stage = "done"
