"""Command-line interface: ``deeptrace run | resume | show | list | worker | serve``."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from .budget import Budget
from .env import getenv
from .llm import LLMError, OpenAICompatLLM
from .runtime import InvalidAction, ResearchRuntime, RunLocked, RunResult
from .search import LocalCorpusSearch, SearchProvider, TavilySearch
from .store import RunStore


def _search_from_args(args: argparse.Namespace) -> SearchProvider:
    corpus = args.corpus or (None if args.web else getenv("CORPUS"))
    if corpus:
        return LocalCorpusSearch(corpus)
    return TavilySearch()


def _budget_from_args(args: argparse.Namespace) -> Budget:
    return Budget(
        max_tokens=args.max_tokens,
        max_tool_calls=args.max_tool_calls,
        max_seconds=args.max_minutes * 60,
        max_replans=args.max_replans,
    )


def _report(result: RunResult, out_dir: Path) -> int:
    state = result.state
    usage = state.usage
    print(
        f"\nrun {result.run_id}: {result.status} · {len(state.plan.tasks)} tasks · "
        f"{len(state.evidence)} evidence · {usage.get('tokens', 0):,.0f} tokens · "
        f"{usage.get('tool_calls', 0):.0f} tool calls · replans {state.replans} · repairs {state.repairs}"
    )
    if result.markdown:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{result.run_id}.md"
        path.write_text(result.markdown, encoding="utf-8")
        print(f"report: {path}")
        return 0
    if result.status == "awaiting_approval":
        print("proposed plan:")
        for t in state.plan.tasks:
            deps = f"  (after {', '.join(t.depends_on)})" if t.depends_on else ""
            print(f"  {t.id}: {t.question}{deps}")
        print(f"approve with: deeptrace approve {result.run_id} [--plan edited.json], then resume")
        return 0
    if state.error:
        print(f"stopped: {state.error}")
    if result.status == "halted":
        print(f"continue with: deeptrace resume {result.run_id} --max-tokens <larger>")
    if result.status == "paused":
        print(f"continue with: deeptrace resume {result.run_id}")
    return 1


def _show(store: RunStore, run_id: str) -> int:
    events = store.events(run_id)
    if not events:
        print(f"no events for run {run_id}", file=sys.stderr)
        return 1
    t0 = events[0].ts
    for ev in events:
        detail = " ".join(f"{k}={v}" for k, v in ev.payload.items())
        print(f"{ev.ts - t0:7.1f}s  {ev.kind:<16} {detail[:160]}")

    from .metrics import run_metrics

    m = run_metrics(store.load(run_id))
    print("\nLLM calls by purpose        calls   tokens   seconds")
    for purpose, v in sorted(m["llm"].items(), key=lambda kv: -kv[1]["tokens"]):
        print(f"  {purpose:<24} {v['calls']:>6.0f} {v['tokens']:>8,.0f} {v['seconds']:>9.1f}")
    print("time by stage               steps  seconds")
    for stage, v in m["stages"].items():
        print(f"  {stage:<24} {v['steps']:>6.0f} {v['seconds']:>8.1f}")
    for tool, v in m["tools"].items():
        print(f"tool {tool}: {v['calls']:.0f} calls, {v['cache_hits']:.0f} cache hits, {v['errors']:.0f} errors")
    return 0


def _list(store: RunStore) -> int:
    for run in store.runs():
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.created_at))
        print(f"{run.id}  {run.status:<8} {when}  {run.question[:70]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # --db is accepted both before and after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db", default=argparse.SUPPRESS, help="SQLite file for runs (default: $DEEPTRACE_DB or deeptrace.db)"
    )
    parser = argparse.ArgumentParser(prog="deeptrace", description="DeepTrace: a fault-tolerant deep research agent.", parents=[common])
    parser.set_defaults(db=getenv("DB", "deeptrace.db"))
    sub = parser.add_subparsers(dest="command", required=True)

    def add_run_options(p: argparse.ArgumentParser) -> None:
        src = p.add_mutually_exclusive_group()
        src.add_argument("--corpus", help="search a local folder of .md/.txt files instead of the web")
        src.add_argument("--web", action="store_true", help="search the web with Tavily (default)")
        p.add_argument("--out", default="reports", help="where to write the Markdown report")
        p.add_argument("--max-tokens", type=int, default=250_000)
        p.add_argument("--max-tool-calls", type=int, default=60)
        p.add_argument("--max-minutes", type=float, default=30)
        p.add_argument("--max-replans", type=int, default=2)
        p.add_argument("--max-tasks", type=int, default=5)
        p.add_argument("--concurrency", type=int, default=3)
        p.add_argument("--critic", action="store_true", help="add an LLM review for coverage gaps")
        p.add_argument("--lease-ttl", type=float, default=30.0, help="seconds before a dead worker's run is taken over")
        p.add_argument("--fetch-pages", type=int, default=2, help="web hits per query to fetch in full (web search)")

    run = sub.add_parser("run", help="start a new research run", parents=[common])
    run.add_argument("question")
    run.add_argument("--approve-plan", action="store_true", help="stop after planning for human approval")
    run.add_argument("--webhook", help="POST the finished report to this URL (with an Idempotency-Key header)")
    add_run_options(run)

    resume = sub.add_parser("resume", help="continue an interrupted or halted run", parents=[common])
    resume.add_argument("run_id")
    add_run_options(resume)

    show = sub.add_parser("show", help="print a run's event timeline", parents=[common])
    show.add_argument("run_id")

    sub.add_parser("list", help="list recent runs", parents=[common])

    for name, text in (("cancel", "cancel a run"), ("pause", "pause a run at its next step boundary")):
        p = sub.add_parser(name, help=text, parents=[common])
        p.add_argument("run_id")

    resolve = sub.add_parser("resolve", help="reconcile an interrupted side-effecting tool call", parents=[common])
    resolve.add_argument("run_id")
    resolve.add_argument("key", nargs="?", help="tool call key (omit to list pending calls)")
    resolve.add_argument("--outcome", choices=("done", "retry"), help="did the interrupted call happen?")

    approve = sub.add_parser("approve", help="approve a run's proposed plan", parents=[common])
    approve.add_argument("run_id")
    approve.add_argument("--plan", help='JSON file with an edited plan: [{"id", "question", "depends_on"}]')

    worker = sub.add_parser("worker", help="claim and execute queued runs until stopped", parents=[common])
    add_run_options(worker)
    worker.add_argument("--max-active", type=int, default=2, help="runs driven concurrently by this worker")

    serve = sub.add_parser("serve", help="start the HTTP API (FastAPI + uvicorn)", parents=[common])
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-worker", action="store_true", help="API only; run `deeptrace worker` separately")
    add_run_options(serve)
    return parser


def _runtime(args: argparse.Namespace, store: RunStore) -> ResearchRuntime:
    from .tools import FetchPageTool

    search = _search_from_args(args)
    return ResearchRuntime(
        OpenAICompatLLM.from_env(),
        search,
        store,
        fetch=FetchPageTool() if isinstance(search, TavilySearch) else None,
        fetch_pages=args.fetch_pages,
        budget=_budget_from_args(args),
        max_tasks=args.max_tasks,
        concurrency=args.concurrency,
        critic=args.critic,
        lease_ttl=args.lease_ttl,
    )


async def _close(runtime: ResearchRuntime) -> None:
    await runtime.llm.aclose()
    if isinstance(runtime.search, TavilySearch):
        await runtime.search.aclose()
    if runtime.fetch is not None:
        await runtime.fetch.aclose()


async def _resume_when_free(runtime: ResearchRuntime, run_id: str) -> RunResult:
    """Resume, waiting out the lease of a crashed process that still holds the run."""
    while True:
        try:
            return await runtime.resume(run_id)
        except RunLocked as exc:
            holder = runtime.store.lease_holder(run_id)
            if holder is None:
                continue
            print(f"{exc}; waiting…", file=sys.stderr)
            await asyncio.sleep(min(holder[1] + 0.5, runtime.lease_ttl))


async def _run(args: argparse.Namespace, store: RunStore) -> int:
    runtime = _runtime(args, store)
    try:
        if args.command == "run":
            result = await runtime.start(args.question, approve_plan=args.approve_plan, deliver_to=args.webhook)
        else:
            if store.load(args.run_id).status in ("paused", "halted"):
                runtime.unpause(args.run_id)
            result = await _resume_when_free(runtime, args.run_id)
    finally:
        await _close(runtime)
    return _report(result, Path(args.out))


async def _work(args: argparse.Namespace, store: RunStore) -> int:
    from .worker import Worker

    runtime = _runtime(args, store)
    print(f"worker {runtime.owner} polling {args.db}", file=sys.stderr)
    try:
        await Worker(runtime, max_active=args.max_active).run()
    finally:
        await _close(runtime)
    return 0


def _control(args: argparse.Namespace, store: RunStore) -> int:
    import json

    # Control actions touch only the store; no model or search provider is needed.
    runtime = ResearchRuntime(None, None, store)  # type: ignore[arg-type]
    try:
        if args.command == "approve":
            tasks = json.loads(Path(args.plan).read_text()) if args.plan else None
            status = runtime.approve_plan(args.run_id, tasks).status
        elif args.command == "resolve":
            if not args.key or not args.outcome:
                for call in store.pending_tool_calls(args.run_id):
                    print(f"{call['key']}  {call['tool']}  attempts={call['attempts']}  args={json.dumps(call['args'])[:120]}")
                return 0
            status = runtime.resolve_tool_call(args.run_id, args.key, args.outcome)
        else:
            status = getattr(runtime, args.command)(args.run_id)
    except (InvalidAction, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"run {args.run_id}: {status}")
    return 0


def _serve(args: argparse.Namespace, store: RunStore) -> int:
    try:
        import uvicorn

        from .server import create_app
    except ImportError:
        print('error: install the server extra: pip install "deeptrace_agent[server]"', file=sys.stderr)
        return 2
    app = create_app(_runtime(args, store), embedded_worker=not args.no_worker)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = RunStore(args.db)
    try:
        if args.command == "show":
            return _show(store, args.run_id)
        if args.command == "list":
            return _list(store)
        if args.command in ("cancel", "pause", "approve", "resolve"):
            return _control(args, store)
        if args.command == "serve":
            return _serve(args, store)
        if args.command == "worker":
            return asyncio.run(_work(args, store))
        return asyncio.run(_run(args, store))
    except (LLMError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; resume later with `deeptrace resume <run id>`", file=sys.stderr)
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
