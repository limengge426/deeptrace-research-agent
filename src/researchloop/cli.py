"""Command-line interface: ``researchloop run | resume | show | list``."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from .budget import Budget
from .llm import LLMError, OpenAICompatLLM
from .runtime import ResearchRuntime, RunResult
from .search import LocalCorpusSearch, SearchProvider, TavilySearch
from .store import RunStore


def _search_from_args(args: argparse.Namespace) -> SearchProvider:
    if args.corpus:
        return LocalCorpusSearch(args.corpus)
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
    if state.error:
        print(f"stopped: {state.error}")
    if result.status == "halted":
        print(f"continue with: researchloop resume {result.run_id} --max-tokens <larger>")
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
    return 0


def _list(store: RunStore) -> int:
    for run in store.runs():
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.created_at))
        print(f"{run.id}  {run.status:<8} {when}  {run.question[:70]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # --db is accepted both before and after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="SQLite file for runs (default: researchloop.db)")
    parser = argparse.ArgumentParser(prog="researchloop", description="Evidence-first research agent.", parents=[common])
    parser.set_defaults(db="researchloop.db")
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

    run = sub.add_parser("run", help="start a new research run", parents=[common])
    run.add_argument("question")
    add_run_options(run)

    resume = sub.add_parser("resume", help="continue an interrupted or halted run", parents=[common])
    resume.add_argument("run_id")
    add_run_options(resume)

    show = sub.add_parser("show", help="print a run's event timeline", parents=[common])
    show.add_argument("run_id")

    sub.add_parser("list", help="list recent runs", parents=[common])
    return parser


async def _run(args: argparse.Namespace, store: RunStore) -> int:
    llm = OpenAICompatLLM.from_env()
    search = _search_from_args(args)
    runtime = ResearchRuntime(
        llm,
        search,
        store,
        budget=_budget_from_args(args),
        max_tasks=args.max_tasks,
        concurrency=args.concurrency,
        critic=args.critic,
    )
    try:
        if args.command == "run":
            result = await runtime.start(args.question)
        else:
            result = await runtime.resume(args.run_id)
    finally:
        await llm.aclose()
        if isinstance(search, TavilySearch):
            await search.aclose()
    return _report(result, Path(args.out))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = RunStore(args.db)
    try:
        if args.command == "show":
            return _show(store, args.run_id)
        if args.command == "list":
            return _list(store)
        return asyncio.run(_run(args, store))
    except (LLMError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; resume later with `researchloop resume <run id>`", file=sys.stderr)
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
