"""HTTP API (FastAPI): submit runs, follow them live over SSE, fetch reports.

The API only writes runs to the database; workers (embedded in this process by
default, or separate ``researchloop worker`` processes) claim and execute them.

    uvicorn researchloop.server:app_from_env --factory
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .runtime import TERMINAL, ResearchRuntime
from .worker import Worker


class RunRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


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

    app = FastAPI(title="researchloop", version="0.2.0", lifespan=lifespan)

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
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "owner": runtime.owner}

    @app.post("/runs", status_code=202)
    async def submit(req: RunRequest) -> RunView:
        return view(runtime.create(req.question))

    @app.get("/runs")
    async def list_runs(limit: int = Query(20, ge=1, le=100)) -> list[dict]:
        return [vars(r) for r in store.runs(limit)]

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> RunView:
        return view(run_id)

    @app.get("/runs/{run_id}/report", response_class=PlainTextResponse)
    async def get_report(run_id: str) -> str:
        current = view(run_id)
        result = runtime.result(run_id)
        if result.markdown is None:
            raise HTTPException(409, f"run is {current.status}; no report yet")
        return result.markdown

    @app.post("/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str) -> RunView:
        if view(run_id).status != "halted":
            raise HTTPException(409, "only halted runs can be resumed; interrupted runs are picked up automatically")
        store.requeue(run_id)
        return view(run_id)

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
                if status in TERMINAL or status == "halted":
                    if not store.events(run_id, after=cursor):
                        yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
                        return
                    continue
                await asyncio.sleep(poll_interval / 2)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


def app_from_env() -> FastAPI:
    """App factory configured from environment variables (used by Docker)."""
    from .budget import Budget
    from .llm import OpenAICompatLLM
    from .search import LocalCorpusSearch, TavilySearch
    from .store import RunStore

    corpus = os.getenv("RESEARCHLOOP_CORPUS")
    runtime = ResearchRuntime(
        OpenAICompatLLM.from_env(),
        LocalCorpusSearch(corpus) if corpus else TavilySearch(),
        RunStore(os.getenv("RESEARCHLOOP_DB", "researchloop.db")),
        budget=Budget(max_tokens=int(os.getenv("RESEARCHLOOP_MAX_TOKENS", "250000"))),
    )
    embedded = os.getenv("RESEARCHLOOP_EMBEDDED_WORKER", "1") != "0"
    return create_app(runtime, embedded_worker=embedded)
