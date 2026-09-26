"""Held-out evaluation: should the loop also repair claims the judge finds only *partially* supported?

Runs on 24 held-out questions (evals/data/questions_heldout.json) that were written after the
first evaluation and never used to tune anything. Two configurations, identical except for
the repair policy (both: sectioned reports, evidence routing, in-loop claim judge):

  strict    repair unsupported and contradicted claims (the previous default)
  partial   also repair claims the judge labels partial

An independent gpt-4o grader labels every cited claim of every final report.

    python evals/partial_repair_eval.py runs grade report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import faithfulness_eval as fe  # noqa: E402
from deeptrace_agent import ResearchRuntime, RunStore  # noqa: E402
from deeptrace_agent.faithfulness import extract_claims, judge_claims  # noqa: E402
from deeptrace_agent.search import LocalCorpusSearch  # noqa: E402

QUESTIONS = fe.DATA / "questions_heldout.json"
RUNS = fe.DATA / "partial_runs.jsonl"
GRADES = fe.DATA / "partial_grades.jsonl"
CONFIGS = {"strict": False, "partial": True}


async def step_runs(concurrency: int) -> None:
    questions = json.loads(QUESTIONS.read_text())
    done = {(r["qid"], r["config"]) for r in fe.read_jsonl(RUNS)}
    todo = [(q, c) for q in questions for c in CONFIGS if (q["id"], c) not in done]
    print(f"runs: {len(done)} done, {len(todo)} to go")
    if not todo:
        return
    search = LocalCorpusSearch(fe.DATA / "wikipedia")
    writer = fe.llm(fe.WRITER_MODEL)
    store = RunStore(fe.DB)
    runtimes = {name: ResearchRuntime(writer, search, store, owner=f"heldout-{name}", check_faithfulness=True,
                                      repair_partial=flag)
                for name, flag in CONFIGS.items()}
    slots = asyncio.Semaphore(concurrency)

    async def one(q: dict, config: str) -> None:
        async with slots:
            try:
                result = await runtimes[config].start(q["question"])
            except Exception as exc:  # noqa: BLE001 - a re-run retries it
                print(f"  {q['id']:<6} {config:<7} ERROR {str(exc)[:100]}")
                return
            fe.append_jsonl(RUNS, {
                "qid": q["id"], "config": config, "run_id": result.run_id, "status": result.status,
                "tokens": result.state.usage.get("tokens", 0), "repairs": result.state.repairs,
                "in_loop": result.state.faithfulness,
            })
            print(f"  {q['id']:<6} {config:<7} {result.status:<6} tokens={result.state.usage.get('tokens', 0):>6,} "
                  f"repairs={result.state.repairs}")

    await asyncio.gather(*(one(q, c) for q, c in todo))
    await writer.aclose()
    store.close()


async def step_grade(concurrency: int) -> None:
    graded = {g["run_id"] for g in fe.read_jsonl(GRADES)}
    runs = [r for r in fe.read_jsonl(RUNS) if r["status"] == "done" and r["run_id"] not in graded]
    print(f"grade: {len(graded)} done, {len(runs)} to go")
    if not runs:
        return
    grader = fe.Throttled(fe.llm(fe.GRADER_MODEL), 25_000)
    store = RunStore(fe.DB)
    slots = asyncio.Semaphore(concurrency)

    async def one(r: dict) -> None:
        async with slots:
            state = store.load(r["run_id"])
            try:
                judged = await judge_claims(grader, extract_claims(state.report), fe.ledger_of(state))
            except Exception as exc:  # noqa: BLE001
                print(f"  {r['qid']} {r['config']} grading failed: {str(exc)[:80]}")
                return
            fe.append_jsonl(GRADES, {"run_id": r["run_id"], "qid": r["qid"], "config": r["config"],
                                     "summary": judged.summary()})

    await asyncio.gather(*(one(r) for r in runs))
    await grader.aclose()
    store.close()


def step_report() -> None:
    runs = {r["run_id"]: r for r in fe.read_jsonl(RUNS) if r["status"] == "done"}
    grades = [g for g in fe.read_jsonl(GRADES) if g["run_id"] in runs]
    by_q: dict[str, dict[str, dict]] = {}
    for g in grades:
        by_q.setdefault(g["qid"], {})[g["config"]] = g
    paired = sorted(q for q, v in by_q.items() if len(v) == 2)

    def share(config: str, labels: tuple[str, ...]) -> float:
        k = sum(sum(by_q[q][config]["summary"][lab] for lab in labels) for q in paired)
        n = sum(by_q[q][config]["summary"]["claims"] for q in paired)
        return k / n if n else 0.0

    def diff(labels: tuple[str, ...]) -> tuple[float, float, float]:
        per_q = {q: {c: (sum(by_q[q][c]["summary"][lab] for lab in labels), by_q[q][c]["summary"]["claims"])
                     for c in CONFIGS} for q in paired}
        return fe.bootstrap_diff(per_q, "strict", "partial")

    def mean_of(config: str, key: str) -> float:
        return statistics.mean(runs[by_q[q][config]["run_id"]][key] for q in paired)

    rows = {
        "Claims fully supported": ("supported",),
        "Claims partially supported": ("partial",),
        "Claims unsupported or contradicted": ("unsupported", "contradicted"),
    }
    table = {label: {"strict": share("strict", labs), "partial": share("partial", labs), "diff": diff(labs)}
             for label, labs in rows.items()}
    tokens = {c: mean_of(c, "tokens") for c in CONFIGS}
    repairs = {c: mean_of(c, "repairs") for c in CONFIGS}
    claims = {c: statistics.mean(by_q[q][c]["summary"]["claims"] for q in paired) for c in CONFIGS}

    out = {"questions": len(paired), "shares": table, "tokens": tokens, "repairs": repairs,
           "claims_per_report": claims, "generated_at": time.strftime("%Y-%m-%d")}
    fe.RESULTS.mkdir(parents=True, exist_ok=True)
    (fe.RESULTS / "partial_repair_eval.json").write_text(json.dumps(out, indent=2))

    def pct(x):
        return f"{100 * x:.1f}%"

    lines = [
        "# Repairing partially supported claims (held-out questions)",
        "",
        f"{len(paired)} held-out questions, pinned Wikipedia corpus. Writer and in-loop judge `{fe.WRITER_MODEL}`, "
        f"grader `{fe.GRADER_MODEL}`. `strict` repairs unsupported and contradicted claims; `partial` also repairs "
        "claims the in-loop judge labels partial. Differences: paired bootstrap by question, 95% CI.",
        "",
        "| | strict | partial | Difference (points) |",
        "|---|--:|--:|--:|",
    ]
    for label, t in table.items():
        d, lo, hi = t["diff"]
        lines.append(f"| {label} (grader) | {pct(t['strict'])} | {pct(t['partial'])} | "
                     f"**{100 * d:+.1f}** ({100 * lo:+.1f} to {100 * hi:+.1f}) |")
    lines += [
        f"| Repairs per run | {repairs['strict']:.2f} | {repairs['partial']:.2f} | |",
        f"| Cited claims per report | {claims['strict']:.1f} | {claims['partial']:.1f} | |",
        f"| LLM tokens per run | {tokens['strict']:,.0f} | {tokens['partial']:,.0f} | "
        f"{100 * (tokens['partial'] / tokens['strict'] - 1):+.1f}% |",
        "",
    ]
    (fe.RESULTS / "partial_repair_eval.md").write_text("\n".join(lines))
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("steps", nargs="+", choices=("runs", "grade", "report"))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--grader-concurrency", type=int, default=3)
    args = parser.parse_args()
    fe.load_env()
    random.seed(0)
    for step in args.steps:
        if step == "runs":
            asyncio.run(step_runs(args.concurrency))
        elif step == "grade":
            asyncio.run(step_grade(args.grader_concurrency))
        else:
            step_report()


if __name__ == "__main__":
    main()
