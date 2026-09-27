# 设计

[← 返回 README](../../README.zh-CN.md) · [English](../design.md)

## 租约、fencing 与工具层

### 租约和 fencing 的工作方式

1. worker 通过获取租约来认领一次运行（使用 `BEGIN IMMEDIATE`，两个 worker 不可能同时抢到）。租约包含 owner、过期时间和一个 **fencing token**，每换一次 owner，token 就加一。
2. worker 驱动运行期间，心跳每隔 `ttl / 3` 续期一次租约。
3. 如果 worker 挂了，就没有人续期。租约过期后，另一个 worker 以 token + 1 认领这次运行，并从最后一个检查点继续。
4. 如果旧 worker 只是被*暂停*了（GC 停顿、虚拟机挂起、事件循环阻塞），之后醒来，它的下一个检查点会以旧 token 为条件写入，因而被拒绝并抛出 `LeaseLost`，于是它放弃这次运行，而不会覆盖更新的进度。
5. 一次反复失败的运行（比如 API key 错误）在被认领 3 次后标记为 `failed`，而不是无限重试。

WAL 模式的 SQLite 可以安全地被同一台主机上的多个进程使用（同一个 Docker 数据卷），但不适用于跨机器共享的网络文件系统。worker 分布在多台主机时，请使用 Postgres 存储（`--db postgresql+psycopg://…`），认领时使用 `SELECT … FOR UPDATE SKIP LOCKED`。


### 工具层如何处理崩溃

每个工具都声明自己是否有副作用、接收端是否按幂等键去重。工具层在每次调用**之前**写入一条 `pending` 意图，调用**之后**写入结果。恢复时：

| 找到的记录 | 工具 | 处理方式 |
|---|---|---|
| `done` | 任意 | 重放记录的结果，不再调用 |
| `pending` | 只读（搜索、抓取） | 再调用一次；重复无害 |
| `pending` | 有副作用 + 幂等键（webhook） | **用同一个键**再调用一次；接收端丢弃重复请求 |
| `pending` | 有副作用，不支持幂等 | **不**重试；运行进入 `needs_reconciliation`，等待人工确认该操作是否已经发生 |

单靠本地记录，无法让外部副作用做到恰好一次：外部调用和本地写入是两个独立的系统。意图日志让每一次可能的重复都*可被发现*，幂等键则让接收端把它变得*无害*。


## 🗂️ 目录结构

```text
src/deeptrace_agent/
├── runtime.py       # 规划 → 执行 → 写报告 → 核对 → 推送 状态机，人工控制
├── planner.py       # 任务 DAG 规划与重新规划，带自我纠错
├── executor.py      # 单个任务的研究、并发分层执行、网页内容补充
├── tools.py         # 带预写意图的工具层；搜索、抓取与 webhook 工具
├── reporter.py      # 大纲、按章节分配证据、压缩、定向修复
├── faithfulness.py  # 论断抽取，以及论断对照证据的评审
├── verifier.py      # 确定性检查 + 可选的 LLM 审阅
├── ledger.py        # 去重的证据账本
├── budget.py        # 预算，以及计量用的 LLM 包装
├── metrics.py       # 单次运行指标、汇总、Prometheus 输出
├── store.py         # SQLAlchemy 存储：检查点、租约 + fencing、控制标记、事件、工具意图
├── schema.py        # 与 Alembic 迁移（migrations/）共用的表定义
├── agent.py         # 研究单个子问题的 LangGraph ReAct agent
├── retrieval.py     # sentence-transformer 向量存入 Faiss；Reciprocal Rank Fusion
├── graph.py         # Neo4j 知识图谱：抽取、导入、基于实体链接的检索
├── factory.py       # 根据 CLI / 环境变量配置构建检索器和研究器
├── worker.py        # 认领无主的运行并驱动执行
├── server.py        # FastAPI 应用
├── search.py        # 本地 BM25 语料与 Tavily 网页搜索
├── views.py         # 给控制台用的运行 JSON 视图
├── llm.py           # OpenAI 兼容客户端，感知限流的重试，健壮的 JSON 提取
├── env.py           # DEEPTRACE_* 配置
├── prompts.py
└── cli.py
evals/
├── crash_recovery.py   # 故障注入基准；结果在 evals/results/
├── retrieval_eval.py   # 在人工标注查询上比较 BM25、向量、图谱与混合检索
├── faithfulness_eval.py  # 在固定版本 Wikipedia 文章上的真实模型评测
├── routing_ablation.py   # 按章节分配证据 vs. 每节全部证据
├── fetch_wikipedia.py  # 构建固定版本的评测语料
├── deploy_drill.sh     # API + 2 个 worker，其中一个在运行中被 SIGKILL
└── mock_llm_server.py  # 用于端到端检查的脚本化 OpenAI 兼容服务
web/                    # React + TypeScript 控制台（Vite）；实时模式与回放演示，支持中英文
Dockerfile · docker-compose.yml
```

## 🗺️ 路线图

- [x] 在固定语料上用真实模型评测（[结果](../../evals/results/faithfulness_eval.md)）
- [ ] 也修复*部分支持*的论断：已通过 `repair_partial=True` 实现（默认关闭）；另一组问题上的评测（[`evals/partial_repair_eval.py`](../../evals/partial_repair_eval.py)，24 个新问题）已写好但尚未运行
- [ ] 基于新信息量的研究轮次提前停止
- [ ] 跨来源的矛盾检测
- [x] Postgres 存储（`FOR UPDATE SKIP LOCKED`），支持多台主机上的 worker
- [ ] 段落级检索基准（由 LLM 针对具体事实生成问题）
- [ ] 用真实模型对 LangGraph agent 研究器与固定流程做 A/B 评测
