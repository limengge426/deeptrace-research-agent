"""Record real runs and export them for the web console's replay demo (GitHub Pages).

Runs the given questions with the real model over the pinned Wikipedia corpus, then writes
``web/public/demo/<run_id>.json`` (events + final state) and ``web/public/demo/index.json``.

Exported data is sanitised: worker names (which contain the host name) are replaced, and
evidence links point at the exact Wikipedia revision the text came from (CC BY-SA 4.0).

    python evals/export_demo.py en1 cp1 hi1            # record new runs (uses the API)
    python evals/export_demo.py --from-eval en1 bm3     # reuse runs from the evaluation (no API calls)

With --from-eval, sentence colours come from the independent gpt-4o grader of the evaluation,
and the console says so.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import faithfulness_eval as fe  # noqa: E402
from deeptrace_agent import ResearchRuntime, RunStore  # noqa: E402
from deeptrace_agent.search import LocalCorpusSearch  # noqa: E402
from deeptrace_agent.faithfulness import annotate_report  # noqa: E402
from deeptrace_agent.views import run_state  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "public" / "demo"
DB = fe.DATA / "demo_runs.db"


def _wikipedia_links() -> dict[str, str]:
    manifest = json.loads((fe.DATA / "wikipedia_manifest.json").read_text())
    links = {}
    for a in manifest["articles"]:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", a["title"]).strip("_").lower()
        links[f"{a['topic']}__{slug}.md"] = f"https://en.wikipedia.org/w/index.php?oldid={a['revid']}"
    return links


def _sanitize(obj, owners: dict[str, str]):
    """Replace worker names (host:pid:nonce) with neutral ones, everywhere in the payload."""
    if isinstance(obj, dict):
        return {k: (owners.setdefault(v, f"worker-{len(owners) + 1}:{1000 + len(owners)}:demo")
                    if k == "owner" and isinstance(v, str) else _sanitize(v, owners)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(x, owners) for x in obj]
    return obj


def export(store: RunStore, run_id: str, links: dict[str, str], grader_verdicts: list[dict] | None = None) -> dict:
    owners: dict[str, str] = {}
    events = [{"id": e.id, "ts": e.ts, "kind": e.kind, "payload": _sanitize(e.payload, owners)}
              for e in store.events(run_id)]
    state = run_state(store, run_id)
    state["lease"] = None
    if grader_verdicts is not None:
        report = store.load(run_id).report
        state["report"]["sections"] = annotate_report(report, grader_verdicts)
        state["verdict_source"] = f"independent {fe.GRADER_MODEL} grader"
    for ev in state["evidence"]:
        page = ev["url"].split("#")[0]
        if page in links:
            ev["url"] = links[page]
    record = {"id": run_id, "question": state["question"], "events": events, "state": state}
    (OUT / f"{run_id}.json").write_text(json.dumps(record, ensure_ascii=False))
    return {"id": run_id, "question": state["question"], "status": state["status"]}


async def record(qids: list[str]) -> list[str]:
    questions = {q["id"]: q for q in json.loads((fe.DATA / "questions.json").read_text())}
    questions.update({q["id"]: q for q in json.loads((fe.DATA / "questions_heldout.json").read_text())})
    writer = fe.llm(fe.WRITER_MODEL)
    store = RunStore(DB)
    runtime = ResearchRuntime(writer, LocalCorpusSearch(fe.DATA / "wikipedia"), store, owner="demo")
    results = await asyncio.gather(*(runtime.start(questions[q]["question"]) for q in qids))
    for q, r in zip(qids, results):
        print(f"  {q}: {r.run_id} {r.status} repairs={r.state.repairs} replans={r.state.replans} "
              f"faithfulness={[f['support_rate'] for f in r.state.faithfulness]}")
    await writer.aclose()
    store.close()
    return [r.run_id for r in results]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("questions", nargs="*", help="question ids to record (from the eval question sets)")
    parser.add_argument("--from-eval", action="store_true",
                        help="export the evaluation's sections+judge runs for these question ids (no API calls)")
    args = parser.parse_args()
    fe.load_env()
    OUT.mkdir(parents=True, exist_ok=True)
    links = _wikipedia_links()
    if args.from_eval:
        runs = {r["qid"]: r for r in fe.read_jsonl(fe.RUNS) if r["config"] == "sections+judge" and r["status"] == "done"}
        grades = {g["run_id"]: g["verdicts"] for g in fe.read_jsonl(fe.GRADES)}
        store = RunStore(fe.DB)
        index = [export(store, runs[q]["run_id"], links, grades[runs[q]["run_id"]]) for q in args.questions]
    else:
        run_ids = asyncio.run(record(args.questions))
        store = RunStore(DB)
        index = [export(store, rid, links) for rid in run_ids]
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2))
    (OUT / "README.md").write_text(
        "Recorded DeepTrace runs replayed by the web console demo.\n\n"
        "Evidence text in these files is excerpted from Wikipedia and licensed under CC BY-SA 4.0; each evidence\n"
        "item links to the exact article revision it came from. Everything else is under the repository's MIT license.\n"
    )
    print(f"exported {len(index)} runs to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
