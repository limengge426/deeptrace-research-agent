"""Real-model evaluation on a fixed Wikipedia corpus.

Three steps, each resumable (finished work is skipped on re-runs):

  runs    Research 24 questions under three configurations:
            single          one-shot report, no claim judge (baseline)
            sections        sectioned report with per-section evidence routing, no claim judge
            sections+judge  sectioned report + claim-level faithfulness judge and repair (full system)
  grade   An independent, stronger model (gpt-4o) labels every cited claim of every final
          report against its evidence. The writer and in-loop judge are gpt-4o-mini.
  detect  Take claims the grader labeled "supported", inject two kinds of faults
          (citation swapped to unrelated evidence; minimal factual edit written by gpt-4o),
          keep only faults the grader confirms are no longer supported, and measure how many
          the in-loop judge catches, plus its false-alarm rate on the untouched claims.
  report  Write evals/results/faithfulness_eval.{md,json}.

    python evals/fetch_wikipedia.py
    python evals/faithfulness_eval.py runs grade detect report

Needs RESEARCHLOOP_API_KEY (read from the environment or .env). Uses an OpenAI-compatible API.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from researchloop import ResearchRuntime, RunStore  # noqa: E402
from researchloop.faithfulness import FAILING, Claim, extract_claims, judge_claims  # noqa: E402
from researchloop.ledger import EvidenceLedger  # noqa: E402
from researchloop.llm import Completion, OpenAICompatLLM, estimate_tokens, parse_json_object  # noqa: E402
from researchloop.metrics import run_metrics  # noqa: E402
from researchloop.search import LocalCorpusSearch  # noqa: E402
from researchloop.verifier import _FIGURE_RE  # noqa: E402
from researchloop.faithfulness import split_sentences  # noqa: E402
from researchloop.models import CITATION_RE  # noqa: E402

DATA = ROOT / "evals" / "data"
RESULTS = ROOT / "evals" / "results"
DB = DATA / "eval_runs.db"
RUNS = DATA / "eval_runs.jsonl"
GRADES = DATA / "eval_grades.jsonl"
DETECT = DATA / "eval_detect.jsonl"
CASES = DATA / "eval_detect_cases.jsonl"

WRITER_MODEL = "gpt-4o-mini"
GRADER_MODEL = "gpt-4o"
CONFIGS = {
    "single": {"report_mode": "single", "check_faithfulness": False},
    "sections": {"report_mode": "sections", "check_faithfulness": False},
    "sections+judge": {"report_mode": "sections", "check_faithfulness": True},
}
REPORT_PURPOSES = ("report", "outline", "section", "synthesis", "digest")


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def llm(model: str) -> OpenAICompatLLM:
    return OpenAICompatLLM(model, os.environ["RESEARCHLOOP_API_KEY"],
                           os.getenv("RESEARCHLOOP_BASE_URL", "https://api.openai.com/v1"), temperature=0.2,
                           max_retries=12)


class Throttled:
    """Client-side tokens-per-minute limiter, so sustained load stays under the account's rate limit."""

    def __init__(self, inner: OpenAICompatLLM, tokens_per_minute: int) -> None:
        self.inner = inner
        self.tpm = tokens_per_minute
        self._window: list[tuple[float, int]] = []
        self._lock = asyncio.Lock()

    async def complete(self, system: str, user: str, *, purpose: str, json_mode: bool = False) -> Completion:
        need = estimate_tokens(system, user) + 400  # prompt estimate plus room for the reply
        async with self._lock:
            while True:
                now = time.monotonic()
                self._window = [(t, n) for t, n in self._window if now - t < 60]
                if sum(n for _, n in self._window) + need <= self.tpm:
                    self._window.append((now, need))
                    break
                await asyncio.sleep(max(0.2, 60 - (now - self._window[0][0])))
        return await self.inner.complete(system, user, purpose=purpose, json_mode=json_mode)

    async def aclose(self) -> None:
        await self.inner.aclose()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def ledger_of(state) -> EvidenceLedger:
    return EvidenceLedger(state.evidence)


# -- step 1: runs ----------------------------------------------------------------


async def step_runs(concurrency: int, only: set[str] | None = None) -> None:
    questions = [q for q in json.loads((DATA / "questions.json").read_text()) if not only or q["id"] in only]
    done = {(r["qid"], r["config"]) for r in read_jsonl(RUNS)}
    todo = [(q, c) for q in questions for c in CONFIGS if (q["id"], c) not in done]
    print(f"runs: {len(done)} done, {len(todo)} to go")
    if not todo:
        return
    search = LocalCorpusSearch(DATA / "wikipedia")
    writer = llm(WRITER_MODEL)
    store = RunStore(DB)
    runtimes = {name: ResearchRuntime(writer, search, store, owner=f"eval-{name}", **cfg)
                for name, cfg in CONFIGS.items()}
    slots = asyncio.Semaphore(concurrency)

    async def one(q: dict, config: str) -> None:
        async with slots:
            started = time.monotonic()
            try:
                result = await runtimes[config].start(q["question"])
            except Exception as exc:  # noqa: BLE001 - record and move on
                append_jsonl(RUNS, {"qid": q["id"], "config": config, "status": "error", "error": str(exc)})
                print(f"  {q['id']:<4} {config:<15} ERROR {exc}")
                return
            m = run_metrics(result.state)
            record = {
                "qid": q["id"], "config": config, "run_id": result.run_id, "status": result.status,
                "seconds": round(time.monotonic() - started, 1),
                "tokens": result.state.usage.get("tokens", 0),
                "report_tokens": sum(m["llm"].get(p, {}).get("tokens", 0) for p in REPORT_PURPOSES),
                "judge_tokens": m["llm"].get("judge", {}).get("tokens", 0),
                "max_report_call_tokens": max((m["llm"].get(p, {}).get("max_call_tokens", 0)
                                               for p in REPORT_PURPOSES), default=0),
                "llm": m["llm"], "repairs": result.state.repairs, "replans": result.state.replans,
                "evidence": len(result.state.evidence), "in_loop_faithfulness": result.state.faithfulness,
            }
            append_jsonl(RUNS, record)
            print(f"  {q['id']:<4} {config:<15} {result.status:<6} {record['tokens']:>7,} tok "
                  f"repairs={record['repairs']} {record['seconds']}s")

    await asyncio.gather(*(one(q, c) for q, c in todo))
    await writer.aclose()
    store.close()


# -- step 2: independent grading ----------------------------------------------------


def uncited_figures(report) -> int:
    return sum(
        1 for s in report.sections if not s.synthesis
        for sent in split_sentences(s.body) if _FIGURE_RE.search(sent) and not CITATION_RE.search(sent)
    )


async def step_grade(concurrency: int) -> None:
    graded = {r["run_id"] for r in read_jsonl(GRADES)}
    runs = [r for r in read_jsonl(RUNS) if r.get("status") == "done" and r["run_id"] not in graded]
    print(f"grade: {len(graded)} done, {len(runs)} to go")
    if not runs:
        return
    grader = Throttled(llm(GRADER_MODEL), 25_000)
    store = RunStore(DB)
    slots = asyncio.Semaphore(concurrency)

    async def one(r: dict) -> None:
        async with slots:
            state = store.load(r["run_id"])
            claims = extract_claims(state.report)
            try:
                judged = await judge_claims(grader, claims, ledger_of(state))
            except Exception as exc:  # noqa: BLE001 - skip; a re-run grades it
                print(f"  {r['qid']:<4} {r['config']:<15} grading failed, will retry on re-run: {exc}")
                return
            append_jsonl(GRADES, {
                "run_id": r["run_id"], "qid": r["qid"], "config": r["config"],
                "summary": judged.summary(), "uncited_figures": uncited_figures(state.report),
                "verdicts": [{"id": v.claim.id, "section": v.claim.section, "text": v.claim.text,
                              "citations": v.claim.citations, "label": v.label, "reason": v.reason}
                             for v in judged.verdicts],
            })
            print(f"  {r['qid']:<4} {r['config']:<15} support={judged.support_rate:.2f} ({len(claims)} claims)")

    await asyncio.gather(*(one(r) for r in runs))
    await grader.aclose()
    store.close()


# -- step 3: fault-injection detection ---------------------------------------------

EDIT_SYSTEM = """You create test cases for a citation checker.
Minimally edit the claim so that the cited evidence NO LONGER supports it: change one number,
named entity, date, direction (increase/decrease), or quantifier (all/some). Keep the wording,
length and style otherwise identical. The edited claim must still sound plausible.

Reply with JSON only: {"edited": "...", "change": "what you changed, in a few words"}"""


async def step_detect(n: int, seed: int, concurrency: int) -> None:
    rng = random.Random(seed)
    store = RunStore(DB)
    grader, judge = Throttled(llm(GRADER_MODEL), 25_000), llm(WRITER_MODEL)
    slots = asyncio.Semaphore(concurrency)
    if not CASES.exists():
        await _make_detect_cases(n, rng, store, grader, slots)
    cases = read_jsonl(CASES)
    labeled = {(d["case"]) for d in read_jsonl(DETECT)}
    todo = [c for c in cases if c["case"] not in labeled]
    print(f"detect: {len(cases)} cases, {len(labeled)} labeled, {len(todo)} to go")

    async def label(model_llm, case: dict) -> dict | None:
        state = store.load(case["run_id"])
        claim = Claim(1, "eval", case["text"], case["citations"])
        async with slots:
            try:
                judged = await judge_claims(model_llm, [claim], ledger_of(state))
            except Exception as exc:  # noqa: BLE001 - left unlabeled; a re-run retries it
                print(f"  case {case['case']} failed: {str(exc)[:80]}")
                return None
        v = judged.verdicts[0]
        return {"label": v.label, "reason": v.reason}

    async def one(case: dict) -> None:
        g, j = await asyncio.gather(label(grader, case), label(judge, case))
        if g and j:
            append_jsonl(DETECT, {**case, "grader": g, "judge": j})

    await asyncio.gather(*(one(c) for c in todo))
    await grader.aclose()
    await judge.aclose()
    store.close()


async def _make_detect_cases(n, rng, store, grader, slots) -> None:
    grades = sorted((g for g in read_jsonl(GRADES) if g["config"] in ("sections", "sections+judge")),
                    key=lambda g: (g["qid"], g["config"]))
    pool = [(g["run_id"], v) for g in grades for v in g["verdicts"] if v["label"] == "supported"]
    sample = rng.sample(pool, min(n, len(pool)))

    async def make_cases(run_id: str, v: dict) -> list[dict]:
        state = store.load(run_id)
        ledger = ledger_of(state)
        cited_tasks = {ledger.get(c).task_id for c in v["citations"] if c in ledger}
        foreign = [e.id for e in ledger.items() if e.task_id not in cited_tasks]
        cases = [{"run_id": run_id, "kind": "original", "text": v["text"], "citations": v["citations"]}]
        if foreign:
            swapped = rng.choice(foreign)
            text = CITATION_RE.sub("", v["text"]).rstrip() + f" [{swapped}]"
            cases.append({"run_id": run_id, "kind": "citation_swap", "text": text, "citations": [swapped]})
        evidence = "\n\n".join(ledger.get(c).content[:1500] for c in v["citations"] if c in ledger)
        async with slots:
            reply = await grader.complete(EDIT_SYSTEM, f"Evidence:\n{evidence}\n\nClaim: {v['text']}",
                                          purpose="perturb", json_mode=True)
        try:
            edited = str(parse_json_object(reply.text)["edited"]).strip()
            if edited and edited != v["text"]:
                if not CITATION_RE.search(edited):
                    edited += " " + " ".join(f"[{c}]" for c in v["citations"])
                cases.append({"run_id": run_id, "kind": "fact_edit", "text": edited, "citations": v["citations"]})
        except Exception:  # noqa: BLE001
            pass
        return cases

    groups = await asyncio.gather(*(make_cases(r, v) for r, v in sample))
    cases = [c for group in groups for c in group]
    for i, c in enumerate(cases):
        append_jsonl(CASES, {"case": i, **c})
    print(f"detect: {len(sample)} supported claims -> {len(cases)} cases")


# -- step 4: report --------------------------------------------------------------


def _mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def step_report() -> None:
    runs = read_jsonl(RUNS)
    grades = {g["run_id"]: g for g in read_jsonl(GRADES)}
    detect = read_jsonl(DETECT)
    questions = {q["id"] for q in json.loads((DATA / "questions.json").read_text())}
    both_done = {q for q in questions if all(any(r["qid"] == q and r["config"] == c and r.get("status") == "done"
                                                 for r in runs) for c in CONFIGS)}

    table = {}
    for config in CONFIGS:
        rs = [r for r in runs if r["config"] == config and r["qid"] in both_done]
        gs = [grades[r["run_id"]] for r in rs if r["run_id"] in grades]
        claims = sum(g["summary"]["claims"] for g in gs)
        failing = sum(g["summary"]["unsupported"] + g["summary"]["contradicted"] for g in gs)
        table[config] = {
            "runs": len(rs),
            "support_rate_mean": _mean([g["summary"]["support_rate"] for g in gs]),
            "claims": claims,
            "claims_per_report": claims / len(gs) if gs else None,
            "unsupported_or_contradicted_share": failing / claims if claims else None,
            "uncited_figures_per_report": _mean([g["uncited_figures"] for g in gs]),
            "tokens_mean": _mean([r["tokens"] for r in rs]),
            "report_tokens_mean": _mean([r["report_tokens"] for r in rs]),
            "judge_tokens_mean": _mean([r["judge_tokens"] for r in rs]),
            "max_report_call_tokens_mean": _mean([r.get("max_report_call_tokens") for r in rs]),
            "max_report_call_tokens_max": max((r.get("max_report_call_tokens", 0) for r in rs), default=None),
            "repairs_mean": _mean([r["repairs"] for r in rs]),
            "seconds_mean": _mean([r["seconds"] for r in rs]),
        }

    det = {}
    originals = [d for d in detect if d["kind"] == "original" and d["grader"]["label"] == "supported"]
    for kind in ("citation_swap", "fact_edit"):
        confirmed = [d for d in detect if d["kind"] == kind and d["grader"]["label"] in FAILING]
        caught = [d for d in confirmed if d["judge"]["label"] in FAILING]
        caught_lenient = [d for d in confirmed if d["judge"]["label"] != "supported"]
        det[kind] = {"cases": len(confirmed), "caught": len(caught), "recall": len(caught) / len(confirmed) if confirmed else None,
                     "recall_incl_partial": len(caught_lenient) / len(confirmed) if confirmed else None,
                     "generated": sum(d["kind"] == kind for d in detect)}
    false_alarms = [d for d in originals if d["judge"]["label"] in FAILING]
    det["false_alarm_rate"] = len(false_alarms) / len(originals) if originals else None
    det["originals"] = len(originals)

    out = {"writer_model": WRITER_MODEL, "grader_model": GRADER_MODEL, "questions": len(both_done),
           "configs": table, "detection": det, "generated_at": time.strftime("%Y-%m-%d")}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "faithfulness_eval.json").write_text(json.dumps(out, indent=2))

    def pct(x):
        return "–" if x is None else f"{100 * x:.1f}%"

    def num(x, fmt="{:,.0f}"):
        return "–" if x is None else fmt.format(x)

    lines = [
        "# Faithfulness and context evaluation",
        "",
        f"{len(both_done)} questions over 40 pinned Wikipedia articles (local BM25 search). "
        f"Writer and in-loop judge: `{WRITER_MODEL}`. Independent grader: `{GRADER_MODEL}`, "
        "labeling every cited claim of every final report against the full text of its evidence.",
        "",
        "## End to end",
        "",
        "| Config | Claims fully supported (grader) | Unsupported or contradicted | Uncited figures / report | "
        "Tokens / run | Report-stage tokens / run | Largest report-stage call (mean / max) | Judge tokens / run | "
        "Repairs / run |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for config, t in table.items():
        lines.append(f"| `{config}` | {pct(t['support_rate_mean'])} | {pct(t['unsupported_or_contradicted_share'])} | "
                     f"{num(t['uncited_figures_per_report'], '{:.2f}')} | {num(t['tokens_mean'])} | "
                     f"{num(t['report_tokens_mean'])} | {num(t['max_report_call_tokens_mean'])} / "
                     f"{num(t['max_report_call_tokens_max'])} | {num(t['judge_tokens_mean'])} | "
                     f"{num(t['repairs_mean'], '{:.2f}')} |")
    lines += [
        "",
        "## Can the in-loop judge catch injected faults?",
        "",
        "Faults are injected into claims the grader labeled *supported*; only faults the grader confirms as "
        "unsupported or contradicted are kept as ground truth.",
        "",
        "| Fault | Confirmed cases | Caught (unsupported/contradicted) | Caught incl. *partial* |",
        "|---|--:|--:|--:|",
    ]
    for kind, label in (("citation_swap", "Citation swapped to unrelated evidence"),
                        ("fact_edit", "Minimal factual edit (number, entity, direction)")):
        d = det[kind]
        lines.append(f"| {label} | {d['cases']} | {pct(d['recall'])} | {pct(d['recall_incl_partial'])} |")
    lines += ["", f"False alarms on untouched supported claims: {pct(det['false_alarm_rate'])} "
                  f"({det['originals']} claims).", ""]
    (RESULTS / "faithfulness_eval.md").write_text("\n".join(lines))
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("steps", nargs="+", choices=("runs", "grade", "detect", "report"))
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--grader-concurrency", type=int, default=2, help="gpt-4o is rate-limited harder")
    parser.add_argument("--detect-n", type=int, default=150)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--only", help="comma-separated question ids (runs step only)")
    args = parser.parse_args()
    load_env()
    DATA.mkdir(parents=True, exist_ok=True)
    for step in args.steps:
        if step == "runs":
            asyncio.run(step_runs(args.concurrency, set(args.only.split(",")) if args.only else None))
        elif step == "grade":
            asyncio.run(step_grade(args.grader_concurrency))
        elif step == "detect":
            asyncio.run(step_detect(args.detect_n, args.seed, args.grader_concurrency))
        else:
            step_report()


if __name__ == "__main__":
    main()
