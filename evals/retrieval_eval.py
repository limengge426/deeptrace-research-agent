"""Retrieval evaluation on the pinned Wikipedia corpus: BM25 vs. dense vectors vs. hybrid (vs. graph).

Two hand-labelled query sets (evals/data/retrieval_labels.json):
  questions    the 48 research questions, each labelled with the articles it needs
  paraphrases  one query per article, written without the article title's words

Metrics (a retrieved chunk counts for the article it comes from):
  Recall@k     share of gold articles with at least one chunk in the top k
  Hit@k        share of queries with at least one gold article in the top k
  MRR@10       mean reciprocal rank of the first gold chunk

    python evals/retrieval_eval.py                    # bm25, vector, hybrid
    python evals/retrieval_eval.py --graph            # adds the Neo4j graph search (needs Neo4j running)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from deeptrace_agent.retrieval import HybridSearch, SentenceTransformerEmbedder, VectorCorpusSearch  # noqa: E402
from deeptrace_agent.search import LocalCorpusSearch  # noqa: E402

DATA = ROOT / "evals" / "data"
RESULTS = ROOT / "evals" / "results"
CORPUS = DATA / "wikipedia"


def article(url: str) -> str:
    return url.split("#")[0].removesuffix(".md")


def load_queries() -> dict[str, list[tuple[str, list[str]]]]:
    labels = json.loads((DATA / "retrieval_labels.json").read_text())
    questions = {q["id"]: q["question"] for f in ("questions.json", "questions_heldout.json")
                 for q in json.loads((DATA / f).read_text())}
    return {
        "questions": [(questions[qid], gold) for qid, gold in labels["questions"].items()],
        "paraphrases": [(p["query"], p["gold"]) for p in labels["paraphrases"]],
    }


async def evaluate(provider, queries: list[tuple[str, list[str]]]) -> dict[str, float]:
    recall5 = recall10 = hit5 = mrr = 0.0
    for query, gold in queries:
        ranked = [article(h.url) for h in await provider.search(query, 10)]
        recall5 += len(set(gold) & set(ranked[:5])) / len(gold)
        recall10 += len(set(gold) & set(ranked[:10])) / len(gold)
        hit5 += any(a in gold for a in ranked[:5])
        first = next((i for i, a in enumerate(ranked) if a in gold), None)
        mrr += 1 / (first + 1) if first is not None else 0.0
    n = len(queries)
    return {"recall@5": recall5 / n, "recall@10": recall10 / n, "hit@5": hit5 / n, "mrr@10": mrr / n}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--graph", action="store_true", help="include the Neo4j graph search")
    args = parser.parse_args()

    started = time.time()
    bm25 = LocalCorpusSearch(CORPUS)
    embedder = SentenceTransformerEmbedder()
    vector = VectorCorpusSearch(CORPUS, embedder)
    print(f"{len(bm25.chunks)} chunks; vector index {'loaded from cache' if vector.built_from_cache else 'built'} "
          f"with {embedder.name} ({time.time() - started:.1f}s)")
    providers = {"bm25": bm25, "vector": vector, "hybrid (bm25+vector)": HybridSearch(bm25, vector)}
    if args.graph:
        from deeptrace_agent.graph import GraphSearch

        graph = GraphSearch.from_env()
        providers["graph"] = graph
        providers["hybrid (bm25+vector+graph)"] = HybridSearch(bm25, vector, graph)

    queries = load_queries()
    results = {name: {set_name: asyncio.run(evaluate(p, qs)) for set_name, qs in queries.items()}
               for name, p in providers.items()}

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = {"embedder": embedder.name, "chunks": len(bm25.chunks),
           "queries": {k: len(v) for k, v in queries.items()}, "results": results,
           "generated_at": time.strftime("%Y-%m-%d")}
    (RESULTS / "retrieval_eval.json").write_text(json.dumps(out, indent=2))

    lines = [
        "# Retrieval evaluation",
        "",
        f"{len(bm25.chunks)} chunks from 40 pinned Wikipedia articles. Embeddings: `{embedder.name}` in a Faiss "
        "inner-product index. Hybrid = Reciprocal Rank Fusion. Gold articles are hand-labelled "
        "([labels](../data/retrieval_labels.json)).",
        "",
    ]
    for set_name, n in out["queries"].items():
        desc = ("the 48 research questions" if set_name == "questions"
                else "one query per article, written without the article title's words")
        lines += [f"## {set_name.capitalize()} ({n} queries: {desc})", "",
                  "| Method | Recall@5 | Recall@10 | Hit@5 | MRR@10 |", "|---|--:|--:|--:|--:|"]
        for name, r in results.items():
            m = r[set_name]
            lines.append(f"| {name} | {m['recall@5']:.3f} | {m['recall@10']:.3f} | {m['hit@5']:.3f} | {m['mrr@10']:.3f} |")
        lines.append("")
    (RESULTS / "retrieval_eval.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
