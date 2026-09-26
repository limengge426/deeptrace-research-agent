"""Executor: researches one task at a time, and whole DAG waves concurrently."""

from __future__ import annotations

import asyncio

from . import prompts
from .budget import BudgetExceeded, BudgetMeter
from .ledger import EvidenceLedger
from .llm import LLM, LLMFormatError, parse_json_object
from .models import CITATION_RE, Finding, Task
from .search import SearchHit, SearchProvider
from .store import RunStore
from .tools import FetchPageTool, SearchTool, ToolOutcomeUnknown, ToolRunner, best_passages

# Errors that must stop the whole wave instead of failing one task.
_RUN_LEVEL = (BudgetExceeded, ToolOutcomeUnknown)


class TaskExecutor:
    def __init__(
        self,
        llm: LLM,
        search: SearchProvider,
        ledger: EvidenceLedger,
        store: RunStore,
        meter: BudgetMeter,
        run_id: str,
        *,
        queries_per_task: int = 2,
        hits_per_query: int = 4,
        concurrency: int = 3,
        fetch: FetchPageTool | None = None,
        fetch_pages: int = 0,
    ) -> None:
        self.llm = llm
        self.search = search
        self.search_tool = SearchTool(search)
        self.fetch = fetch
        self.fetch_pages = fetch_pages if fetch else 0
        self.tools = ToolRunner(store, meter, run_id)
        self.ledger = ledger
        self.store = store
        self.meter = meter
        self.run_id = run_id
        self.queries_per_task = queries_per_task
        self.hits_per_query = hits_per_query
        self._slots = asyncio.Semaphore(concurrency)

    async def run_wave(self, wave: list[Task], findings: dict[str, Finding]) -> dict[str, Finding]:
        """Run independent tasks concurrently.

        A failing task is marked ``failed`` without cancelling its siblings; the
        verifier later turns it into an evidence gap. Budget exhaustion is the
        exception: it aborts the whole wave so the runtime can stop cleanly.
        """
        results = await asyncio.gather(*(self._guarded(t, findings) for t in wave), return_exceptions=True)
        for result in results:
            # Raise before touching task state so the last checkpoint stays consistent.
            if isinstance(result, _RUN_LEVEL):
                raise result
        out: dict[str, Finding] = {}
        for task, result in zip(wave, results):
            if isinstance(result, BaseException):
                task.status = "failed"
                self.store.log(self.run_id, "task_failed", task=task.id, error=f"{type(result).__name__}: {result}")
                continue
            task.status = "done"
            out[task.id] = result
            self.store.log(self.run_id, "task_done", task=task.id, evidence=result.evidence_ids)
        return out

    async def _guarded(self, task: Task, findings: dict[str, Finding]) -> Finding:
        async with self._slots:
            self.store.log(self.run_id, "task_started", task=task.id, question=task.question)
            return await self.run_task(task, findings)

    async def run_task(self, task: Task, findings: dict[str, Finding]) -> Finding:
        upstream = [findings[d] for d in task.depends_on if d in findings]
        queries = await self._queries(task, upstream)

        evidence_ids: list[str] = []
        for query in queries:
            hits = await self._search(query)
            if self.fetch_pages:
                hits = await self._enrich(hits, f"{query} {task.question}")
            for hit in hits:
                ev = self.ledger.add(hit, task_id=task.id, query=query)
                if ev.id not in evidence_ids:
                    evidence_ids.append(ev.id)

        if not evidence_ids:
            return Finding(task.id, "No sources were found for this sub-question.", [])
        return await self._summarize(task, evidence_ids)

    async def _queries(self, task: Task, upstream: list[Finding]) -> list[str]:
        user = f"Sub-question: {task.question}"
        if upstream:
            user += "\n\nFindings from prerequisite tasks:\n" + "\n".join(f"- {f.summary}" for f in upstream)
        reply = await self.llm.complete(
            prompts.QUERY_SYSTEM.format(n=self.queries_per_task), user, purpose="queries", json_mode=True
        )
        try:
            raw = parse_json_object(reply.text).get("queries") or []
        except LLMFormatError:
            raw = []
        queries = list(dict.fromkeys(q.strip() for q in raw if isinstance(q, str) and q.strip()))
        # Fall back to the sub-question itself rather than failing the task.
        return queries[: self.queries_per_task] or [task.question]

    async def _search(self, query: str) -> list[SearchHit]:
        """Search through the tool runner: a resumed run replays results instead of re-querying."""
        raw = await self.tools.call(self.search_tool, {"query": query, "k": self.hits_per_query})
        return [SearchHit(**h) for h in raw]

    async def _enrich(self, hits: list[SearchHit], focus: str) -> list[SearchHit]:
        """Replace thin search snippets of the top web hits with the most relevant passages of the page."""
        out = list(hits)
        for i, hit in enumerate(hits[: self.fetch_pages]):
            if not hit.url.startswith(("http://", "https://")):
                continue
            try:
                page = await self.tools.call(self.fetch, {"url": hit.url})
            except _RUN_LEVEL:
                raise
            except Exception as exc:  # noqa: BLE001 - an unreachable page keeps its snippet
                self.store.log(self.run_id, "fetch_failed", url=hit.url, error=f"{type(exc).__name__}: {exc}")
                continue
            passages = best_passages(page["text"], focus)
            if passages:
                out[i] = SearchHit(hit.url, hit.title or page["title"], f"{hit.content}\n\n{passages}".strip())
        return out

    async def _summarize(self, task: Task, evidence_ids: list[str]) -> Finding:
        user = f"Sub-question: {task.question}\n\nEvidence:\n{self.ledger.cards(evidence_ids, max_chars=1200)}"
        reply = await self.llm.complete(prompts.FINDING_SYSTEM, user, purpose="finding", json_mode=True)
        data = parse_json_object(reply.text)
        summary = str(data.get("summary", "")).strip()
        if not summary:
            raise LLMFormatError("finding has an empty summary")
        # Keep only ids retrieved for this task; anything else is a hallucinated citation.
        claimed = [str(e) for e in data.get("used") or []] + CITATION_RE.findall(summary)
        used = [e for e in dict.fromkeys(claimed) if e in evidence_ids]
        return Finding(task.id, summary, used or evidence_ids)
