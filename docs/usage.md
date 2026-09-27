# Usage

[← Back to README](../README.md) · [中文](zh/usage.md)

## 🚀 Quick start

```bash
git clone https://github.com/limengge426/deeptrace-research-agent.git
cd deeptrace-research-agent
pip install -e ".[dev]"
```

Point it at any OpenAI-compatible model (or put these lines in a `.env` file):

```bash
export DEEPTRACE_API_KEY=sk-...
export DEEPTRACE_MODEL=gpt-4o-mini                      # or deepseek-chat, qwen-plus, ...
export DEEPTRACE_BASE_URL=https://api.openai.com/v1     # or your provider's endpoint
```

**Offline**: research a local folder of `.md` / `.txt` files:

```bash
deeptrace run "Are heat pumps worth it in cold climates?" --corpus examples/corpus
```

**Web**: search with [Tavily](https://tavily.com); the top hits of each query are fetched in full:

```bash
export TAVILY_API_KEY=tvly-...
deeptrace run "What changed in EU AI regulation in 2025?" --critic
```

The report is written to `reports/<run_id>.md`, with a **Sources** section listing every cited evidence id and a **Limitations** section for anything that could not be verified.

### Inspect and control runs

```bash
deeptrace ask <run_id> "What about running costs?"   # follow-up question in the run's conversation
deeptrace thread <run_id>                 # the turns of that conversation

deeptrace show <run_id>                   # event timeline + metrics per LLM purpose, stage and tool
deeptrace list

deeptrace run "..." --approve-plan        # stop after planning; prints the proposed DAG
deeptrace approve <run_id> [--plan edited.json]
deeptrace resume <run_id>                 # continue after approval, a pause, a halt or a crash

deeptrace pause <run_id>                  # applied at the next step boundary
deeptrace cancel <run_id>

deeptrace run "..." --webhook https://hooks.example/report   # deliver the finished report
deeptrace resolve <run_id>                                    # list interrupted side-effecting calls
deeptrace resolve <run_id> <key> --outcome done|retry         # reconcile one of them
```

### All run options

| Option | Default | Meaning |
|---|---|---|
| `--corpus DIR` | `$DEEPTRACE_CORPUS` | Search local files instead of the web |
| `--max-tasks` | 5 | Sub-questions in the initial plan |
| `--concurrency` | 3 | Tasks researched in parallel |
| `--fetch-pages` | 2 | Web hits per query fetched in full |
| `--max-tokens` | 250,000 | Token budget for the whole run |
| `--max-tool-calls` | 60 | Tool call budget (searches and fetches) |
| `--max-minutes` | 30 | Wall-clock budget (summed across resumes) |
| `--max-replans` | 2 | Extra research rounds for evidence gaps |
| `--critic` | off | Ask the LLM to review the report for missing aspects |
| `--lease-ttl` | 30 | Seconds before a dead worker's run is taken over |
| `--retrieval` | `bm25` | Over a local corpus: `bm25`, `vector`, `hybrid`, `graph` or `hybrid+graph` |
| `--researcher` | `pipeline` | `pipeline` (fixed queries → search → summarise) or `agent` (LangGraph ReAct agent) |
| `--db` | `$DEEPTRACE_DB` or `deeptrace.db` | SQLite file for runs |


### Retrieval, knowledge graph and storage

```bash
pip install -e ".[rag,graph,agent]"                     # Faiss + sentence-transformers, Neo4j driver, LangGraph

deeptrace run "..." --corpus docs/ --retrieval hybrid   # BM25 + embeddings (index cached in docs/.deeptrace_index)
deeptrace graph build --corpus docs/                    # load the corpus into Neo4j (NEO4J_URI/USER/PASSWORD)
deeptrace run "..." --corpus docs/ --retrieval hybrid+graph --researcher agent

deeptrace db upgrade --db postgresql+psycopg://user:pass@host/deeptrace   # Alembic migrations (also run on startup)
```

`graph build` extracts entities without a model by default (article titles plus recurring proper names, related when they appear in the same passage). `--extractor llm` asks the model for typed relations instead.

### Web console

```bash
cd web && npm install && npm run build && cd ..     # builds into the Python package
deeptrace serve --corpus examples/corpus             # open http://127.0.0.1:8000
```

The interface is in English; add `?lang=zh` to the URL (or use the link at the bottom of the sidebar) for Chinese.

| View | What it shows |
|---|---|
| **Live** | The task graph, laid out by dependency level, updates as tasks start and finish, next to a readable event log. The log shows worker takeovers ("taken over by worker … lease token 2"), repairs and replans. |
| **Plan review** | With *"Let me review the plan"* ticked, the run stops after planning; edit, add or remove sub-questions and dependencies, then approve. |
| **Report** | Sentences are coloured by the claim judge (supported / partial / unsupported / contradicted). Hover for the judge's reason; click `[E3]` to open the source text and every sentence that cites it. |
| **Follow-ups** | Below a finished report, ask a follow-up. The conversation's turns are shown above the stage bar; each follow-up shows how it was understood and whether it was answered from existing evidence or needed more research. |
| **Metrics** | Tokens per LLM purpose, time per stage, evidence placed into prompts, tool calls and cache hits. |

For development, `npm run dev` in `web/` proxies API calls to a server on port 8000. The same console runs as a static **replay demo** (`npm run build:demo`), published to GitHub Pages by [`pages.yml`](../.github/workflows/pages.yml). Its data comes from [`evals/export_demo.py`](../evals/export_demo.py), which exports real runs with worker names anonymised and evidence linked to the exact Wikipedia revisions.

### Run as a service

```bash
pip install -e ".[server]"
deeptrace serve --corpus examples/corpus            # API + embedded worker on :8000
```

| Endpoint | |
|---|---|
| `POST /runs` `{"question", "approve_plan"?, "webhook_url"?}` | Queue a run (202) |
| `GET /runs/{id}` | Status, tasks, evidence count, usage, delivery |
| `GET /runs/{id}/events` | Live Server-Sent Events; reconnect with `Last-Event-ID` to resume the stream |
| `GET /runs/{id}/report` | The Markdown report once the run is done |
| `GET /runs/{id}/metrics` | LLM usage per purpose, tool calls and cache hits, time per stage, faithfulness |
| `POST /runs/{id}/followups` `{"question"}` | Ask a follow-up on a finished run (202); 409 while the conversation has an unfinished turn |
| `GET /threads/{id}` | The runs of one conversation, oldest first |
| `POST /runs/{id}/pause` · `/cancel` · `/resume` | Human control |
| `POST /runs/{id}/plan/approve` `{"tasks"?}` | Approve the proposed plan, or replace it with an edited DAG |
| `GET /runs/{id}/tool-calls/pending` | Side-effecting calls interrupted mid-flight |
| `POST /runs/{id}/tool-calls/{key}/resolve` `{"outcome": "done" \| "retry"}` | Reconcile one |
| `GET /runs/{id}/state` | Everything the console shows: task graph, evidence, report annotated sentence by sentence, metrics |
| `GET /metrics` `?format=prometheus` | Aggregates across runs (status counts, P50/P95, cache hit rate, faithfulness) |

Interactive docs are served at `/docs`.

**API and workers as separate processes.** The API only writes runs to the database, and any number of workers execute them:

```bash
deeptrace serve --no-worker &
deeptrace worker &
deeptrace worker &
```

**Docker**: the same topology, one API plus two worker replicas on a shared volume:

```bash
cp .env.example .env    # add your model key
docker compose up --build
```

```mermaid
flowchart LR
    C([Client]) -->|POST /runs · SSE · control| A[FastAPI]
    A -->|enqueue · control flags| DB[(SQLite · WAL<br/>runs · leases · events · tool intents)]
    W1[Worker 1] <-->|claim · heartbeat · fenced checkpoints| DB
    W2[Worker 2] <-->|claim · heartbeat · fenced checkpoints| DB
    W1 & W2 --> LLM[[LLM]] & S[[Search · Fetch]] & H[[Webhook]]
```

### Use as a library

```python
import asyncio
from deeptrace_agent import Budget, LocalCorpusSearch, OpenAICompatLLM, ResearchRuntime, RunStore

runtime = ResearchRuntime(
    OpenAICompatLLM.from_env(),
    LocalCorpusSearch("examples/corpus"),
    RunStore("runs.db"),
    budget=Budget(max_tokens=100_000),
    judge_llm=None,          # or a separate (stronger) model for the faithfulness judge
    report_mode="sections",  # or "single" for the one-shot baseline
)
result = asyncio.run(runtime.start("Are heat pumps worth it in cold climates?"))
print(result.status, result.markdown)
```
