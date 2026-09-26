import asyncio

import pytest

from deeptrace_agent.ledger import EvidenceLedger
from deeptrace_agent.llm import LLMFormatError, parse_json_object
from deeptrace_agent.models import Evidence, Finding, Plan, PlanError, Report, RunState, Section, Task
from deeptrace_agent.planner import initial_plan
from deeptrace_agent.search import LocalCorpusSearch, SearchHit
from deeptrace_agent.verifier import check_report

from .fakes import ScriptedLLM


# -- plan DAG -----------------------------------------------------------------


def test_waves_group_independent_tasks():
    plan = Plan([Task("a", "?"), Task("b", "?"), Task("c", "?", ["a", "b"]), Task("d", "?", ["c"])])
    assert [[t.id for t in w] for w in plan.waves()] == [["a", "b"], ["c"], ["d"]]


def test_waves_skip_settled_tasks():
    plan = Plan([Task("a", "?", status="done"), Task("b", "?", ["a"])])
    assert [[t.id for t in w] for w in plan.waves()] == [["b"]]


@pytest.mark.parametrize(
    "tasks, message",
    [
        ([Task("a", "?", ["b"]), Task("b", "?", ["a"])], "cycle"),
        ([Task("a", "?", ["zzz"])], "unknown"),
        ([Task("a", "?"), Task("a", "?")], "duplicate"),
    ],
)
def test_invalid_plans_are_rejected(tasks, message):
    with pytest.raises(PlanError, match=message):
        Plan(tasks).validate()


def test_planner_retries_with_feedback_after_invalid_plan():
    replies = iter(
        [
            {"tasks": [{"id": "t1", "question": "x", "depends_on": ["t1"]}]},
            {"tasks": [{"id": "t1", "question": "x", "depends_on": []}]},
        ]
    )
    prompts = []

    def plan(system, user):
        prompts.append(user)
        return next(replies)

    tasks = asyncio.run(initial_plan(ScriptedLLM(plan=plan), "q"))
    assert [t.id for t in tasks] == ["t1"]
    assert "rejected" in prompts[1]


# -- JSON parsing -------------------------------------------------------------


def test_parse_json_tolerates_fences_and_braces_in_strings():
    text = 'Sure!\n```json\n{"summary": "a {weird} string \\"quoted\\"", "used": ["E1"]}\n```'
    assert parse_json_object(text) == {"summary": 'a {weird} string "quoted"', "used": ["E1"]}


@pytest.mark.parametrize("text", ["no json here", '{"a": 1', "[1, 2]"])
def test_parse_json_rejects_bad_replies(text):
    with pytest.raises(LLMFormatError):
        parse_json_object(text)


# -- ledger -------------------------------------------------------------------


def test_ledger_deduplicates_and_restores():
    ledger = EvidenceLedger()
    a = ledger.add(SearchHit("u1", "t", "Some  text"), task_id="t1", query="q")
    again = ledger.add(SearchHit("u1", "t", "some text"), task_id="t2", query="q2")
    b = ledger.add(SearchHit("u2", "t", "other"), task_id="t2", query="q2")
    assert (a.id, again.id, b.id) == ("E1", "E1", "E2")

    restored = EvidenceLedger(ledger.items())
    assert restored.add(SearchHit("u2", "t", "other"), task_id="t3", query="q").id == "E2"
    assert restored.add(SearchHit("u3", "t", "new"), task_id="t3", query="q").id == "E3"


def test_run_state_round_trips_through_json():
    state = RunState(
        "q",
        stage="verify",
        plan=Plan([Task("t1", "?", status="done")]),
        findings={"t1": Finding("t1", "s [E1]", ["E1"])},
        evidence=[Evidence("E1", "u", "t", "c", "t1", "q")],
        report=Report("T", [Section("H", "b [E1]")]),
    )
    assert RunState.from_dict(state.to_dict()) == state


# -- local search -------------------------------------------------------------


def test_local_corpus_ranks_relevant_chunk_first(tmp_path):
    (tmp_path / "pumps.md").write_text("# Heat pumps\n\nA heat pump moves heat using a refrigerant cycle.")
    (tmp_path / "solar.md").write_text("# Solar\n\nPhotovoltaic panels convert sunlight into electricity.")
    (tmp_path / "茶.md").write_text("# 茶\n\n绿茶是一种未发酵的茶。")
    search = LocalCorpusSearch(tmp_path)

    hits = asyncio.run(search.search("how does a heat pump work", 2))
    assert hits[0].title == "Heat pumps"
    assert asyncio.run(search.search("绿茶", 1))[0].title == "茶"


# -- verifier -----------------------------------------------------------------


def _ledger(n: int) -> EvidenceLedger:
    ledger = EvidenceLedger()
    for i in range(n):
        ledger.add(SearchHit(f"u{i}", "t", f"c{i}"), task_id="t1", query="q")
    return ledger


def test_verifier_passes_a_clean_report():
    plan = Plan([Task("t1", "q1", status="done")])
    findings = {"t1": Finding("t1", "s", ["E1"])}
    report = Report("T", [Section("A", "claim [E1]")])
    assert check_report(report, plan, findings, _ledger(1), addressed_gaps=[]).passed


def test_verifier_flags_report_and_evidence_problems():
    plan = Plan([Task("t1", "q1", status="done"), Task("t2", "q2", status="done"), Task("t3", "q3", status="failed")])
    findings = {"t1": Finding("t1", "s", ["E1"]), "t2": Finding("t2", "s", ["E2"])}
    report = Report("T", [Section("A", "claim [E1] and [E9]"), Section("B", "unsourced " * 60)])

    verdict = check_report(report, plan, findings, _ledger(2), addressed_gaps=[])

    assert sorted(i.code for i in verdict.issues) == [
        "dropped_finding",
        "no_evidence",
        "uncited_section",
        "unknown_citation",
    ]
    assert verdict.gaps == ["q3"]
    assert check_report(report, plan, findings, _ledger(2), addressed_gaps=["q3"]).gaps == []
