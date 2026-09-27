<div align="center">

# 🔍 DeepTrace

**一个容错的深度研究 agent，报告里的每条论断都能追溯到来源。**

[English](README.md) · 简体中文

[![CI](https://github.com/limengge426/deeptrace-research-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/limengge426/deeptrace-research-agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/agent-LangGraph-1C3C3C)
![License](https://img.shields.io/badge/license-MIT-blue)

**[▶ 在线演示](https://limengge426.github.io/deeptrace-research-agent/?lang=zh)** · [功能](docs/zh/features.md) · [评测](docs/zh/evaluation.md) · [使用](docs/zh/usage.md) · [设计](docs/zh/design.md)

</div>

<p align="center">
  <img src="docs/console-live.png" width="49%"> <img src="docs/console-report.png" width="49%">
</p>

大多数深度研究 agent 写完报告就默认它是对的。**DeepTrace 把每一句话都当作需要通过检查的论断。** 每个来源都有一个固定的证据编号；一个评审模型会读完每条被引用来源的全文，标出来源不支持的论断。没通过的章节会被重写，证据不足的地方会触发补充调研。模型外面包着一层为故障而设计的执行框架：检查点、带 fencing token 的租约，以及预写式的工具调用日志。进程崩溃不会丢失已完成的工作，也不会重复执行有副作用的操作。

## 结果

所有数字都由 [`evals/`](evals/) 里的脚本测得，语料是一组固定版本的 Wikipedia 文章，评分由独立的 `gpt-4o` 模型完成。细节与置信区间见 [docs/zh/evaluation.md](docs/zh/evaluation.md)。

| | |
|---|---|
| **82/82** 次强制杀进程的运行全部恢复 | 报告与未崩溃时逐字节一致，webhook 报告恰好送达一次 |
| **99.2%** 被替换的引用被发现 | 论断评审标出 **100%** 注入的事实性改动，**0/129** 误报 |
| prompt 中累计证据量 **−62%** | 按章节分配证据 vs. 每节都放全部证据；总 token **−20%** |
| **100%** 的研究发现被引用 | 由核对环节强制保证 |

## 工作原理

<p align="center"><img src="docs/architecture.png" width="100%"></p>

## 快速开始

```bash
git clone https://github.com/limengge426/deeptrace-research-agent.git && cd deeptrace-research-agent
pip install -e ".[server]"
export DEEPTRACE_API_KEY=sk-...              # 任意 OpenAI 兼容接口（DEEPTRACE_BASE_URL, DEEPTRACE_MODEL）

deeptrace run "Are heat pumps worth it in cold climates?" --corpus examples/corpus
deeptrace serve --corpus examples/corpus     # 网页控制台 http://127.0.0.1:8000（先构建一次：cd web && npm i && npm run build）
docker compose up --build                    # API + 两个 worker，可选 Postgres / Neo4j profile
```

更多用法：[网页搜索、混合检索、LangGraph agent、计划审批、HTTP API](docs/zh/usage.md)。

## 技术栈

| 层 | |
|---|---|
| Agent | LangGraph + LangChain，OpenAI 兼容 API |
| 执行框架 | asyncio 状态机、检查点、租约 + fencing token、预写式工具调用日志、预算控制 |
| 检索 | Tavily + 网页抓取、BM25、sentence-transformers + Faiss、Neo4j 知识图谱、Reciprocal Rank Fusion |
| 存储 | SQLAlchemy（SQLite / Postgres）、Alembic |
| API / 界面 | FastAPI + Server-Sent Events、React + TypeScript |
| 运维 | Docker Compose、Prometheus 指标、GitHub Actions（SQLite、Postgres、Neo4j 任务）、GitHub Pages |

## 致谢

整体设计（可恢复的执行框架、证据账本、在最终报告前把关的完成度检查）受到 [SichengLong26/deepresearch_agent_harness](https://github.com/SichengLong26/deepresearch_agent_harness) 所描述架构的启发。DeepTrace 是从零开始的独立实现，没有复制其代码。其中的论断级忠实度评审，针对的是原项目文档自己指出的不足：它的论断支持度指标是一个确定性的近似，而不是语义层面的检查。

## 许可证

[MIT](LICENSE)。`web/public/demo/` 中的演示数据包含 Wikipedia 摘录（CC BY-SA 4.0），每段都链接到对应的原文版本。

<sub>DeepTrace 曾用名 *researchloop*。</sub>
