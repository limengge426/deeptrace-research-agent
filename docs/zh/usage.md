# 使用

[← 返回 README](../../README.zh-CN.md) · [English](../usage.md)

## 🚀 快速开始

```bash
git clone https://github.com/limengge426/deeptrace-research-agent.git
cd deeptrace-research-agent
pip install -e ".[dev]"
```

指向任意 OpenAI 兼容模型（也可以把这几行写进 `.env` 文件）：

```bash
export DEEPTRACE_API_KEY=sk-...
export DEEPTRACE_MODEL=gpt-4o-mini                      # 或 deepseek-chat、qwen-plus 等
export DEEPTRACE_BASE_URL=https://api.openai.com/v1     # 或你的服务商地址
```

**离线**：研究一个本地的 `.md` / `.txt` 文件夹：

```bash
deeptrace run "Are heat pumps worth it in cold climates?" --corpus examples/corpus
```

**联网**：用 [Tavily](https://tavily.com) 搜索；每条查询排名靠前的结果会被抓取全文：

```bash
export TAVILY_API_KEY=tvly-...
deeptrace run "What changed in EU AI regulation in 2025?" --critic
```

报告写入 `reports/<run_id>.md`，其中 **Sources** 一节列出所有被引用的证据编号，**Limitations** 一节列出无法核实的内容。

### 查看和控制运行

```bash
deeptrace show <run_id>                   # 事件时间线 + 按 LLM 调用目的、阶段、工具统计的指标
deeptrace list

deeptrace run "..." --approve-plan        # 规划完成后停下，打印拟定的 DAG
deeptrace approve <run_id> [--plan edited.json]
deeptrace resume <run_id>                 # 在审批、暂停、预算耗尽或崩溃之后继续

deeptrace pause <run_id>                  # 在下一个步骤边界生效
deeptrace cancel <run_id>

deeptrace run "..." --webhook https://hooks.example/report   # 把完成的报告推送出去
deeptrace resolve <run_id>                                    # 列出被中断的有副作用调用
deeptrace resolve <run_id> <key> --outcome done|retry         # 核对其中一个
```

### 全部运行参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--corpus DIR` | `$DEEPTRACE_CORPUS` | 搜索本地文件而不是网页 |
| `--max-tasks` | 5 | 初始计划中的子问题数 |
| `--concurrency` | 3 | 并行研究的任务数 |
| `--fetch-pages` | 2 | 每条查询抓取全文的网页数 |
| `--max-tokens` | 250,000 | 整次运行的 token 预算 |
| `--max-tool-calls` | 60 | 工具调用预算（搜索与抓取） |
| `--max-minutes` | 30 | 墙钟时间预算（多次恢复累计） |
| `--max-replans` | 2 | 为补足证据额外进行的研究轮数 |
| `--critic` | 关 | 让 LLM 审阅报告是否漏掉了某些方面 |
| `--lease-ttl` | 30 | 挂掉的 worker 的运行在多少秒后被接管 |
| `--retrieval` | `bm25` | 本地语料的检索方式：`bm25`、`vector`、`hybrid`、`graph` 或 `hybrid+graph` |
| `--researcher` | `pipeline` | `pipeline`（固定流程：生成查询 → 搜索 → 总结）或 `agent`（LangGraph ReAct agent） |
| `--db` | `$DEEPTRACE_DB` 或 `deeptrace.db` | 存放运行记录的 SQLite 文件 |


### 检索、知识图谱与存储

```bash
pip install -e ".[rag,graph,agent]"                     # Faiss + sentence-transformers、Neo4j 驱动、LangGraph

deeptrace run "..." --corpus docs/ --retrieval hybrid   # BM25 + 向量（索引缓存在 docs/.deeptrace_index）
deeptrace graph build --corpus docs/                    # 把语料导入 Neo4j（NEO4J_URI/USER/PASSWORD）
deeptrace run "..." --corpus docs/ --retrieval hybrid+graph --researcher agent

deeptrace db upgrade --db postgresql+psycopg://user:pass@host/deeptrace   # Alembic 迁移（启动时也会自动执行）
```

`graph build` 默认不调用模型来抽取实体（文章标题加上反复出现的专有名词，在同一段落中出现即视为相关）。`--extractor llm` 则让模型抽取带类型的关系。

### 网页控制台

```bash
cd web && npm install && npm run build && cd ..     # 构建产物放进 Python 包
deeptrace serve --corpus examples/corpus             # 打开 http://127.0.0.1:8000
```

界面默认英文，地址加上 `?lang=zh`（例如 `http://127.0.0.1:8000/?lang=zh`）即为中文，侧边栏底部也可以切换。

| 视图 | 显示内容 |
|---|---|
| **实时** | 按依赖层级排布的任务图，随任务开始和结束实时更新，旁边是可读的事件日志。日志会显示 worker 接管（“已被 worker 接管，租约 token 2”）、修复和补充调研。 |
| **计划审阅** | 勾选 *“研究开始前先让我审阅计划”* 后，运行会在规划后停下；可以修改、添加或删除子问题及依赖，然后批准。 |
| **报告** | 句子按论断评审的结果着色（完全支持 / 部分支持 / 不支持 / 矛盾）。悬停可看评审理由；点击 `[E3]` 打开来源原文以及所有引用它的句子。 |
| **指标** | 按 LLM 调用目的统计的 token、各阶段耗时、放入 prompt 的证据量、工具调用与缓存命中。 |

开发时，在 `web/` 下运行 `npm run dev`，API 请求会被代理到 8000 端口的服务。同一个控制台也可以作为静态的**回放演示**运行（`npm run build:demo`），由 [`pages.yml`](../../.github/workflows/pages.yml) 发布到 GitHub Pages。演示数据来自 [`evals/export_demo.py`](../../evals/export_demo.py)，它导出真实的运行记录，把 worker 名称匿名化，并把证据链接到确切的 Wikipedia 版本。

### 作为服务运行

```bash
pip install -e ".[server]"
deeptrace serve --corpus examples/corpus            # API + 内嵌 worker，端口 8000
```

| 接口 | |
|---|---|
| `POST /runs` `{"question", "approve_plan"?, "webhook_url"?}` | 创建一次运行（202） |
| `GET /runs/{id}` | 状态、任务、证据数量、用量、推送情况 |
| `GET /runs/{id}/events` | 实时 Server-Sent Events；断线后带 `Last-Event-ID` 重连即可接着收 |
| `GET /runs/{id}/report` | 运行完成后的 Markdown 报告 |
| `GET /runs/{id}/metrics` | 按调用目的统计的 LLM 用量、工具调用与缓存命中、各阶段耗时、忠实度 |
| `POST /runs/{id}/pause` · `/cancel` · `/resume` | 人工控制 |
| `POST /runs/{id}/plan/approve` `{"tasks"?}` | 批准拟定的计划，或用修改后的 DAG 替换 |
| `GET /runs/{id}/tool-calls/pending` | 执行中途被中断的有副作用调用 |
| `POST /runs/{id}/tool-calls/{key}/resolve` `{"outcome": "done" \| "retry"}` | 核对其中一个 |
| `GET /runs/{id}/state` | 控制台显示的全部内容：任务图、证据、逐句标注的报告、指标 |
| `GET /metrics` `?format=prometheus` | 跨运行汇总（状态计数、P50/P95、缓存命中率、忠实度） |

交互式接口文档在 `/docs`。

**API 与 worker 分成独立进程。** API 只负责把运行写入数据库，任意数量的 worker 负责执行：

```bash
deeptrace serve --no-worker &
deeptrace worker &
deeptrace worker &
```

**Docker**：同样的拓扑，一个 API 加两个 worker 副本，共享一个数据卷：

```bash
cp .env.example .env    # 填入你的模型 key
docker compose up --build
```

```mermaid
flowchart LR
    C([客户端]) -->|POST /runs · SSE · 控制| A[FastAPI]
    A -->|入队 · 控制标记| DB[(SQLite · WAL<br/>运行 · 租约 · 事件 · 工具意图)]
    W1[Worker 1] <-->|认领 · 心跳 · 带 fencing 的检查点| DB
    W2[Worker 2] <-->|认领 · 心跳 · 带 fencing 的检查点| DB
    W1 & W2 --> LLM[[LLM]] & S[[搜索 · 抓取]] & H[[Webhook]]
```

### 作为库使用

```python
import asyncio
from deeptrace_agent import Budget, LocalCorpusSearch, OpenAICompatLLM, ResearchRuntime, RunStore

runtime = ResearchRuntime(
    OpenAICompatLLM.from_env(),
    LocalCorpusSearch("examples/corpus"),
    RunStore("runs.db"),
    budget=Budget(max_tokens=100_000),
    judge_llm=None,          # 或者给忠实度评审单独配一个（更强的）模型
    report_mode="sections",  # 或 "single"，即一次写完的基线
)
result = asyncio.run(runtime.start("Are heat pumps worth it in cold climates?"))
print(result.status, result.markdown)
```
