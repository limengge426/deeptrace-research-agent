"""Claim extraction, the judge wrapper, and faithfulness-driven repair."""

import asyncio
import re

from researchloop import ResearchRuntime, RunStore
from researchloop.faithfulness import extract_claims, judge_claims
from researchloop.ledger import EvidenceLedger
from researchloop.models import Report, Section
from researchloop.search import SearchHit

from . import fakes
from .fakes import FakeSearch, ScriptedLLM


def _ledger(n: int) -> EvidenceLedger:
    ledger = EvidenceLedger()
    for i in range(n):
        ledger.add(SearchHit(f"u{i}", f"Source {i}", f"content {i}"), task_id="t1", query="q")
    return ledger


def test_extract_claims_splits_sentences_and_keeps_only_cited_ones():
    report = Report("T", [
        Section("Basics", "Heat pumps move heat [E1]. They are popular. COP is often above 3 [E2][E3]!"),
        Section("Notes", "- Cold climates need backup heat [E4]\n- Costs vary.\n\nEfficiency falls in the cold. [E2]"),
        Section("中文", "热泵搬运热量[E1]。它很受欢迎。"),
    ])
    claims = extract_claims(report)
    assert [(c.section, c.citations) for c in claims] == [
        ("Basics", ["E1"]),
        ("Basics", ["E2", "E3"]),
        ("Notes", ["E4"]),
        ("Notes", ["E2"]),  # citation after the full stop is reattached
        ("中文", ["E1"]),
    ]
    assert claims[3].text.startswith("Efficiency falls in the cold.")


def test_judge_fails_closed_on_missing_verdicts_and_unknown_evidence():
    report = Report("T", [Section("A", "First claim [E1]. Second claim [E2]. Third claim [E9].")])

    def judge(system, user):
        return {"verdicts": [{"id": 1, "label": "supported", "reason": "ok"}]}  # skips claim 2

    result = asyncio.run(judge_claims(ScriptedLLM(judge=judge), extract_claims(report), _ledger(2)))
    assert [v.label for v in result.verdicts] == ["supported", "unsupported", "unsupported"]
    assert "no verdict" in result.verdicts[1].reason
    assert "does not exist" in result.verdicts[2].reason
    assert result.summary()["support_rate"] == round(1 / 3, 4)


def test_judge_sees_full_text_of_cited_evidence_in_batches():
    report = Report("T", [Section("A", " ".join(f"Claim {i} [E{i % 3 + 1}]." for i in range(10)))])
    prompts = []

    def judge(system, user):
        prompts.append(user)
        return fakes.judge_all(user, "supported")

    result = asyncio.run(judge_claims(ScriptedLLM(judge=judge), extract_claims(report), _ledger(3), batch_size=4))
    assert len(prompts) == 3 and len(result.verdicts) == 10
    assert "content 1" in prompts[0] and "[E1] Source 0" in prompts[0]


def test_unsupported_claim_is_repaired_with_a_precise_note(tmp_path):
    drafts = []

    def report(system, user):
        drafts.append(user)
        return fakes.report(system, user)

    def judge(system, user):
        # First draft: flag claim 1 as contradicted; afterwards everything is supported.
        label = "contradicted" if len(drafts) == 1 else "supported"
        out = fakes.judge_all(user, "supported")
        out["verdicts"][0].update(label=label, reason="E1 says the opposite")
        return out

    runtime = ResearchRuntime(ScriptedLLM(report=report, judge=judge), FakeSearch(), RunStore(tmp_path / "r.db"))
    result = asyncio.run(runtime.start("Are heat pumps worth it?"))

    assert result.status == "done" and result.state.repairs == 1
    assert "contradicted" in drafts[1] and "E1 says the opposite" in drafts[1]
    rates = [f["support_rate"] for f in result.state.faithfulness]
    assert rates[0] < 1.0 and rates[-1] == 1.0
    assert "Limitations" not in result.markdown


def test_claims_still_unsupported_after_repairs_are_reported_as_limitations(tmp_path):
    llm = ScriptedLLM(judge=lambda s, u: fakes.judge_all(u, "unsupported"))
    result = asyncio.run(ResearchRuntime(llm, FakeSearch(), RunStore(tmp_path / "r.db")).start("q about heat pumps"))
    assert result.status == "done"
    assert "## Limitations" in result.markdown and "not supported by the cited source" in result.markdown
    assert "Rewrite it" not in result.markdown


def test_separate_judge_model_is_used_when_given(tmp_path):
    writer, judge = ScriptedLLM(), ScriptedLLM()
    runtime = ResearchRuntime(writer, FakeSearch(), RunStore(tmp_path / "r.db"), judge_llm=judge)
    asyncio.run(runtime.start("Are heat pumps worth it?"))
    assert writer.calls["judge"] == 0 and judge.calls["judge"] >= 1


def test_faithfulness_can_be_disabled(tmp_path):
    llm = ScriptedLLM()
    result = asyncio.run(
        ResearchRuntime(llm, FakeSearch(), RunStore(tmp_path / "r.db"), check_faithfulness=False).start("q")
    )
    assert llm.calls["judge"] == 0 and result.state.faithfulness == []
