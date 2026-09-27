"""Follow-up questions: a thread of runs that share one evidence ledger."""

import asyncio
import json

import pytest
from sqlalchemy import text

from deeptrace_agent import ResearchRuntime, RunStore
from deeptrace_agent.runtime import InvalidAction
from deeptrace_agent.store import MIGRATIONS

from . import fakes
from .fakes import FakeSearch, ScriptedLLM


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs.db")
    yield s
    s.close()


def first_turn(runtime):
    return asyncio.run(runtime.start("Are heat pumps worth it?"))


def stages(store, run_id):
    return [(e.payload["frm"], e.payload["to"]) for e in store.events(run_id) if e.kind == "stage"]


def test_answerable_followup_reuses_the_evidence_without_searching(store):
    search = FakeSearch()
    runtime = ResearchRuntime(ScriptedLLM(), search, store)
    parent = first_turn(runtime)
    searches = sum(search.calls.values())

    result = asyncio.run(runtime.followup(parent.run_id, "What about the second point?"))

    assert result.status == "done"
    assert sum(search.calls.values()) == searches  # answered from the inherited ledger
    assert stages(store, result.run_id) == [("route", "report"), ("report", "verify"), ("verify", "done")]
    state = result.state
    assert state.mode == "followup" and state.turn == 2 and state.parent_id == parent.run_id
    assert state.thread_id == parent.run_id
    assert state.route["decision"] == "answer" and state.research_question == "Standalone: What about the second point?"
    assert [e.id for e in state.evidence] == [e.id for e in parent.state.evidence]
    assert set(state.report.citations()) <= {e.id for e in parent.state.evidence}
    assert [r.id for r in store.thread(parent.run_id)] == [parent.run_id, result.run_id]
    assert "## Answer" in result.markdown and "## Sources" in result.markdown


def test_router_sees_the_earlier_turns(store):
    seen = []

    def route(system, user):
        seen.append(user)
        return fakes.route_answer(system, user)

    runtime = ResearchRuntime(ScriptedLLM(route=route), FakeSearch(), store)
    parent = first_turn(runtime)
    second = asyncio.run(runtime.followup(parent.run_id, "And the costs?"))
    asyncio.run(runtime.followup(second.run_id, "Compare them"))

    assert "Q: Are heat pumps worth it?" in seen[0] and "A: A sourced claim" in seen[0]
    assert "[E" not in seen[0].split("Research done so far:")[0]  # citations are stripped from the digest
    assert "Q: Standalone: And the costs?" in seen[1]  # the rewritten question is remembered
    assert store.load(store.thread(parent.run_id)[-1].id).turn == 3


def test_extend_researches_only_what_is_missing_and_keeps_evidence_ids(store):
    def route(system, user):
        out = fakes.route_answer(system, user)
        return {**out, "decision": "extend", "tasks": ["t2"], "missing": ["Heat pump running costs"]}

    search = FakeSearch()
    runtime = ResearchRuntime(ScriptedLLM(route=route), search, store)
    parent = first_turn(runtime)
    inherited = {e.id: e.url for e in parent.state.evidence}

    result = asyncio.run(runtime.followup(parent.run_id, "How much do they cost to run?"))

    assert result.status == "done"
    new_tasks = [t for t in result.state.plan.tasks if t.id.startswith("f2r1_")]
    assert len(new_tasks) == 1 and new_tasks[0].status == "done"
    assert result.state.focus == ["t2", new_tasks[0].id]
    assert {e.id: e.url for e in result.state.evidence if e.id in inherited} == inherited
    new_ids = set(result.state.findings[new_tasks[0].id].evidence_ids)
    assert new_ids and not new_ids & set(inherited)  # numbering continues after the inherited ids
    assert new_ids & set(result.state.report.citations())
    assert stages(store, result.run_id)[:2] == [("route", "execute"), ("execute", "report")]


def test_answer_that_lacks_evidence_escalates_to_more_research(store):
    calls = []

    def answer(system, user):
        out = fakes.answer(system, user)
        calls.append(user)
        return {**out, "missing": ["Heat pump noise levels"]} if len(calls) == 1 else out

    runtime = ResearchRuntime(ScriptedLLM(answer=answer), FakeSearch(), store)
    parent = first_turn(runtime)
    result = asyncio.run(runtime.followup(parent.run_id, "Are they loud?"))

    assert result.status == "done" and len(calls) == 2
    assert any(e.kind == "answer_incomplete" for e in store.events(result.run_id))
    assert any(t.id.startswith("f2r1_") for t in result.state.plan.tasks)


def test_unrelated_question_starts_a_fresh_report(store):
    planned = []

    def route(system, user):
        return {"question": "How do tides work?", "decision": "new", "tasks": [], "missing": [], "reason": "new topic"}

    def plan(system, user):
        planned.append(user)
        return fakes.plan_or_replan(system, user)

    runtime = ResearchRuntime(ScriptedLLM(route=route, plan=plan), FakeSearch(), store)
    parent = first_turn(runtime)
    result = asyncio.run(runtime.followup(parent.run_id, "Unrelated: how do tides work?"))

    assert result.status == "done" and result.state.mode == "report"
    assert stages(store, result.run_id)[:2] == [("route", "plan"), ("plan", "execute")]
    assert "How do tides work?" in planned[-1]  # planned from the rewritten question
    assert [t.id for t in result.state.plan.tasks] == ["t1", "t2", "t3"]  # nothing inherited
    assert len(result.state.evidence) == len(parent.state.evidence)  # ledger restarted, not appended to
    assert result.state.thread_id == parent.run_id  # still part of the conversation


def test_unusable_router_reply_falls_back_to_answering(store):
    runtime = ResearchRuntime(ScriptedLLM(route=lambda s, u: "not json"), FakeSearch(), store)
    parent = first_turn(runtime)
    result = asyncio.run(runtime.followup(parent.run_id, "Why?"))

    assert result.status == "done" and result.state.route["decision"] == "answer"
    assert result.state.research_question == "Why?"


def test_followups_need_a_finished_parent_and_an_idle_thread(store):
    runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), store)
    queued = runtime.create("Not started yet")
    with pytest.raises(InvalidAction, match="only finished runs"):
        runtime.create_followup(queued, "Hm?")

    parent = first_turn(runtime)
    runtime.create_followup(parent.run_id, "First follow-up")
    with pytest.raises(InvalidAction, match="unfinished turn"):
        runtime.create_followup(parent.run_id, "Second follow-up")


def test_followup_survives_a_crash_while_answering(store):
    crash = {"armed": True}

    def answer(system, user):
        if crash["armed"]:
            crash["armed"] = False
            raise KeyboardInterrupt
        return fakes.answer(system, user)

    runtime = ResearchRuntime(ScriptedLLM(answer=answer), FakeSearch(), store)
    parent = first_turn(runtime)
    run_id = runtime.create_followup(parent.run_id, "What about the second point?")
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(runtime.resume(run_id))
    assert store.load(run_id).stage == "report"  # the routing decision was checkpointed

    result = asyncio.run(runtime.resume(run_id))
    assert result.status == "done"
    assert sum(1 for e in store.events(run_id) if e.kind == "route") == 1  # not routed again


def test_migration_adds_threads_to_an_existing_database(tmp_path):
    from alembic import command
    from alembic.config import Config

    path = tmp_path / "old.db"
    old = RunStore(path, migrate=False)
    with old.engine.begin() as conn:
        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS))
        config.attributes["connection"] = conn
        command.upgrade(config, "0001")
        conn.execute(text("INSERT INTO runs (id, question, status, created_at, updated_at, state) "
                          "VALUES ('old1', 'q', 'done', 1, 1, :s)"), {"s": json.dumps({"question": "q"})})
    old.close()

    upgraded = RunStore(path)
    assert [r.id for r in upgraded.thread("old1")] == ["old1"]
    upgraded.close()
