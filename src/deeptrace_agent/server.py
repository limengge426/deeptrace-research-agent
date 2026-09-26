"""HTTP API (FastAPI): submit runs, follow them live over SSE, fetch reports.

The API only writes runs to the database; workers (embedded in this process by
default, or separate ``deeptrace worker`` processes) claim and execute them.

    uvicorn deeptrace_agent.server:app_from_env --factory
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import metrics as run_metrics
from .env import getenv
from .models import TERMINAL
from .runtime import InvalidAction, ResearchRuntime
from .views import run_state
from .worker import Worker

WEB_DIST = Path(__file__).parent / "web_dist"  # built web console (see web/), served at /


class RunRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    approve_plan: bool = Field(False, description="pause after planning until the plan is approved")
    webhook_url: str | None = Field(None, pattern=r"^https?://", description="receives the finished report")


class PlanTask(BaseModel):
    id: str
    question: str = Field(min_length=3)
    depends_on: list[str] = []


class Resolution(BaseModel):
    outcome: str = Field(pattern="^(done|retry)$", description="did the interrupted call happen?")


class PlanApproval(BaseModel):
    tasks: list[PlanTask] | None = Field(None, description="an edited plan; omit to approve as proposed")


class TaskView(BaseModel):
    id: str
    question: str
    status: str
    depends_on: list[str]
    round: int


class RunView(BaseModel):
    id: str
    question: str
    status: str
    error: str | None
    tasks: list[TaskView]
    evidence: int
    replans: int
    repairs: int
    usage: dict[str, float]
    delivery: dict | None = None


def create_app(runtime: ResearchRuntime, *, embedded_worker: bool = True, poll_interval: float = 1.0) -> FastAPI:
    store = runtime.store

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = None
        if embedded_worker:
            task = asyncio.create_task(Worker(runtime, poll_interval=poll_interval).run(stop))
        yield
        stop.set()
        if task:
            await task

    app = FastAPI(title="DeepTrace", version="0.3.0", lifespan=lifespan)

    def view(run_id: str) -> RunView:
        try:
            status = store.status(run_id)
            state = store.load(run_id)
        except KeyError:
            raise HTTPException(404, f"no run {run_id}") from None
        return RunView(
            id=run_id,
            question=state.question,
            status=status,
            error=state.error,
            tasks=[TaskView(id=t.id, question=t.question, status=t.status, depends_on=t.depends_on, round=t.round)
                   for t in state.plan.tasks],
            evidence=len(state.evidence),
            replans=state.replans,
            repairs=state.repairs,
            usage=state.usage,
            delivery=state.delivery,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "owner": runtime.owner}

    @app.post("/runs", status_code=202)
    async def submit(req: RunRequest) -> RunView:
        return view(runtime.create(req.question, approve_plan=req.approve_plan, deliver_to=req.webhook_url))

    def act(run_id: str, action) -> RunView:
        view(run_id)  # 404 for unknown runs
        try:
            action()
        except InvalidAction as exc:
            raise HTTPException(409, str(exc)) from None
        return view(run_id)

    @app.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel(run_id: str) -> RunView:
        """Cancel now if idle, otherwise at the running worker's next step boundary."""
        return act(run_id, lambda: runtime.cancel(run_id))

    @app.post("/runs/{run_id}/pause", status_code=202)
    async def pause(run_id: str) -> RunView:
        return act(run_id, lambda: runtime.pause(run_id))

    @app.post("/runs/{run_id}/plan/approve", status_code=202)
    async def approve(run_id: str, body: PlanApproval | None = None) -> RunView:
        tasks = [t.model_dump() for t in body.tasks] if body and body.tasks is not None else None
        return act(run_id, lambda: runtime.approve_plan(run_id, tasks))

    @app.get("/runs")
    async def list_runs(limit: int = Query(20, ge=1, le=100)) -> list[dict]:
        return [vars(r) for r in store.runs(limit)]

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> RunView:
        return view(run_id)

    @app.get("/runs/{run_id}/metrics")
    async def get_run_metrics(run_id: str) -> dict:
        view(run_id)
        return run_metrics.run_metrics(store.load(run_id))

    @app.get("/metrics")
    async def get_metrics(format: str = Query("json", pattern="^(json|prometheus)$"), limit: int = 1000):
        """Aggregate metrics over recent runs; ?format=prometheus for scraping."""
        summary = run_metrics.summarize(store.states(limit))
        if format == "prometheus":
            return PlainTextResponse(run_metrics.to_prometheus(summary), media_type="text/plain; version=0.0.4")
        return summary

    @app.get("/runs/{run_id}/report", response_class=PlainTextResponse)
    async def get_report(run_id: str) -> str:
        current = view(run_id)
        result = runtime.result(run_id)
        if result.markdown is None:
            raise HTTPException(409, f"run is {current.status}; no report yet")
        return result.markdown

    @app.get("/runs/{run_id}/tool-calls/pending")
    async def pending_tool_calls(run_id: str) -> list[dict]:
        """Tool calls whose outcome is unknown because the process died mid-call."""
        view(run_id)
        return store.pending_tool_calls(run_id)

    @app.post("/runs/{run_id}/tool-calls/{key}/resolve", status_code=202)
    async def resolve_tool_call(run_id: str, key: str, body: Resolution) -> RunView:
        return act(run_id, lambda: runtime.resolve_tool_call(run_id, key, body.outcome))

    @app.post("/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str) -> RunView:
        """Release a paused or budget-halted run. Crashed runs are picked up automatically."""
        return act(run_id, lambda: runtime.unpause(run_id))

    @app.get("/runs/{run_id}/events")
    async def events(
        run_id: str,
        after: int = Query(0, ge=0),
        last_event_id: int | None = Header(None),
    ) -> StreamingResponse:
        """Server-sent events; reconnecting clients resume from Last-Event-ID."""
        view(run_id)
        cursor = last_event_id if last_event_id is not None else after

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            while True:
                for ev in store.events(run_id, after=cursor):
                    cursor = ev.id
                    payload = json.dumps({"ts": ev.ts, **ev.payload}, ensure_ascii=False)
                    yield f"id: {ev.id}\nevent: {ev.kind}\ndata: {payload}\n\n"
                status = store.status(run_id)
                if status in TERMINAL or status in ("halted", "paused", "awaiting_approval", "needs_reconciliation"):
                    if not store.events(run_id, after=cursor):
                        yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
                        return
                    continue
                await asyncio.sleep(poll_interval / 2)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/runs/{run_id}/state")
    async def get_state(run_id: str) -> dict:
        """Everything the web console shows for one run."""
        view(run_id)  # 404 for unknown runs
        return run_state(store, run_id)

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def web_missing() -> str:
            return ("<h1>DeepTrace API</h1><p>The web console is not built. Run <code>cd web && npm install && "
                    "npm run build</code>, or use the <a href='/docs'>API docs</a>.</p>")

    return app


def app_from_env() -> FastAPI:
    """App factory configured from environment variables (used by Docker)."""
    from .budget import Budget
    from .llm import OpenAICompatLLM
    from .search import LocalCorpusSearch, TavilySearch
    from .store import RunStore

    from .tools import FetchPageTool

    corpus = getenv("CORPUS")
    runtime = ResearchRuntime(
        OpenAICompatLLM.from_env(),
        LocalCorpusSearch(corpus) if corpus else TavilySearch(),
        RunStore(getenv("DB", "deeptrace.db")),
        budget=Budget(max_tokens=int(getenv("MAX_TOKENS", "250000"))),
        fetch=None if corpus else FetchPageTool(),
    )
    embedded = getenv("EMBEDDED_WORKER", "1") != "0"
    return create_app(runtime, embedded_worker=embedded)
