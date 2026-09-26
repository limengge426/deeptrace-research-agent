"""Ablation: does per-section evidence routing save tokens and context without hurting quality?

Same 24 questions and pinned Wikipedia corpus as faithfulness_eval.py, two configurations,
both writing the report section by section with the claim judge off:

  routed     each section sees only the evidence of the findings assigned to it (DeepTrace default)
  unrouted   every section sees all evidence (no routing, no condensing): the natural alternative

Measured per run:
  tokens            all LLM tokens of the run, and those of the report stage alone
  context exposure  characters of evidence text placed into report-stage prompts, summed over calls
  coverage          share of findings (and of the evidence they used) cited by the final report
  quality           an independent gpt-4o grader labels every cited claim against its evidence

    python evals/routing_ablation.py runs grade report
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
from deeptrace_agent.metrics import run_metrics  # noqa: E402
from deeptrace_agent.reporter import ContextBudget  # noqa: E402
from deeptrace_agent.search import LocalCorpusSearch  # noqa: E402

RUNS = fe.DATA / "routing_runs.jsonl"
GRADES = fe.DATA / "routing_grades.jsonl"
CONFIGS = {
    "routed": ContextBudget(route=True),
    "unrouted": ContextBudget(route=False, section_chars=10**7),
}
REPORT_PURPOSES = ("outline", "section", "synthesis", "digest")


def coverage(state) -> dict[str, float]:
    cited = set(state.report.citations()) if state.report else set()
    usable = {tid: f for tid, f in state.findings.items() if f.evidence_ids}
    used_evidence = {e for f in usable.values() for e in f.evidence_ids}
    return {
        "findings": len(usable),
        "findings_cited": sum(bool(cited & set(f.evidence_ids)) for f in usable.values()),
        "evidence_used": len(used_evidence),
        "evidence_cited": len(used_evidence & cited),
    }


async def step_runs(concurrency: int) -> None:
    questions = json.loads((fe.DATA / "questions.json").read_text())
    done = {(r["qid"], r["config"]) for r in fe.read_jsonl(RUNS)}
    todo = [(q, c) for q in questions for c in CONFIGS if (q["id"], c) not in done]
    print(f"runs: {len(done)} done, {len(todo)} to go")
    if not todo:
        return
    search = LocalCorpusSearch(fe.DATA / "wikipedia")
    writer = fe.llm(fe.WRITER_MODEL)
    store = RunStore(fe.DB)
    runtimes = {name: ResearchRuntime(writer, search, store, owner=f"ablation-{name}", report_mode="sections",
                                      check_faithfulness=False, context_budget=budget)
                for name, budget in CONFIGS.items()}
    slots = asyncio.Semaphore(concurrency)

    async def one(q: dict, config: str) -> None:
        async with slots:
            try:
                result = await runtimes[config].start(q["question"])
            except Exception as exc:  # noqa: BLE001
                print(f"  {q['id']:<4} {config:<9} ERROR {exc}")
                return
            m = run_metrics(result.state)
            context = result.state.metrics.get("context", {})
            record = {
                "qid": q["id"], "config": config, "run_id": result.run_id, "status": result.status,
                "tokens": result.state.usage.get("tokens", 0),
                "report_tokens": sum(m["llm"].get(p, {}).get("tokens", 0) for p in REPORT_PURPOSES),
                "report_evidence_chars": sum(context.get(p, {}).get("evidence_chars", 0) for p in REPORT_PURPOSES),
                "max_prompt_evidence_chars": max((context.get(p, {}).get("max_evidence_chars", 0)
                                                  for p in REPORT_PURPOSES), default=0),
                "repairs": result.state.repairs,
                **coverage(result.state),
            }
            fe.append_jsonl(RUNS, record)
            print(f"  {q['id']:<4} {config:<9} {result.status:<6} report_tokens={record['report_tokens']:>6,} "
                  f"evidence_chars={record['report_evidence_chars']:>7,} findings_cited="
                  f"{record['findings_cited']}/{record['findings']}")

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
            except Exception as exc:  # noqa: BLE001 - a re-run grades it
                print(f"  {r['qid']} {r['config']} grading failed: {str(exc)[:80]}")
                return
            fe.append_jsonl(GRADES, {"run_id": r["run_id"], "qid": r["qid"], "config": r["config"],
                                     "summary": judged.summary()})

    await asyncio.gather(*(one(r) for r in runs))
    await grader.aclose()
    store.close()


def _paired_reduction(runs: list[dict], key: str) -> tuple[float, float, float, int, int]:
    """Pooled reduction of ``key`` (routed vs unrouted), with a paired bootstrap 95% CI by question."""
    by_q: dict[str, dict[str, float]] = {}
    for r in runs:
        by_q.setdefault(r["qid"], {})[r["config"]] = r[key]
    qs = sorted(q for q, v in by_q.items() if len(v) == 2)
    rng = random.Random(0)

    def reduction(sample):
        routed = sum(by_q[q]["routed"] for q in sample)
        unrouted = sum(by_q[q]["unrouted"] for q in sample)
        return 1 - routed / unrouted if unrouted else 0.0

    boots = sorted(reduction([rng.choice(qs) for _ in qs]) for _ in range(5000))
    smaller = sum(by_q[q]["routed"] < by_q[q]["unrouted"] for q in qs)
    return reduction(qs), boots[125], boots[4875], smaller, len(qs)


def step_report() -> None:
    runs = [r for r in fe.read_jsonl(RUNS) if r["status"] == "done"]
    grades = {g["run_id"]: g for g in fe.read_jsonl(GRADES)}
    paired = {q for q in {r["qid"] for r in runs} if sum(r["qid"] == q for r in runs) == 2}
    runs = [r for r in runs if r["qid"] in paired]

    def cfg(name):
        return [r for r in runs if r["config"] == name]

    rows = {}
    for name in CONFIGS:
        rs = cfg(name)
        gs = [grades[r["run_id"]]["summary"] for r in rs if r["run_id"] in grades]
        claims = sum(g["claims"] for g in gs)
        rows[name] = {
            "tokens": statistics.mean(r["tokens"] for r in rs),
            "report_tokens": statistics.mean(r["report_tokens"] for r in rs),
            "report_evidence_chars": statistics.mean(r["report_evidence_chars"] for r in rs),
            "max_prompt_evidence_chars": statistics.mean(r["max_prompt_evidence_chars"] for r in rs),
            "findings_coverage": sum(r["findings_cited"] for r in rs) / max(sum(r["findings"] for r in rs), 1),
            "evidence_coverage": sum(r["evidence_cited"] for r in rs) / max(sum(r["evidence_used"] for r in rs), 1),
            "supported": sum(g["supported"] for g in gs) / claims if claims else None,
            "failing": sum(g["unsupported"] + g["contradicted"] for g in gs) / claims if claims else None,
            "graded": len(gs),
        }
    reductions = {key: _paired_reduction(runs, key)
                  for key in ("tokens", "report_tokens", "report_evidence_chars", "max_prompt_evidence_chars")}

    per_q = {}
    for r in runs:
        g = grades.get(r["run_id"])
        if g:
            per_q.setdefault(r["qid"], {})[r["config"]] = (g["summary"]["supported"], g["summary"]["claims"])
    per_q = {q: v for q, v in per_q.items() if len(v) == 2}
    quality = fe.bootstrap_diff(per_q, "unrouted", "routed") if per_q else None

    out = {"questions": len(paired), "configs": rows, "reductions": reductions,
           "supported_share_difference": quality, "generated_at": time.strftime("%Y-%m-%d")}
    fe.RESULTS.mkdir(parents=True, exist_ok=True)
    (fe.RESULTS / "routing_ablation.json").write_text(json.dumps(out, indent=2))

    def pct(x):
        return f"{100 * x:.1f}%"

    lines = [
        "# Evidence routing ablation",
        "",
        f"{len(paired)} questions, pinned Wikipedia corpus, `{fe.WRITER_MODEL}`, sectioned reports, claim judge off. "
        "`routed`: each section sees only its findings' evidence (DeepTrace). `unrouted`: every section sees all "
        f"evidence. Quality graded by `{fe.GRADER_MODEL}`.",
        "",
        "| | routed | unrouted | Reduction (95% CI, paired bootstrap) | Questions where routed is smaller |",
        "|---|--:|--:|--:|--:|",
    ]
    labels = {"tokens": "LLM tokens per run", "report_tokens": "Report-stage tokens per run",
              "report_evidence_chars": "Evidence characters placed into report-stage prompts (cumulative exposure)",
              "max_prompt_evidence_chars": "Evidence characters in the largest report-stage prompt"}
    for key, label in labels.items():
        red, lo, hi, smaller, n = reductions[key]
        lines.append(f"| {label} | {rows['routed'][key]:,.0f} | {rows['unrouted'][key]:,.0f} | "
                     f"**{pct(red)}** ({pct(lo)} to {pct(hi)}) | {smaller}/{n} |")
    lines += [
        f"| Findings cited by the final report | {pct(rows['routed']['findings_coverage'])} | "
        f"{pct(rows['unrouted']['findings_coverage'])} | | |",
        f"| Evidence used by findings that the report cites | {pct(rows['routed']['evidence_coverage'])} | "
        f"{pct(rows['unrouted']['evidence_coverage'])} | | |",
        f"| Claims fully supported (grader) | {pct(rows['routed']['supported'])} | "
        f"{pct(rows['unrouted']['supported'])} | | |",
        f"| Claims unsupported or contradicted (grader) | {pct(rows['routed']['failing'])} | "
        f"{pct(rows['unrouted']['failing'])} | | |",
        "",
    ]
    if quality:
        diff, lo, hi = quality
        lines.append(f"Difference in fully supported claims, routed minus unrouted: {100 * diff:+.1f} points "
                     f"(95% CI {100 * lo:+.1f} to {100 * hi:+.1f}).")
    lines.append("")
    (fe.RESULTS / "routing_ablation.md").write_text("\n".join(lines))
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("steps", nargs="+", choices=("runs", "grade", "report"))
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--grader-concurrency", type=int, default=3)
    args = parser.parse_args()
    fe.load_env()
    for step in args.steps:
        if step == "runs":
            asyncio.run(step_runs(args.concurrency))
        elif step == "grade":
            asyncio.run(step_grade(args.grader_concurrency))
        else:
            step_report()


if __name__ == "__main__":
    main()
