"""Context engineering: outline coverage, per-section evidence routing, condensing, figure checks."""

import asyncio
import re

from deeptrace_agent import ResearchRuntime, RunStore
from deeptrace_agent.ledger import EvidenceLedger
from deeptrace_agent.models import Finding, Plan, Report, Section, Task
from deeptrace_agent.reporter import ContextBudget, make_outline, write_sectioned_report
from deeptrace_agent.search import SearchHit
from deeptrace_agent.verifier import check_report

from . import fakes
from .fakes import FakeSearch, ScriptedLLM


def _world(n_tasks=3, per_task=2):
    ledger = EvidenceLedger()
    plan = Plan([Task(f"t{i}", f"Sub-question number {i}?", status="done") for i in range(1, n_tasks + 1)])
    findings = {}
    for task in plan.tasks:
        ids = [ledger.add(SearchHit(f"{task.id}/{k}", f"{task.id} source {k}", f"Fact {k} about {task.id}. " * 20),
                          task_id=task.id, query="q").id for k in range(per_task)]
        findings[task.id] = Finding(task.id, f"Summary of {task.id} [{ids[0]}].", ids)
    return plan, findings, ledger


def test_outline_keeps_every_finding_and_drops_unknown_tasks():
    plan, findings, _ = _world()

    def outline(system, user):
        return {"title": "T", "sections": [
            {"heading": "Intro", "tasks": ["t1", "t999"]},
            {"heading": "Intro", "tasks": ["t3"]},  # duplicate heading is ignored
            {"heading": "Wrap-up", "tasks": []},
        ]}

    title, sections = asyncio.run(make_outline(ScriptedLLM(outline=outline), "Q?", plan, findings))
    assert title == "T"
    assert sections == [
        {"heading": "Intro", "tasks": ["t1", "t2", "t3"], "synthesis": False},
        {"heading": "Wrap-up", "tasks": [], "synthesis": True},
    ]


def test_unusable_outline_falls_back_to_one_section_per_finding():
    plan, findings, _ = _world(n_tasks=2)
    _, sections = asyncio.run(make_outline(ScriptedLLM(outline=lambda s, u: "not json"), "Q?", plan, findings))
    assert [s["tasks"] for s in sections] == [["t1"], ["t2"]]


def test_each_section_sees_only_its_own_evidence_and_the_conclusion_sees_the_sections():
    plan, findings, ledger = _world()
    prompts = {}

    def section(system, user):
        prompts[re.search(r"Section: (.+)", user).group(1)] = user
        return fakes.section(system, user)

    def synthesis(system, user):
        prompts["Conclusion"] = user
        return {"body": "Summary."}

    llm = ScriptedLLM(section=section, synthesis=synthesis)
    report, outline = asyncio.run(write_sectioned_report(llm, "Q?", plan, findings, ledger))

    assert [s.heading for s in report.sections] == ["About t1", "About t2", "About t3", "Conclusion"]
    assert report.sections[-1].synthesis
    routed = set(re.findall(r"^\[(E\d+)\]", prompts["About t2"], re.MULTILINE))
    assert routed == set(findings["t2"].evidence_ids)
    assert "### About t1" in prompts["Conclusion"] and "Evidence:" not in prompts["Conclusion"]


def test_evidence_over_budget_is_condensed_into_cited_notes():
    plan, findings, ledger = _world(n_tasks=1, per_task=6)
    llm = ScriptedLLM()
    tight = ContextBudget(section_chars=900, card_chars=700, min_card_chars=250, digest_batch=4)
    report, _ = asyncio.run(write_sectioned_report(llm, "Q?", plan, findings, ledger, budget=tight))

    assert llm.calls["digest"] == 2  # 6 cards in batches of 4
    assert set(report.sections[0].citations()) == set(findings["t1"].evidence_ids)


def test_cards_shrink_before_condensing():
    plan, findings, ledger = _world(n_tasks=1, per_task=4)
    llm, seen = ScriptedLLM(), []

    def section(system, user):
        seen.append(user)
        return fakes.section(system, user)

    llm.handlers["section"] = section
    asyncio.run(write_sectioned_report(llm, "Q?", plan, findings, ledger,
                                       budget=ContextBudget(section_chars=1600, min_card_chars=250)))
    assert llm.calls["digest"] == 0
    evidence = seen[0].split("Evidence:", 1)[1]
    assert len(evidence) < 1600 + 400  # 4 cards of <= 400 chars plus headers


def test_uncited_figures_are_flagged_except_in_the_conclusion():
    plan, findings, ledger = _world(n_tasks=1)
    report = Report("T", [
        Section("About t1", "Heat pumps are efficient [E1]. Their COP reaches 3.5 in mild weather."),
        Section("Conclusion", "Overall, 2 of 3 sources agree.", synthesis=True),
    ])
    verdict = check_report(report, plan, findings, ledger, addressed_gaps=[],
                           outline=[{"heading": "About t1", "tasks": ["t1"], "synthesis": False}])
    figures = [i for i in verdict.issues if i.code == "uncited_figure"]
    assert len(figures) == 1 and figures[0].section == "About t1" and "3.5" in figures[0].detail


def test_uncited_figure_is_repaired_end_to_end(tmp_path):
    def section(system, user):
        out = fakes.section(system, user)
        if "failed verification" not in user and "About t1" in user:
            out["body"] += " Efficiency is 42% higher."
        return out

    result = asyncio.run(ResearchRuntime(ScriptedLLM(section=section), FakeSearch(),
                                         RunStore(tmp_path / "r.db")).start("Are heat pumps worth it?"))
    assert result.status == "done" and result.state.repairs == 1 and "42%" not in result.markdown


def test_unrouted_baseline_shows_every_section_all_evidence():
    plan, findings, ledger = _world()
    prompts = {}

    def section(system, user):
        prompts[re.search(r"Section: (.+)", user).group(1)] = user
        return fakes.section(system, user)

    llm = ScriptedLLM(section=section)
    budget = ContextBudget(route=False, section_chars=10**7)
    asyncio.run(write_sectioned_report(llm, "Q?", plan, findings, ledger, budget=budget))
    routed = set(re.findall(r"^\[(E\d+)\]", prompts["About t2"], re.MULTILINE))
    assert routed == {e.id for e in ledger.items()}


def test_evidence_exposure_is_metered_and_routing_reduces_it(tmp_path):
    exposure = {}
    for route in (True, False):
        runtime = ResearchRuntime(ScriptedLLM(), FakeSearch(), RunStore(tmp_path / f"{route}.db"),
                                  check_faithfulness=False,
                                  context_budget=ContextBudget(route=route, section_chars=10**7))
        state = asyncio.run(runtime.start("Are heat pumps worth it?")).state
        context = state.metrics["context"]["section"]
        assert context["prompts"] == len([s for s in state.report.sections if not s.synthesis])
        exposure[route] = context["evidence_chars"]
    assert exposure[True] < exposure[False]
