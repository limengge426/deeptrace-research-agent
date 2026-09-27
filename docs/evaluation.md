# Evaluation

[← Back to README](../README.md) · [中文](zh/evaluation.md)

## 🧪 Tests

```bash
pytest
```

105 tests, using a scripted LLM and a fake search provider, run offline in a few seconds (the Postgres and Neo4j tests are skipped unless those services are available; CI runs them). They cover:

- **Planning and repair**: parallel waves, dependency hand-off, citation repair, replanning after evidence gaps, one task failing without killing its siblings.
- **Faithfulness**: claim extraction (including Chinese text and trailing citations), judge fail-closed behaviour, and repair of only the section holding a bad claim.
- **Context engineering**: outline coverage repair, per-section evidence routing, condensing over-budget evidence.
- **Crash recovery**: resuming after a hard crash without repeating searches, budget halt and resume.
- **Workers and leases**: two workers splitting a queue, orphaned-run takeover, and **a stalled worker fenced off after another worker takes over**.
- **Human control**: cancel, pause and resume, plan approval with edited DAGs.
- **Tool layer**: replay, retry with the same key, reconciliation holds, page fetching, idempotent delivery.
- **API, SSE and metrics**.

CI also runs a **deployment drill** ([`evals/deploy_drill.sh`](../evals/deploy_drill.sh)): an API and two worker processes against a mock OpenAI-compatible server, with one worker `SIGKILL`ed while it holds a lease; every run must still finish exactly once. A separate job builds the Docker image and completes a run inside the container.

## 💥 Crash-recovery benchmark

[`evals/crash_recovery.py`](../evals/crash_recovery.py) hard-kills (`SIGKILL`) a worker process at **every** LLM call, search call, checkpoint write and webhook delivery of a scripted run. The run includes a section repair, a replan and delivery of the report to a webhook, so every stage is exercised. Fresh processes then resume the run: each one waits for the killed process's lease to expire and takes the run over with a new fencing token. Every executed search and every webhook send is written to an fsync'ed side-effect log, so duplicates are counted across processes. The simulated receiver honors `Idempotency-Key`, as Stripe-style APIs do.

| Scenario | Crashed runs | Recovered | Report byte-identical to uncrashed run | Runs with a repeated search | Report delivered exactly once |
|---|--:|--:|--:|--:|--:|
| Kill before an LLM call | 21 | 21/21 | 21/21 | 0 | 21/21 |
| Kill before a search | 8 | 8/8 | 8/8 | 0 | 8/8 |
| Kill **after** a search, before its result is recorded | 8 | 8/8 | 8/8 | 8 | 8/8 |
| Kill before a checkpoint write | 13 | 13/13 | 13/13 | 0 | 13/13 |
| Kill before / **after** the webhook POST | 2 | 2/2 | 2/2 | 0 | 2/2 (1 re-send, deduplicated) |
| **All single crashes** | **52** | **52/52** | **52/52** | 8 | **52/52** |
| 2–3 crashes in the same run (random) | 30 | 30/30 | 30/30 | 5 | 30/30 (9 re-sends, deduplicated) |

On average, recovery re-executes **1.3 LLM calls** per crash (2.0 when a run crashes 2–3 times).

**What the numbers mean:** read-only searches are *at-least-once*: a crash between a search and the recording of its result repeats that search, which is harmless. The side-effecting delivery is also sent again after such a crash, but with the same idempotency key, so the receiver processes it **exactly once in 82/82 crashed runs**. Replacing the stable key with a random one makes the same benchmark report a duplicate delivery. Usage counters (tokens, calls) are checkpointed with the run, so work done after the last checkpoint of a crashed process is not counted.

```bash
python evals/crash_recovery.py    # 83 scenarios, ~1 min; writes evals/results/
```

## 🎯 Real-model evaluation

[`evals/faithfulness_eval.py`](../evals/faithfulness_eval.py) runs 24 research questions over 40 Wikipedia articles pinned to fixed revisions ([manifest](../evals/data/wikipedia_manifest.json)). The writer and the in-loop judge are `gpt-4o-mini`. An independent `gpt-4o` grader labels every cited claim of every final report against the full text of its evidence. Full results: [`evals/results/faithfulness_eval.md`](../evals/results/faithfulness_eval.md).

**The claim judge catches injected faults.** Faults were injected into claims the grader had labeled *supported*, and only faults the grader confirmed were kept:

| Injected fault | Cases | Caught by the in-loop judge (95% CI) |
|---|--:|--:|
| Citation swapped to unrelated evidence | 132 | **99.2%** (95.8–99.9) |
| Minimal factual edit (a number, entity or direction) | 54 | **100%** flagged (93.4–100); 74.1% as unsupported/contradicted, the rest as *partial* |
| False alarms on untouched supported claims | 129 | **0.0%** (0.0–2.9) |

**Sectioned writing bounds the context of every call.** Compared with writing the whole report in one call, the largest report-stage call was smaller for **24/24** questions (median **−41%**), at the cost of more calls in total.

**Per-section evidence routing saves tokens and context.** [`evals/routing_ablation.py`](../evals/routing_ablation.py) compares routing with the natural alternative, where every section sees all evidence ([results](../evals/results/routing_ablation.md)), over 24 questions with paired bootstrap 95% CIs:

| | Routed vs. all-evidence sections |
|---|--:|
| LLM tokens per run | **−19.9%** (−13.3 to −25.7) |
| Report-stage tokens | **−37.0%** (−28.7 to −44.0) |
| Cumulative evidence exposure in report prompts | **−61.6%** (−54.7 to −66.9), smaller for 23/24 questions |
| Findings cited by the final report | **100%** in both (enforced by the verifier) |
| Unsupported or contradicted claims | 2.4% vs. 3.2%: no increase (−0.8 points, CI −4.1 to +1.8) |
| Claims graded *partial* rather than fully supported | **+6.4 points** (CI +3.2 to +9.6) |

The trade-off: with less evidence in view, sections more often state a claim slightly beyond what its source says (*partial*), without producing more unsupported or contradicted claims.

**Not yet shown: better final reports.** On this clean corpus the baseline already has few unsupported claims (3.3%). With the judge in the loop that fell to 2.2%, but the paired bootstrap interval (−5.3 to +4.0 points) includes zero. The detection results point to the cause: the loop only repairs *unsupported* and *contradicted* claims, while a quarter of injected factual edits are labeled *partial*. Repairing *partial* claims is the next change, to be evaluated on held-out questions.

```bash
python evals/fetch_wikipedia.py                          # 40 pinned articles, ~2 MB (text not committed: CC BY-SA)
python evals/faithfulness_eval.py runs grade detect report
```

## 🔎 Retrieval

[`evals/retrieval_eval.py`](../evals/retrieval_eval.py) compares retrieval methods over the same 40 pinned Wikipedia articles (2,868 chunks), with hand-labelled gold articles ([labels](../evals/data/retrieval_labels.json), [results](../evals/results/retrieval_eval.md)). Embeddings: `all-MiniLM-L6-v2`.

| Method | 48 research questions: Recall@5 | Recall@10 | 40 paraphrased queries: Hit@5 | MRR@10 |
|---|--:|--:|--:|--:|
| BM25 | 0.920 | 0.972 | 1.000 | 0.927 |
| Dense vectors (Faiss) | 0.920 | 0.938 | 1.000 | **0.983** |
| Hybrid (BM25 + vectors) | 0.917 | 0.944 | 1.000 | 0.963 |
| Knowledge graph (Neo4j) | 0.906 | 0.955 | 0.600 | 0.523 |
| **Hybrid (BM25 + vectors + graph)** | **0.941** | **0.979** | 1.000 | 0.942 |

What this shows:
- **The three-way hybrid covers multi-topic questions best.** Many research questions span two or three articles, and the graph links them through shared entities.
- **Dense vectors rank paraphrases best.** For queries that avoid the article's own words, they put the right article first more often.
- **The graph alone is weak on paraphrases.** With no entity named in the query, it has nothing to start from, so it is useful only as part of a hybrid.

**Limitation:** with 40 articles on distinct topics, article-level retrieval is close to ceiling for every method, so the differences are small. A harder passage-level benchmark (LLM-generated questions about specific facts) is on the roadmap.
