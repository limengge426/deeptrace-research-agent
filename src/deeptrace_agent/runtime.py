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
import time
import uuid
from dataclasses import dataclass
from typing import Any

from . import faithfulness, planner, prompts, reporter, verifier
from .budget import Budget, BudgetExceeded, BudgetMeter, MeteredLLM
from .executor import TaskExecutor
from .ledger import EvidenceLedger
from .llm import LLM, LLMFormatError, parse_json_object
from .models import CITATION_RE, TERMINAL, Plan, PlanError, RunState, Task
from .search import SearchProvider
from .store import Lease, LeaseLost, RunStore
from .tools import FetchPageTool, Tool, ToolOutcomeUnknown, ToolRunner, WebhookTool

ROUTES = ("answer", "extend", "new")
HISTORY_TURNS = 3  # earlier turns shown to the router
HISTORY_ANSWER_CHARS = 700  # how much of each earlier answer the router sees


def answer_digest(state: RunState, max_chars: int = HISTORY_ANSWER_CHARS) -> str:
    """A short plain-text view of a finished run's answer, for the next turn's router."""
    if not state.report:
        return ""
    text = " ".join(CITATION_RE.sub("", s.body) for s in state.report.sections)
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


class RunLocked(RuntimeError):
    """Another live worker holds this run's lease."""


class InvalidAction(ValueError):
    """The requested control action does not apply to the run's current status."""


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


@dataclass
class RunResult:
    run_id: str
    state: RunState
    markdown: str | None

    @property
    def status(self) -> str:
        return self.state.status


@dataclass
class _Session:
    """Per-drive objects rebuilt from the checkpoint each time a run is (re)started."""

    run_id: str
    state: RunState
    lease: Lease
    llm: MeteredLLM
    judge: MeteredLLM
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
        check_faithfulness: bool = True,
        repair_partial: bool = False,
        judge_llm: LLM | None = None,
        report_mode: str = "sections",
        context_budget: reporter.ContextBudget | None = None,
        fetch: FetchPageTool | None = None,
        fetch_pages: int = 2,
        webhook: Tool | None = None,
        delivery_retries: int = 3,
        researcher: Any = None,
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
        self.check_faithfulness = check_faithfulness
        self.repair_partial = repair_partial  # also rewrite claims the judge finds only partially supported
        self.judge_llm = judge_llm  # defaults to the main model; a separate judge avoids self-grading
        if report_mode not in ("sections", "single"):
            raise ValueError("report_mode must be 'sections' or 'single'")
        self.report_mode = report_mode
        self.context_budget = context_budget or reporter.ContextBudget()
        self.fetch = fetch  # page fetching is used for web search hits only (http/https URLs)
        self.fetch_pages = fetch_pages
        self.webhook = webhook
        self.delivery_retries = delivery_retries
        self.researcher = researcher  # e.g. agent.AgentResearcher: LangGraph ReAct loop per sub-question

    # -- public API --------------------------------------------------------

    def create(self, question: str, *, approve_plan: bool = False, deliver_to: str | None = None) -> str:
        """Register a run without executing it; any worker can then claim it."""
        run_id = self.store.create_run(RunState(question=question, approve_plan=approve_plan, deliver_to=deliver_to))
        self.store.log(run_id, "run_created", question=question, approve_plan=approve_plan,
                       deliver=bool(deliver_to))
        return run_id

    async def start(self, question: str, *, approve_plan: bool = False, deliver_to: str | None = None) -> RunResult:
        return await self.resume(self.create(question, approve_plan=approve_plan, deliver_to=deliver_to))

    def create_followup(self, parent_id: str, question: str) -> str:
        """Register a follow-up question on a finished run, continuing its thread.

        The follow-up is a run of its own (leases, checkpoints and budgets apply as usual). It
        inherits the parent's plan, findings and evidence ledger, so it can answer from evidence
        already gathered and cite the same ids; its first stage decides whether more research
        is needed.
        """
        parent = self.store.load(parent_id)
        if parent.stage != "done":
            raise InvalidAction(f"only finished runs can be followed up; this one is {parent.status}")
        thread_id = parent.thread_id or parent_id
        busy = [r.id for r in self.store.thread(thread_id) if r.status not in TERMINAL]
        if busy:
            raise InvalidAction(f"the thread already has an unfinished turn ({busy[0]})")
        inherited = RunState.from_dict(parent.to_dict())  # deep copy
        history = parent.history + [{"question": parent.research_question, "answer": answer_digest(parent)}]
        state = RunState(
            question=question.strip(), stage="route", mode="followup",
            plan=inherited.plan, findings=inherited.findings, evidence=inherited.evidence,
            thread_id=thread_id, parent_id=parent_id, turn=parent.turn + 1,
            history=history[-HISTORY_TURNS:],
        )
        run_id = self.store.create_run(state)
        self.store.log(run_id, "run_created", question=state.question, followup=True, parent=parent_id,
                       turn=state.turn, inherited_evidence=len(state.evidence))
        return run_id

    async def followup(self, parent_id: str, question: str) -> RunResult:
        return await self.resume(self.create_followup(parent_id, question))

    # -- human control -----------------------------------------------------

    def cancel(self, run_id: str) -> str:
        return self._control(run_id, "cancel")

    def pause(self, run_id: str) -> str:
        return self._control(run_id, "pause")

    def _control(self, run_id: str, action: str) -> str:
        """Request a cancel/pause; apply it right away if no worker is driving the run.

        Returns the run's status afterwards ("cancelled"/"paused", or its current
        status if a worker will apply the request at its next step boundary).
        """
        state = self.store.load(run_id)
        if state.stage in TERMINAL or (action == "pause" and state.hold):
            raise InvalidAction(f"cannot {action} a run that is {state.status}")
        self.store.request_control(run_id, action)
        self.store.log(run_id, "control_requested", action=action)
        lease = self.store.acquire(run_id, self.owner, self.lease_ttl)
        if lease is None:
            return state.status  # the driving worker will pick the request up
        try:
            state = self.store.load(run_id)
            if self._apply_control(run_id, state):
                self.store.checkpoint(run_id, state, status=state.status, lease=lease)
            return state.status
        finally:
            self.store.release(lease)

    def _apply_control(self, run_id: str, state: RunState) -> bool:
        action = self.store.take_control(run_id)
        if action == "cancel" and state.stage not in TERMINAL:
            state.stage, state.error, state.hold = "cancelled", "cancelled by user", None
        elif action == "pause" and state.stage not in TERMINAL and not state.hold:
            state.hold = "paused"
        else:
            return False
        self.store.log(run_id, "control_applied", action=action, status=state.status)
        return True

    def unpause(self, run_id: str) -> str:
        """Release a paused or budget-halted run so a worker picks it up again."""
        state = self.store.load(run_id)
        if state.status not in ("paused", "halted"):
            raise InvalidAction(f"only paused or halted runs can be resumed; this one is {state.status}")
        self.store.requeue(run_id)
        self.store.log(run_id, "run_released", previous=state.status)
        return self.store.load(run_id).status

    def approve_plan(self, run_id: str, tasks: list[dict] | None = None) -> RunState:
        """Approve the proposed plan, optionally replacing it with an edited one."""
        state = self.store.load(run_id)
        if state.hold != "awaiting_approval":
            raise InvalidAction(f"run is {state.status}, not awaiting plan approval")
        if tasks is not None:
            try:
                plan = Plan([
                    Task(id=str(t["id"]), question=str(t["question"]).strip(),
                         depends_on=[str(d) for d in t.get("depends_on", [])])
                    for t in tasks
                ])
                if not plan.tasks:
                    raise PlanError("the plan needs at least one task")
                plan.validate()
            except (KeyError, TypeError, AttributeError, PlanError) as exc:
                raise InvalidAction(f"invalid plan: {exc}") from exc
            state.plan = plan
        state.hold = None
        self.store.checkpoint(run_id, state, status=state.status)
        self.store.log(run_id, "plan_approved", edited=tasks is not None,
                       tasks=[{"id": t.id, "q": t.question, "deps": t.depends_on} for t in state.plan.tasks])
        return state

    def resolve_tool_call(self, run_id: str, key: str, outcome: str) -> str:
        """Reconcile an interrupted side-effecting call a human has checked.

        ``outcome="done"``: it did happen; record it so it is never repeated.
        ``outcome="retry"``: it did not happen; the run will call it again.
        """
        state = self.store.load(run_id)
        pending = {c["key"]: c for c in self.store.pending_tool_calls(run_id)}
        if state.hold != "needs_reconciliation" or key not in pending:
            raise InvalidAction("no interrupted tool call with that key is waiting for reconciliation")
        if outcome == "done":
            self.store.finish_tool_call(key, {"resolved": "confirmed by a human"})
        elif outcome == "retry":
            self.store.discard_tool_call(key)
        else:
            raise InvalidAction('outcome must be "done" or "retry"')
        self.store.log(run_id, "tool_call_resolved", tool=pending[key]["tool"], key=key[:12], outcome=outcome)
        if not self.store.pending_tool_calls(run_id):
            state.hold, state.error = None, None
            self.store.checkpoint(run_id, state, status=state.status)
        return self.store.load(run_id).status

    # -- driving -------------------------------------------------------------

    async def resume(self, run_id: str) -> RunResult:
        """Drive a run to completion (or a halt), holding its lease throughout.

        Runs on hold (paused, awaiting plan approval) are returned untouched;
        release them with ``unpause`` / ``approve_plan`` first.
        """
        state = self.store.load(run_id)
        if state.stage in TERMINAL or state.hold:
            return self._result(run_id, state, EvidenceLedger(state.evidence))
        lease = self.store.acquire(run_id, self.owner, self.lease_ttl)
        if lease is None:
            holder = self.store.lease_holder(run_id)
            detail = f" by {holder[0]} (lease expires in {holder[1]:.0f}s)" if holder else ""
            raise RunLocked(f"run {run_id} is being driven{detail}")
        state = self.store.load(run_id)  # re-read: the previous owner may have progressed
        if state.stage in TERMINAL or state.hold:
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
        meter = BudgetMeter(self.budget, state.usage, state.metrics)
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
            fetch=self.fetch,
            fetch_pages=self.fetch_pages,
            researcher=self.researcher,
        )
        judge = MeteredLLM(self.judge_llm, meter) if self.judge_llm else llm
        s = _Session(run_id, state, lease, llm, judge, meter, ledger, executor)

        try:
            while state.stage not in TERMINAL and not state.hold:
                if self._apply_control(run_id, state):  # cancel/pause requested since the last step
                    self._checkpoint(s)
                    continue
                before = state.stage
                started = time.monotonic()
                await getattr(self, f"_{state.stage}")(s)
                meter.metrics.record_stage(before, time.monotonic() - started)
                self._checkpoint(s)
                if state.stage != before:
                    self.store.log(run_id, "stage", frm=before, to=state.stage)
        except BudgetExceeded as exc:
            # Halt without losing progress: the run can be resumed with a larger budget.
            state.error = str(exc)
            self._checkpoint(s)
            self.store.log(run_id, "run_halted", stage=state.stage, reason=str(exc))
        except ToolOutcomeUnknown as exc:
            # A side-effecting call may have happened; retrying could repeat it. Ask a human.
            state.hold, state.error = "needs_reconciliation", str(exc)
            self._checkpoint(s)
            self.store.log(run_id, "run_held", status=state.hold, tool=exc.tool, key=exc.key)
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
        elif state.hold:
            self.store.log(run_id, "run_held", status=state.hold, stage=state.stage)
        return self._result(run_id, state, ledger)

    def _checkpoint(self, s: _Session) -> None:
        s.state.evidence = s.ledger.items()
        s.state.usage = s.meter.snapshot()
        s.state.metrics = s.meter.metrics.to_dict()
        self.store.checkpoint(s.run_id, s.state, status=s.state.status, lease=s.lease)

    def _result(self, run_id: str, state: RunState, ledger: EvidenceLedger) -> RunResult:
        markdown = self._markdown(state, ledger) if state.stage == "done" else None
        return RunResult(run_id, state, markdown)

    @staticmethod
    def _markdown(state: RunState, ledger: EvidenceLedger) -> str | None:
        if not state.report:
            return None
        # Unknown citations are stripped and partial claims are minor: neither goes to Limitations.
        notes = [i.note or i.detail for i in state.issues if i.code not in ("unknown_citation", "partial_claim")]
        return reporter.render_markdown(state.report, ledger, notes=notes)

    # -- stages ------------------------------------------------------------

    async def _plan(self, s: _Session) -> None:
        tasks = await planner.initial_plan(s.llm, s.state.research_question, max_tasks=self.max_tasks)
        s.state.plan = Plan(tasks)
        s.state.stage = "execute"
        self.store.log(s.run_id, "plan", tasks=[{"id": t.id, "q": t.question, "deps": t.depends_on} for t in tasks])
        if s.state.approve_plan:
            s.state.hold = "awaiting_approval"

    async def _route(self, s: _Session) -> None:
        """First stage of a follow-up: rewrite it as a standalone question and decide whether the
        inherited research answers it, needs extending, or it is a new topic altogether."""
        state = s.state
        usable = [tid for tid, f in state.findings.items() if f.evidence_ids]
        done = "\n".join(
            f"- {tid}: {state.plan.get(tid).question}\n  {state.findings[tid].summary[:300]}" for tid in usable
        )
        history = "\n".join(f"Q: {h['question']}\nA: {h['answer']}" for h in state.history)
        user = prompts.ROUTE_USER.format(history=history or "(none)", done=done or "(none)", message=state.question)
        route = {"decision": "answer", "question": state.question, "tasks": usable, "missing": [],
                 "reason": "the router's reply was unusable; answering from the existing research"}
        try:
            reply = await s.llm.complete(prompts.ROUTE_SYSTEM, user, purpose="route", json_mode=True)
            data = parse_json_object(reply.text)
            decision = str(data.get("decision", "")).strip().lower()
            route = {
                "decision": decision if decision in ROUTES else "answer",
                "question": str(data.get("question") or "").strip() or state.question,
                "tasks": [t for t in dict.fromkeys(str(t) for t in data.get("tasks") or []) if t in usable],
                "missing": [str(m).strip() for m in data.get("missing") or [] if str(m).strip()][:3],
                "reason": str(data.get("reason") or "").strip()[:300],
            }
        except LLMFormatError:
            pass
        if route["decision"] == "extend" and not route["missing"]:
            route["missing"] = [route["question"]]
        state.route = route
        self.store.log(s.run_id, "route", **route)

        if route["decision"] == "new":
            # Unrelated to the thread so far: research it from scratch as a full report.
            state.plan, state.findings, state.focus = Plan(), {}, []
            s.ledger.clear()
            state.mode, state.stage = "report", "plan"
            return
        state.focus = route["tasks"] or usable
        if route["decision"] == "extend":
            await self._research_more(s, route["missing"], reason="route")
        else:
            state.stage = "report"

    async def _research_more(self, s: _Session, gaps: list[str], *, reason: str) -> None:
        """Plan and queue new tasks for ``gaps`` (evidence gaps, or what a follow-up needs)."""
        state = s.state
        n = state.replans + 1
        prefix = f"f{state.turn}r{n}_" if state.mode == "followup" else f"r{n}_"
        new_tasks = await planner.replan(
            s.llm, state.research_question, state.plan, state.findings, gaps, round_=n, prefix=prefix
        )
        state.replans = n
        state.plan.tasks.extend(new_tasks)
        state.gaps += gaps
        state.issues = []
        if state.mode == "followup":
            state.focus += [t.id for t in new_tasks]
        state.stage = "execute"
        self.store.log(s.run_id, "replan", round=n, reason=reason,
                       tasks=[{"id": t.id, "q": t.question, "deps": t.depends_on} for t in new_tasks])

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
        state = s.state
        problems = [i for i in state.issues if i.kind == "report"]
        if state.mode == "followup":
            await self._answer(s, problems)
            return
        if self.report_mode == "single":
            state.report = await reporter.write_report(
                s.llm, state.research_question, state.plan, state.findings, s.ledger, problems=problems or None
            )
        elif problems and state.report and state.outline:
            state.report, rewritten = await reporter.repair_sections(
                s.llm, state.research_question, state.report, state.outline, state.plan, state.findings, s.ledger,
                problems, budget=self.context_budget,
            )
            self.store.log(s.run_id, "report_repaired", sections=rewritten,
                           kept=[x.heading for x in state.report.sections if x.heading not in rewritten])
        else:
            state.report, state.outline = await reporter.write_sectioned_report(
                s.llm, state.research_question, state.plan, state.findings, s.ledger, budget=self.context_budget
            )
            self.store.log(s.run_id, "outline", sections=[
                {"heading": x["heading"], "tasks": x["tasks"], "synthesis": x["synthesis"]} for x in state.outline
            ])
        state.stage = "verify"

    async def _answer(self, s: _Session, problems: list) -> None:
        """Write (or repair) a follow-up's short answer from the evidence of its focus tasks."""
        state = s.state
        previous = state.report.sections[0].body if problems and state.report else None
        state.report, missing = await reporter.write_answer(
            s.llm, state.research_question, state.focus, state.plan, state.findings, s.ledger,
            budget=self.context_budget, previous=previous, problems=[p.detail for p in problems] or None,
        )
        state.outline = [{"heading": reporter.ANSWER_HEADING, "tasks": list(state.focus), "synthesis": False}]
        missing = [m for m in missing if m not in state.gaps]
        if missing and not problems and state.replans < self.budget.max_replans:
            # The router thought the evidence was enough, but the writer could not answer from it.
            self.store.log(s.run_id, "answer_incomplete", missing=missing)
            await self._research_more(s, missing, reason="answer")
            return
        state.stage = "verify"

    async def _verify(self, s: _Session) -> None:
        state = s.state
        assert state.report is not None
        plan, findings = state.plan, state.findings
        if state.mode == "followup":
            # An answer need not cite every inherited finding, only what this turn researched.
            own = [t for t in state.plan.tasks if t.id.startswith(f"f{state.turn}r")]
            plan, findings = Plan(own), {t.id: state.findings[t.id] for t in own if t.id in state.findings}
        verdict = verifier.check_report(
            state.report, plan, findings, s.ledger, addressed_gaps=state.gaps, outline=state.outline
        )
        if self.check_faithfulness and not verdict.report_issues:
            # Only judge drafts that already pass the structural checks; others get rewritten anyway.
            judged = await faithfulness.check_faithfulness(s.judge, state.report, s.ledger)
            state.faithfulness.append(judged.summary())
            state.verdicts = [{"section": v.claim.section, "text": v.claim.text, "citations": v.claim.citations,
                               "label": v.label, "reason": v.reason} for v in judged.verdicts]
            verdict.issues += judged.issues(include_partial=self.repair_partial)
            self.store.log(s.run_id, "faithfulness", **judged.summary())
        if verdict.passed and self.critic and state.mode == "report" and state.replans < self.budget.max_replans:
            markdown = reporter.render_markdown(state.report, s.ledger)
            verdict.gaps += [g for g in await verifier.critic_gaps(s.llm, state.research_question, markdown)
                             if g not in state.gaps]
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
            await self._research_more(s, verdict.gaps, reason="verify")
            return

        # Out of repairs/replans (or nothing left to fix): finish, and surface what is
        # still wrong as limitations rather than pretending the report is flawless.
        state.report = reporter.strip_unknown_citations(state.report, s.ledger)
        state.stage = "deliver" if state.deliver_to else "done"

    async def _deliver(self, s: _Session) -> None:
        """POST the finished report to the run's webhook (idempotency-keyed), then finish.

        A delivery that keeps failing is recorded on the run; the report itself is kept.
        """
        state = s.state
        payload = {
            "run_id": s.run_id,
            "question": state.question,
            "title": state.report.title if state.report else state.question,
            "markdown": self._markdown(state, s.ledger),
            "sources": [{"id": e.id, "title": e.title, "url": e.url} for e in s.ledger.items()
                        if state.report and e.id in state.report.citations()],
        }
        tool = self.webhook or WebhookTool()
        runner = ToolRunner(self.store, s.meter, s.run_id)
        error = None
        for attempt in range(self.delivery_retries):
            try:
                await runner.call(tool, {"url": state.deliver_to, "payload": payload})
                error = None
                break
            except (ToolOutcomeUnknown, BudgetExceeded):
                raise
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                self.store.log(s.run_id, "delivery_failed", attempt=attempt + 1, error=error)
                await asyncio.sleep(min(2**attempt, 10) * 0.5)
        state.delivery = {"status": "failed", "error": error} if error else {"status": "delivered"}
        self.store.log(s.run_id, "delivery", **state.delivery)
        state.stage = "done"
