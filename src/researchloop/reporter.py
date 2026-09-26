"""Reporter: writes the cited report and renders it to Markdown."""

from __future__ import annotations

from . import prompts
from .ledger import EvidenceLedger
from .llm import LLM, LLMFormatError, parse_json_object
from .models import CITATION_RE, Finding, Issue, Plan, Report, Section


def route_evidence(findings: dict[str, Finding], *, limit: int) -> list[str]:
    """Pick the evidence the report may cite: only what findings actually used.

    Findings already distilled the raw search hits, so the report prompt sees
    the evidence that matters instead of every hit, which keeps the context
    small without losing traceability (ids stay stable in the ledger).
    """
    ids: dict[str, None] = {}
    for finding in findings.values():
        for eid in finding.evidence_ids:
            ids.setdefault(eid, None)
    return list(ids)[:limit]


async def write_report(
    llm: LLM,
    question: str,
    plan: Plan,
    findings: dict[str, Finding],
    ledger: EvidenceLedger,
    *,
    problems: list[Issue] | None = None,
    max_evidence: int = 40,
    card_chars: int = 500,
) -> Report:
    findings_text = "\n".join(
        f"- [{tid}] {plan.get(tid).question}\n  {f.summary}" for tid, f in findings.items()
    )
    repair = ""
    if problems:
        repair = prompts.REPAIR_NOTE.format(problems="\n".join(f"- {p.detail}" for p in problems))
    user = prompts.REPORT_USER.format(
        question=question,
        findings=findings_text or "(no findings)",
        evidence=ledger.cards(route_evidence(findings, limit=max_evidence), max_chars=card_chars) or "(none)",
        repair=repair,
    )
    reply = await llm.complete(prompts.REPORT_SYSTEM, user, purpose="report", json_mode=True)
    data = parse_json_object(reply.text)
    sections = [
        Section(str(s.get("heading", "")).strip() or "Section", str(s.get("body", "")).strip())
        for s in data.get("sections") or []
        if isinstance(s, dict)
    ]
    if not sections:
        raise LLMFormatError("report has no sections")
    return Report(title=str(data.get("title") or question).strip(), sections=sections)


def strip_unknown_citations(report: Report, ledger: EvidenceLedger) -> Report:
    """Last-resort cleanup: drop citations that do not resolve to ledger entries."""

    def clean(body: str) -> str:
        return CITATION_RE.sub(lambda m: m.group(0) if m.group(1) in ledger else "", body)

    return Report(report.title, [Section(s.heading, clean(s.body)) for s in report.sections])


def render_markdown(report: Report, ledger: EvidenceLedger, *, notes: list[str] | None = None) -> str:
    cited = [cid for cid in report.citations() if cid in ledger]
    lines = [f"# {report.title}", ""]
    for section in report.sections:
        lines += [f"## {section.heading}", "", section.body, ""]
    if notes:
        lines += ["## Limitations", ""] + [f"- {n}" for n in notes] + [""]
    if cited:
        lines += ["## Sources", ""]
        for cid in cited:
            ev = ledger.get(cid)
            lines.append(f"- **[{cid}]** {ev.title}: {ev.url}")
        lines.append("")
    return "\n".join(lines)
