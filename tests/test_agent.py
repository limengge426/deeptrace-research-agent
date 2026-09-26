"""The LangGraph research agent, driven by a scripted chat model (no API calls)."""

import asyncio
import json
import re
from typing import Any, Callable

import pytest

pytest.importorskip("langgraph")

from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from deeptrace_agent import Budget, ResearchRuntime, RunStore  # noqa: E402
from deeptrace_agent.agent import AgentResearcher  # noqa: E402

from .fakes import FakeSearch, ScriptedLLM  # noqa: E402


class ScriptedChatModel(BaseChatModel):
    """Answers with whatever ``script(messages)`` returns; supports bind_tools for LangGraph."""

    script: Any = None
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls += 1
        message = self.script(messages)
        message.usage_metadata = {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
        return ChatResult(generations=[ChatGeneration(message=message)])


def search_then_answer(queries: list[str], extra_citation: str | None = None) -> Callable:
    """Search each query in turn, then answer citing every evidence id seen."""
    def script(messages):
        done = sum(isinstance(m, ToolMessage) for m in messages)
        if done < len(queries):
            return AIMessage(content="", tool_calls=[{"name": "search_sources", "args": {"query": queries[done]},
                                                      "id": f"call{done}"}])
        ids = sorted({i for m in messages if isinstance(m, ToolMessage) for i in re.findall(r"\[(E\d+)\]", m.content)})
        cites = " ".join(f"[{i}]" for i in ids) + (f" [{extra_citation}]" if extra_citation else "")
        return AIMessage(content=json.dumps({"summary": f"Heat pumps move heat {cites}.", "used": ids}))
    return script


def _runtime(tmp_path, chat, **kw):
    return ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / "a.db"),
                           researcher=AgentResearcher(chat, **kw.pop("agent", {})), **kw)


def test_agent_searches_decides_and_cites_only_retrieved_evidence(tmp_path):
    chat = ScriptedChatModel(script=search_then_answer(["heat pump basics", "heat pump efficiency"],
                                                       extra_citation="E999"))
    result = asyncio.run(_runtime(tmp_path, chat).start("Are heat pumps worth it?"))

    assert result.status == "done"
    finding = result.state.findings["t1"]
    assert finding.evidence_ids and "E999" not in finding.evidence_ids  # hallucinated id dropped
    evidence = {e.id: e for e in result.state.evidence}
    assert {evidence[i].task_id for i in finding.evidence_ids} == {"t1"}
    metrics = result.state.metrics
    assert metrics["llm"]["agent"]["calls"] == chat.calls  # agent LLM calls are metered
    assert metrics["tools"]["fake"]["calls"] >= 2  # searches went through the tool runner
    assert metrics["llm"].get("queries") is None  # the fixed pipeline was not used


def test_agent_tool_budget_forces_an_answer(tmp_path):
    seen = []

    def greedy(messages):
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        seen.extend(m.content for m in tool_msgs[len(seen):])
        if tool_msgs and "used up" in tool_msgs[-1].content:
            return search_then_answer([])(messages)
        return AIMessage(content="", tool_calls=[{"name": "search_sources",
                                                  "args": {"query": f"q{len(tool_msgs)}"}, "id": f"c{len(tool_msgs)}"}])

    chat = ScriptedChatModel(script=greedy)
    result = asyncio.run(_runtime(tmp_path, chat, agent={"max_tool_calls": 2}).start("Are heat pumps worth it?"))
    assert result.status == "done"
    assert any("used up" in c for c in seen)


def test_run_budget_halts_an_agent_mid_task(tmp_path):
    chat = ScriptedChatModel(script=search_then_answer([f"query {i}" for i in range(10)]))
    runtime = _runtime(tmp_path, chat, budget=Budget(max_tool_calls=3), agent={"max_tool_calls": 10})
    result = asyncio.run(runtime.start("Are heat pumps worth it?"))
    assert result.status == "halted" and "tool call budget" in result.state.error


def test_agent_with_no_results_reports_no_sources(tmp_path):
    chat = ScriptedChatModel(script=search_then_answer(["nothing"]))
    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(empty_for=("",)), RunStore(tmp_path / "b.db"),
                              researcher=AgentResearcher(chat))
    result = asyncio.run(runtime.start("Are heat pumps worth it?"))
    assert result.status == "failed" and "no evidence" in result.state.error
