# Design

[← Back to README](../README.md)

## Leases, fencing and the tool layer

### How leases and fencing work

1. A worker claims a run by taking its lease (`BEGIN IMMEDIATE`, so two workers cannot both win). A lease has an owner, an expiry and a **fencing token** that increases with every change of owner.
2. While driving the run, a heartbeat renews the lease every `ttl / 3`.
3. If the worker dies, nobody renews the lease. Once it expires, another worker claims the run with token + 1 and resumes from the last checkpoint.
4. If the old worker was only *paused* (GC pause, suspended VM, blocked event loop) and wakes up later, its next checkpoint is conditioned on its old token and is rejected with `LeaseLost`, so it abandons the run instead of overwriting newer progress.
5. A run that keeps failing (for example because of a bad API key) is marked `failed` after 3 claims instead of being retried forever.

SQLite in WAL mode is safe for several processes on one host (one Docker volume), but not on a network filesystem shared across machines. Scaling beyond one host would mean moving the store to Postgres (`SELECT … FOR UPDATE SKIP LOCKED`).


### How the tool layer handles crashes

Every tool declares whether it has side effects and whether its receiver deduplicates by idempotency key. The runner writes a `pending` intent **before** each call and the result **after** it. On resume:

| Record found | Tool | What happens |
|---|---|---|
| `done` | any | The recorded result is replayed; nothing is called |
| `pending` | read-only (search, fetch) | Called again; a repeat is harmless |
| `pending` | side effects + idempotency key (webhook) | Called again **with the same key**; the receiver drops the duplicate |
| `pending` | side effects, no idempotency support | **Not** retried; the run is held as `needs_reconciliation` until a human says whether it happened |

No local bookkeeping can make an external side effect exactly-once on its own: the call and the local write are two separate systems. The intent log makes every possible duplicate *detectable*, and idempotency keys let the receiver make it *harmless*.


## 🗂️ Layout

```text
src/deeptrace_agent/
├── runtime.py       # the Plan → Execute → Report → Verify → Deliver state machine, human control
├── planner.py       # task DAG planning and replanning, with self-correction
├── executor.py      # per-task research, concurrent waves, page enrichment
├── tools.py         # tool runner with write-ahead intents; search, fetch and webhook tools
├── reporter.py      # outline, per-section evidence routing, condensing, targeted repair
├── faithfulness.py  # claim extraction and the claim-vs-evidence judge
├── verifier.py      # deterministic checks + optional LLM critic
├── ledger.py        # deduplicating evidence ledger
├── budget.py        # budgets and the metered LLM wrapper
├── metrics.py       # per-run metrics, aggregation, Prometheus rendering
├── store.py         # SQLAlchemy store: checkpoints, leases + fencing, control flags, events, tool intents
├── schema.py        # table definitions shared with the Alembic migrations (migrations/)
├── agent.py         # LangGraph ReAct researcher for one sub-question
├── retrieval.py     # sentence-transformer embeddings in Faiss; Reciprocal Rank Fusion
├── graph.py         # Neo4j knowledge graph: extraction, loading, entity-linked retrieval
├── factory.py       # builds retrieval and researcher from CLI/env configuration
├── worker.py        # claims unowned runs and drives them
├── server.py        # FastAPI app
├── search.py        # local BM25 corpus and Tavily web search
├── views.py         # JSON view of a run for the console
├── llm.py           # OpenAI-compatible client, rate-limit aware retries, robust JSON extraction
├── env.py           # DEEPTRACE_* configuration
├── prompts.py
└── cli.py
evals/
├── crash_recovery.py   # fault-injection benchmark; results in evals/results/
├── retrieval_eval.py   # BM25 vs. vectors vs. graph vs. hybrid on hand-labelled queries
├── faithfulness_eval.py  # real-model evaluation on pinned Wikipedia articles
├── routing_ablation.py   # per-section evidence routing vs. all-evidence sections
├── fetch_wikipedia.py  # builds the pinned evaluation corpus
├── deploy_drill.sh     # API + 2 workers, one SIGKILLed mid-run
└── mock_llm_server.py  # scripted OpenAI-compatible server for end-to-end checks
web/                    # React + TypeScript console (Vite); live mode and replay demo
Dockerfile · docker-compose.yml
```

## 🗺️ Roadmap

- [x] Evaluation on a fixed corpus with real models ([results](../evals/results/faithfulness_eval.md))
- [ ] Repair *partial* claims too: implemented behind `repair_partial=True` (off by default); the held-out evaluation ([`evals/partial_repair_eval.py`](../evals/partial_repair_eval.py), 24 new questions) is written but has not been run yet
- [ ] Novelty-based early stopping for research rounds
- [ ] Contradiction detection across sources
- [x] Postgres store (`FOR UPDATE SKIP LOCKED`) for workers on several hosts
- [ ] Passage-level retrieval benchmark (LLM-generated questions about specific facts)
- [ ] A/B evaluation of the LangGraph agent researcher against the fixed pipeline with real models
