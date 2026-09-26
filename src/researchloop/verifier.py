"""Verifier: deterministic checks that decide whether a report is done.

Report issues (bad or missing citations, dropped findings) are fixed by
rewriting the report. Evidence issues (failed or empty tasks, critic gaps) are
fixed by planning more research.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import prompts
from .ledger import EvidenceLedger
from .llm import LLM, LLMFormatError, parse_json_object
from .models import Finding, Issue, Plan, Report

UNCITED_SECTION_MIN_CHARS = 400


@dataclass
class Verdict:
    issues: list[Issue] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues and not self.gaps

    @property
    def report_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.kind == "report"]


def check_report(
    report: Report,
    plan: Plan,
    findings: dict[str, Finding],
    ledger: EvidenceLedger,
    *,
    addressed_gaps: list[str],
) -> Verdict:
    verdict = Verdict()

    unknown = [c for c in report.citations() if c not in ledger]
    if unknown:
        verdict.issues.append(
            Issue("report", "unknown_citation", f"Citations {unknown} do not exist. Cite only listed evidence ids.")
        )

    for section in report.sections:
        if not section.citations() and len(section.body) >= UNCITED_SECTION_MIN_CHARS:
            verdict.issues.append(
                Issue("report", "uncited_section", f'Section "{section.heading}" makes claims without any citation.')
            )

    cited = set(report.citations())
    for tid, finding in findings.items():
        if finding.evidence_ids and not cited.intersection(finding.evidence_ids):
            verdict.issues.append(
                Issue(
                    "report",
                    "dropped_finding",
                    f'The report ignores the finding for "{plan.get(tid).question}"; '
                    f"cite at least one of {finding.evidence_ids}.",
                )
            )

    for task in plan.tasks:
        finding = findings.get(task.id)
        empty = task.status == "failed" or (finding is not None and not finding.evidence_ids)
        if empty and task.question not in addressed_gaps:
            verdict.issues.append(Issue("evidence", "no_evidence", f'No evidence for "{task.question}".'))
            verdict.gaps.append(task.question)

    return verdict


async def critic_gaps(llm: LLM, question: str, report_markdown: str) -> list[str]:
    """Optional LLM review for coverage gaps the deterministic checks cannot see."""
    user = f"Research question: {question}\n\nReport:\n{report_markdown}"
    reply = await llm.complete(prompts.CRITIC_SYSTEM, user, purpose="critic", json_mode=True)
    try:
        gaps = parse_json_object(reply.text).get("gaps") or []
    except LLMFormatError:
        return []
    return [g.strip() for g in gaps if isinstance(g, str) and g.strip()][:2]
