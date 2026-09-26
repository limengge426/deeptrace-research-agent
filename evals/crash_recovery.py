"""Fault-injection benchmark: hard-kill a run at every call site, then resume it.

Each scenario starts a fresh worker process that runs the research loop with a
scripted LLM and search provider, and SIGKILLs itself at one injection point:

  llm:k          right before the k-th LLM call
  search:k       right before the k-th search call
  search-post:k  right after the k-th search ran, before its result is cached
  checkpoint:k   right before the k-th checkpoint write
  webhook:k      right before the finished report is POSTed to the run's webhook
  webhook-post:k right after the webhook POST, before its result is recorded

Fresh worker processes then resume the run until it finishes. The killed
process never releases its lease, so each recovery first waits for that lease
to expire (TTL 0.3 s here) and then takes the run over with a new fencing token. Every search that
actually executes is appended (and fsync'ed) to a side-effect log, so duplicate
side effects can be counted across processes.

The scripted scenario exercises every stage, including one report repair (the
first draft of one section cites a non-existent source), one replan (one
sub-question returns no search results) and delivery of the finished report to
a webhook. The simulated receiver honors the Idempotency-Key header the way
Stripe-style APIs do, so both raw sends and effective (deduplicated) deliveries
are counted.

    python evals/crash_recovery.py            # run the benchmark, write results
    python evals/crash_recovery.py --quick    # every 3rd injection point only
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from researchloop import ResearchRuntime, RunStore  # noqa: E402
from researchloop.runtime import RunLocked  # noqa: E402
from researchloop.store import RunStore as _Store  # noqa: E402
from tests import fakes  # noqa: E402

QUESTION = "Are heat pumps worth it in cold climates?"
KINDS = ("llm", "search", "search-post", "checkpoint", "webhook", "webhook-post")
WEBHOOK_URL = "https://hooks.example/report"
LEASE_TTL = 0.3
EXIT_LOCKED = 3


# -- worker (runs in a child process) ---------------------------------------


def _section_with_one_bad_draft(system: str, user: str) -> dict:
    out = fakes.section(system, user)
    if "failed verification" not in user and "Section: About t1" in user:
        out["body"] += " See also [E999]."  # first draft: cite a source that does not exist
    return out


class Injector:
    def __init__(self, kill_at: str | None, trace: Path) -> None:
        self.kind, self.k = (kill_at.split(":")[0], int(kill_at.split(":")[1])) if kill_at else (None, 0)
        self.counts: Counter[str] = Counter()
        self.trace = trace

    def hit(self, kind: str, stage: str) -> None:
        self.counts[kind] += 1
        if kind == self.kind and self.counts[kind] == self.k:
            _append(self.trace, {"killed_at": f"{kind}:{self.k}", "stage": stage})
            os.kill(os.getpid(), signal.SIGKILL)


def _append(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _stage_of(purpose: str, user: str) -> str:
    if purpose == "plan":
        return "verify" if "verifier found these gaps" in user else "plan"
    if purpose in ("queries", "finding"):
        return "execute"
    return "report" if purpose in ("outline", "section", "synthesis", "digest", "report") else "verify"


class InjectingLLM(fakes.ScriptedLLM):
    def __init__(self, injector: Injector) -> None:
        super().__init__(section=_section_with_one_bad_draft)
        self.injector = injector

    async def complete(self, system, user, *, purpose, json_mode=False):
        self.injector.hit("llm", _stage_of(purpose, user))
        _append(self.injector.trace.with_name("llm_calls.jsonl"), {"purpose": purpose, "pid": os.getpid()})
        return await super().complete(system, user, purpose=purpose, json_mode=json_mode)


class InjectingSearch(fakes.FakeSearch):
    def __init__(self, injector: Injector, side_effects: Path) -> None:
        super().__init__(empty_for=("efficient",))
        self.injector = injector
        self.side_effects = side_effects

    async def search(self, query, k):
        self.injector.hit("search", "execute")
        hits = await super().search(query, k)
        _append(self.side_effects, {"query": query, "pid": os.getpid()})
        self.injector.hit("search-post", "execute")
        return hits


class InjectingWebhook:
    """Records every POST it sends; the receiver deduplicates by Idempotency-Key."""

    name, side_effects, idempotent = "webhook", True, True

    def __init__(self, injector: Injector, side_effects: Path) -> None:
        self.injector = injector
        self.log = side_effects

    async def __call__(self, args, *, idempotency_key):
        self.injector.hit("webhook", "deliver")
        _append(self.log, {"webhook_key": idempotency_key, "pid": os.getpid()})
        self.injector.hit("webhook-post", "deliver")
        return {"status_code": 200}


class InjectingStore(_Store):
    injector: Injector

    def checkpoint(self, run_id, state, *, status, lease=None):
        # Label with the stage recorded on disk, i.e. the stage whose work was just completed.
        row = self._conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.injector.hit("checkpoint", row[0] if row else state.stage)
        super().checkpoint(run_id, state, status=status, lease=lease)


def worker(args: argparse.Namespace) -> None:
    work = Path(args.dir)
    injector = Injector(args.kill_at, work / "trace.jsonl")
    store = InjectingStore(work / "runs.db")
    store.injector = injector
    llm = InjectingLLM(injector)
    runtime = ResearchRuntime(
        llm, InjectingSearch(injector, work / "side_effects.jsonl"), store, lease_ttl=LEASE_TTL,
        webhook=InjectingWebhook(injector, work / "side_effects.jsonl"),
    )

    runs = store.runs()
    try:
        result = asyncio.run(runtime.resume(runs[0].id) if runs else runtime.start(QUESTION, deliver_to=WEBHOOK_URL))
    except RunLocked:
        raise SystemExit(EXIT_LOCKED)  # the killed process's lease has not expired yet
    _append(
        work / "trace.jsonl",
        {
            "finished": result.status,
            "llm_calls": sum(llm.calls.values()),
            "report_sha": hashlib.sha256((result.markdown or "").encode()).hexdigest(),
        },
    )
    store.close()


# -- orchestrator --------------------------------------------------------------


def _spawn(work: Path, kill_at: str | None) -> int:
    cmd = [sys.executable, __file__, "worker", "--dir", str(work)]
    if kill_at:
        cmd += ["--kill-at", kill_at]
    return subprocess.run(cmd, capture_output=True, text=True).returncode


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def run_scenario(kills: list[str | None], max_processes: int = 8) -> dict:
    """Run one process per entry in ``kills`` (then clean resumes) until the run finishes."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        processes, crashes, lock_waits = 0, 0, 0
        plan = list(kills)
        while processes < max_processes:
            kill_at = plan.pop(0) if plan else None
            code = _spawn(work, kill_at)
            if code == EXIT_LOCKED:
                lock_waits += 1
                if kill_at:
                    plan.insert(0, kill_at)
                time.sleep(LEASE_TTL / 3)
                continue
            processes += 1
            if code == -signal.SIGKILL:
                crashes += 1
                continue
            if code != 0:
                return {"error": f"worker exited with {code}", "processes": processes}
            break
        trace = _read(work / "trace.jsonl")
        records = _read(work / "side_effects.jsonl")
        side_effects = Counter(r["query"] for r in records if "query" in r)
        deliveries = [r["webhook_key"] for r in records if "webhook_key" in r]
        finished = [t for t in trace if "finished" in t]
        llm_calls = len(_read(work / "llm_calls.jsonl"))
        return {
            "kills": [t for t in trace if "killed_at" in t],
            "crashes": crashes,
            "lock_waits": lock_waits,
            "processes": processes,
            "status": finished[-1]["finished"] if finished else None,
            "report_sha": finished[-1]["report_sha"] if finished else None,
            "llm_calls": llm_calls,
            "searches": sum(side_effects.values()),
            "duplicate_searches": sum(n - 1 for n in side_effects.values() if n > 1),
            "webhook_sends": len(deliveries),
            "webhook_effective": len(set(deliveries)),  # what an Idempotency-Key-honoring receiver processes
        }


def _count_sites() -> Counter[str]:
    """Count injection sites by running the scenario once with a counter-only injector."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        injector = Injector(None, work / "trace.jsonl")
        store = InjectingStore(work / "runs.db")
        store.injector = injector
        runtime = ResearchRuntime(
            InjectingLLM(injector), InjectingSearch(injector, work / "se.jsonl"), store, lease_ttl=LEASE_TTL,
            webhook=InjectingWebhook(injector, work / "se.jsonl"),
        )
        asyncio.run(runtime.start(QUESTION, deliver_to=WEBHOOK_URL))
        store.close()
        return injector.counts


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    w = sub.add_parser("worker")
    w.add_argument("--dir", required=True)
    w.add_argument("--kill-at")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--multi", type=int, default=30, help="random multi-crash scenarios")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    if args.cmd == "worker":
        return worker(args)

    started = time.time()
    sites = _count_sites()
    baseline = run_scenario([None])
    assert baseline["status"] == "done", baseline
    print(f"baseline: {dict(sites)}  llm_calls={baseline['llm_calls']} searches={baseline['searches']}")

    single = []
    for kind in KINDS:
        step = 3 if args.quick else 1
        for k in range(1, sites[kind] + 1, step):
            r = run_scenario([f"{kind}:{k}"])
            r["kind"] = kind
            single.append(r)
            print(f"  {kind}:{k:<3} stage={r['kills'][0]['stage'] if r['kills'] else '-':8} "
                  f"status={r['status']} dup={r['duplicate_searches']} "
                  f"sends={r['webhook_sends']} effective={r['webhook_effective']}")

    rng = random.Random(args.seed)
    multi = []
    for _ in range(args.multi):
        n = rng.randint(2, 3)
        kills = [f"{(kind := rng.choice(KINDS))}:{rng.randint(1, max(1, sites[kind] // 2))}" for _ in range(n)]
        multi.append(run_scenario(kills))

    results = {
        "sites": dict(sites),
        "baseline": baseline,
        "single": single,
        "multi": multi,
        "seconds": round(time.time() - started, 1),
    }
    out = ROOT / "evals" / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / "crash_recovery.json").write_text(json.dumps(results, indent=2))
    (out / "crash_recovery.md").write_text(summarize(results))
    print(summarize(results))


def summarize(results: dict) -> str:
    base = results["baseline"]

    def row(label: str, rs: list[dict]) -> str:
        crashed = [r for r in rs if r["crashes"]]
        ok = sum(r["status"] == "done" for r in crashed)
        same = sum(r["report_sha"] == base["report_sha"] for r in crashed)
        dups = sum(r["duplicate_searches"] for r in crashed)
        with_dup = sum(r["duplicate_searches"] > 0 for r in crashed)
        resent = sum(r["webhook_sends"] > 1 for r in crashed)
        effective_ok = sum(r["webhook_effective"] == 1 for r in crashed)
        extra_llm = sum(r["llm_calls"] - base["llm_calls"] for r in crashed)
        return (f"| {label} | {len(crashed)} | {ok}/{len(crashed)} | {same}/{len(crashed)} | "
                f"{with_dup} ({dups} total) | {resent} | {effective_ok}/{len(crashed)} | "
                f"{extra_llm / max(len(crashed), 1):.1f} |")

    by_stage: dict[str, list[dict]] = {}
    for r in results["single"]:
        if r["kills"]:
            by_stage.setdefault(r["kills"][0]["stage"], []).append(r)
    by_kind: dict[str, list[dict]] = {}
    for r in results["single"]:
        by_kind.setdefault(r["kind"], []).append(r)

    header = ("| Scenario | Crashed runs | Recovered | Report identical to uncrashed run | "
              "Runs with duplicate searches | Runs with a webhook re-send | Delivered exactly once (after dedup) | "
              "Re-executed LLM calls / run |\n|---|--:|--:|--:|--:|--:|--:|--:|")
    lines = [
        "# Crash-recovery benchmark",
        "",
        f"Uncrashed baseline: {base['llm_calls']} LLM calls, {base['searches']} searches, "
        f"{results['sites']['checkpoint']} checkpoints (with 1 report repair and 1 replan).",
        "Each crash is a SIGKILL of the worker process; recovery runs in a fresh process that waits for the "
        f"dead process's lease to expire (TTL {LEASE_TTL}s) and takes the run over.",
        "",
        "## By injection point",
        "",
        header,
        *[row(k, by_kind[k]) for k in KINDS if k in by_kind],
        row("**all single crashes**", results["single"]),
        row("**2-3 crashes per run (random)**", results["multi"]),
        "",
        "## By pipeline stage at crash time",
        "",
        header,
        *[row(s, by_stage[s]) for s in ("plan", "execute", "report", "verify", "deliver") if s in by_stage],
        "",
        f"_{len(results['single']) + len(results['multi']) + 1} scenarios in {results['seconds']}s._",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
