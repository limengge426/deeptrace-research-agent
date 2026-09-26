"""End-to-end tests of the research loop with scripted LLM and search."""

import asyncio

import pytest

from deeptrace_agent import Budget, ResearchRuntime, RunStore

from . import fakes
from .fakes import FakeSearch, ScriptedLLM


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs.db")
    yield s
    s.close()


def run(runtime, question="Are heat pumps worth it?"):
    return asyncio.run(runtime.start(question))


def test_happy_path_produces_verified_cited_report(store):
    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), store)
    result = run(runtime)

    assert result.status == "done"
    assert result.state.replans == 0 and result.state.repairs == 0
    assert all(t.status == "done" for t in result.state.plan.tasks)
    assert "## Sources" in result.markdown and "https://example.org/" in result.markdown
    assert store.runs()[0].status == "done"

    stages = [(e.payload["frm"], e.payload["to"]) for e in store.events(result.run_id) if e.kind == "stage"]
    assert stages == [("plan", "execute"), ("execute", "report"), ("report", "verify"), ("verify", "done")]


def test_independent_tasks_run_in_parallel(store):
    search = FakeSearch(delay=0.05)
    run(ResearchRuntime(ScriptedLLM(), search, store, concurrency=3))
    assert search.max_in_flight >= 2


def test_dependent_task_sees_upstream_findings(store):
    seen = {}

    def queries(system, user):
        seen[fakes._sub_question(user)] = user
        return fakes.queries(system, user)

    run(ResearchRuntime(ScriptedLLM(queries=queries), FakeSearch(), store))
    assert "Findings from prerequisite tasks" in seen["Are heat pumps worth it in cold climates?"]
    assert "Findings from prerequisite tasks" not in seen["What is a heat pump?"]


def test_bad_citation_triggers_repair_of_only_the_broken_section(store):
    repairs = []

    def section(system, user):
        out = fakes.section(system, user)
        if "failed verification" in user:
            repairs.append(user)
        elif "Section: About t1" in user:
            out["body"] += " Also [E999]."
        return out

    result = run(ResearchRuntime(ScriptedLLM(section=section), FakeSearch(), store))

    assert result.status == "done"
    assert result.state.repairs == 1
    assert len(repairs) == 1 and "Section: About t1" in repairs[0] and "E999" in repairs[0]
    assert "[E999]" not in result.markdown
    repaired = [e for e in store.events(result.run_id) if e.kind == "report_repaired"][0].payload
    assert repaired["sections"][0] == "About t1" and "About t2" in repaired["kept"]


def test_single_shot_report_mode_still_works(store):
    result = run(ResearchRuntime(ScriptedLLM(), FakeSearch(), store, report_mode="single"))
    assert result.status == "done" and result.state.outline == []


def test_evidence_gap_triggers_replan(store):
    search = FakeSearch(empty_for=("efficient",))
    result = run(ResearchRuntime(ScriptedLLM(), search, store))

    assert result.status == "done"
    assert result.state.replans == 1
    assert [t.id for t in result.state.plan.tasks if t.round == 1] == ["r1_1"]
    assert result.state.findings["r1_1"].evidence_ids
    assert any(e.kind == "replan" for e in store.events(result.run_id))


def test_failing_task_is_isolated_from_its_siblings(store):
    def finding(system, user):
        if "efficient" in user:
            raise RuntimeError("model crashed")
        return fakes.finding(system, user)

    result = run(ResearchRuntime(ScriptedLLM(finding=finding), FakeSearch(), store))

    statuses = {t.id: t.status for t in result.state.plan.tasks}
    assert statuses["t2"] == "failed"
    assert statuses["t1"] == statuses["t3"] == "done"
    assert result.state.replans == 1  # the failed task became an evidence gap
    assert result.status == "done"


def test_crashed_run_resumes_without_repeating_searches(store):
    crash = {"armed": True}

    def finding(system, user):
        # Kill the process while t3 (the second wave) is summarizing, i.e. after
        # its searches ran but before its wave was checkpointed.
        if "cold climates" in user and crash["armed"]:
            crash["armed"] = False
            raise KeyboardInterrupt
        return fakes.finding(system, user)

    search = FakeSearch()
    runtime = ResearchRuntime(ScriptedLLM(finding=finding), search, store)
    with pytest.raises(KeyboardInterrupt):
        run(runtime)

    run_id = store.runs()[0].id
    saved = store.load(run_id)
    assert saved.stage == "execute"
    assert {t.id: t.status for t in saved.plan.tasks} == {"t1": "done", "t2": "done", "t3": "pending"}
    searches_before = sum(search.calls.values())

    result = asyncio.run(runtime.resume(run_id))

    assert result.status == "done"
    assert sum(search.calls.values()) == searches_before  # t3's searches came from the cache
    assert max(search.calls.values()) == 1


def test_budget_halts_and_a_larger_budget_resumes(store):
    search = FakeSearch()
    tight = ResearchRuntime(ScriptedLLM(), search, store, budget=Budget(max_tool_calls=3))
    halted = run(tight)

    assert halted.status == "halted"
    assert "tool call budget" in halted.state.error
    assert store.runs()[0].status == "halted"

    roomy = ResearchRuntime(ScriptedLLM(), search, store, budget=Budget(max_tool_calls=50))
    result = asyncio.run(roomy.resume(halted.run_id))

    assert result.status == "done"
    assert result.state.usage["tool_calls"] <= 50
    assert max(search.calls.values()) == 1


def test_no_evidence_at_all_fails_honestly(store):
    result = run(ResearchRuntime(ScriptedLLM(), FakeSearch(empty_for=("",)), store))
    assert result.status == "failed"
    assert "no evidence" in result.state.error
    assert result.markdown is None


def test_critic_gaps_drive_one_more_round(store):
    gaps = iter([["Heat pump noise levels"], []])
    llm = ScriptedLLM(critic=lambda s, u: {"gaps": next(gaps)})
    result = run(ResearchRuntime(llm, FakeSearch(), store, critic=True))

    assert result.status == "done"
    assert result.state.replans == 1
    assert llm.calls["critic"] == 2
