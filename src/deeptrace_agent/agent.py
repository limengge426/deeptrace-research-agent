"""Agentic research for one sub-question, as a LangGraph ReAct loop.

The default executor runs a fixed pipeline (write queries, search each, summarise). With an
``AgentResearcher`` the model instead decides what to search, whether to refine a query or read a
page in full, and when it has enough evidence to answer.

The durable harness stays in charge: every tool call still goes through the ToolRunner (write-ahead
intents, idempotent replay, budgets), every source still enters the evidence ledger with an id, and
LLM usage is metered through a LangChain callback into the same budget and metrics.

Requires the ``agent`` extra (langgraph, langchain-core; langchain-openai for ``chat_model_from_env``).
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import LLMResult
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from .budget import BudgetExceeded, BudgetMeter
from .env import getenv
from .llm import LLMFormatError, estimate_tokens, parse_json_object
from .models import CITATION_RE, Finding, Task
from .tools import ToolOutcomeUnknown

if TYPE_CHECKING:
    from .executor import TaskExecutor

AGENT_SYSTEM = """You are a research agent answering ONE sub-question of a larger research task.

Use the tools to gather evidence. Every source a tool returns is recorded with an evidence id like [E4].
Search with focused queries, refine a query when results are off-topic, and read a page in full only
when a snippet looks relevant but incomplete. Stop as soon as the evidence answers the sub-question.

When you are done, reply WITHOUT calling a tool, with JSON only:
{"summary": "2-6 sentences; cite every claim inline like [E4]", "used": ["E4", "E7"]}
Cite only ids that tools returned. If the evidence does not answer the question, say so plainly."""


class _Metering(AsyncCallbackHandler):
    """Charges agent LLM calls to the run's budget and metrics (and stops the loop when over budget)."""

    raise_error = True

    def __init__(self, meter: BudgetMeter) -> None:
        self.meter = meter
        self._started: dict[UUID, tuple[float, int]] = {}

    async def on_chat_model_start(self, serialized: dict[str, Any], messages, *, run_id: UUID, **kwargs: Any) -> None:
        self.meter.check()
        prompt = sum(estimate_tokens(str(m.content)) for batch in messages for m in batch)
        self._started[run_id] = (time.monotonic(), prompt)

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        started, prompt = self._started.pop(run_id, (time.monotonic(), 0))
        tokens = 0
        for gen in response.generations[0] if response.generations else []:
            usage = getattr(getattr(gen, "message", None), "usage_metadata", None)
            if usage:
                tokens += usage.get("total_tokens", 0)
        if not tokens:  # the provider reported no usage: estimate it
            text = "".join(str(getattr(g, "message", g).content) for g in (response.generations[0] or []))
            tokens = prompt + estimate_tokens(text)
        self.meter.tokens += tokens
        self.meter.llm_calls += 1
        self.meter.metrics.record_llm("agent", tokens=tokens, seconds=time.monotonic() - started)


class AgentResearcher:
    def __init__(self, chat_model: Any, *, max_tool_calls: int = 6) -> None:
        self.chat_model = chat_model
        self.max_tool_calls = max_tool_calls

    def _tools(self, task: Task, executor: "TaskExecutor", gathered: list[str]) -> list[StructuredTool]:
        calls = {"n": 0}

        def over_budget() -> str | None:
            calls["n"] += 1
            if calls["n"] > self.max_tool_calls:
                return "Tool budget for this sub-question is used up. Answer now with the evidence you have."
            return None

        async def search_sources(query: str) -> str:
            """Search for sources on a focused query. Returns evidence cards with ids like [E4]."""
            if (stop := over_budget()):
                return stop
            try:
                evidence = await executor.gather(task, query)
            except (BudgetExceeded, ToolOutcomeUnknown):
                raise
            except Exception as exc:  # noqa: BLE001 - let the model recover from a failed search
                return f"Search failed: {type(exc).__name__}: {exc}"
            for ev in evidence:
                if ev.id not in gathered:
                    gathered.append(ev.id)
            if not evidence:
                return "No results. Try a different query."
            return executor.ledger.cards([e.id for e in evidence], max_chars=900)

        return [StructuredTool.from_function(coroutine=search_sources, name="search_sources",
                                             description=search_sources.__doc__)]

    async def research(self, task: Task, upstream: list[Finding], executor: "TaskExecutor") -> Finding:
        gathered: list[str] = []
        tools = self._tools(task, executor, gathered)
        model = self.chat_model.bind_tools(tools)

        async def agent(state: MessagesState, config) -> dict:
            return {"messages": [await model.ainvoke(state["messages"], config)]}

        graph = StateGraph(MessagesState)
        graph.add_node("agent", agent)
        graph.add_node("tools", ToolNode(tools, handle_tool_errors=False))  # run-level errors must propagate
        graph.add_edge(START, "agent")
        graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
        graph.add_edge("tools", "agent")
        app = graph.compile()

        user = f"Sub-question: {task.question}"
        if upstream:
            user += "\n\nFindings from prerequisite sub-questions:\n" + "\n".join(f"- {f.summary}" for f in upstream)
        result = await app.ainvoke(
            {"messages": [SystemMessage(AGENT_SYSTEM), HumanMessage(user)]},
            {"recursion_limit": 2 * self.max_tool_calls + 6, "callbacks": [_Metering(executor.meter)]},
        )
        final = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage)), None)
        return self._finding(task, str(final.content) if final else "", gathered)

    @staticmethod
    def _finding(task: Task, text: str, gathered: list[str]) -> Finding:
        if not gathered:
            return Finding(task.id, "No sources were found for this sub-question.", [])
        try:
            data = parse_json_object(text)
            summary = str(data.get("summary", "")).strip()
            claimed = [str(e) for e in data.get("used") or []]
        except LLMFormatError:
            summary, claimed = text.strip(), []
        if not summary:
            raise LLMFormatError("the agent finished without a summary")
        # Keep only ids this task actually retrieved; anything else is a hallucinated citation.
        used = [e for e in dict.fromkeys(claimed + CITATION_RE.findall(summary)) if e in gathered]
        return Finding(task.id, summary, used or gathered)


def chat_model_from_env() -> Any:
    """A LangChain chat model for the configured OpenAI-compatible endpoint."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=getenv("MODEL", "gpt-4o-mini"),
        api_key=getenv("API_KEY") or os.getenv("OPENAI_API_KEY"),
        base_url=getenv("BASE_URL", "https://api.openai.com/v1"),
        temperature=0.2,
    )
