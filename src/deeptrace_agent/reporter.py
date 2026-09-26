"""Reporter: writes the cited report and renders it to Markdown.

Two modes:

* ``sections`` (default): plan an outline that assigns findings to sections,
  then write each section from only its own findings and evidence, in parallel.
  A section whose evidence exceeds its context budget is first condensed into
  cited notes (map), then written from the notes (reduce). Repairs rewrite only
  the sections that failed verification.
* ``single``: one call writes the whole report from all routed evidence. Kept
  as a baseline for comparison.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

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
    evidence = ledger.cards(route_evidence(findings, limit=max_evidence), max_chars=card_chars) or "(none)"
    user = prompts.REPORT_USER.format(
        question=question,
        findings=findings_text or "(no findings)",
        evidence=evidence,
        repair=repair,
    )
    _record_context(llm, "report", evidence)
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


# -- sectioned mode -----------------------------------------------------------


@dataclass
class ContextBudget:
    section_chars: int = 6000  # evidence characters one section prompt may carry
    card_chars: int = 700  # preferred size of one evidence card
    min_card_chars: int = 250  # below this, condense evidence into notes instead of truncating
    digest_batch: int = 8  # evidence cards per condensing call
    route: bool = True  # False: every section sees all evidence (the ablation baseline)


def _record_context(llm: LLM, purpose: str, evidence: str) -> None:
    """Count evidence text placed into a prompt (only when the LLM is metered)."""
    meter = getattr(llm, "meter", None)
    if meter is not None and evidence not in ("", "(none)"):
        meter.metrics.record_context(purpose, len(evidence))


def evidence_for(tasks: list[str], findings: dict[str, Finding]) -> list[str]:
    ids: dict[str, None] = {}
    for tid in tasks:
        for eid in findings[tid].evidence_ids if tid in findings else []:
            ids.setdefault(eid, None)
    return list(ids)


async def make_outline(llm: LLM, question: str, plan: Plan, findings: dict[str, Finding]) -> tuple[str, list[dict]]:
    """Ask for an outline, then repair it deterministically so that every finding
    with evidence is assigned to at least one body section."""
    usable = [tid for tid, f in findings.items() if f.evidence_ids]
    listing = "\n".join(f"- [{tid}] {plan.get(tid).question}\n  {findings[tid].summary}" for tid in usable)
    title, raw_sections = question, []
    try:
        reply = await llm.complete(
            prompts.OUTLINE_SYSTEM, f"Research question: {question}\n\nFindings:\n{listing}",
            purpose="outline", json_mode=True,
        )
        data = parse_json_object(reply.text)
        title = str(data.get("title") or question).strip()
        raw_sections = [s for s in data.get("sections") or [] if isinstance(s, dict)][:8]
    except LLMFormatError:
        pass  # fall back to one section per finding below

    outline: list[dict] = []
    seen_headings: set[str] = set()
    for sec in raw_sections:
        heading = str(sec.get("heading", "")).strip()
        if not heading or heading in seen_headings:
            continue
        seen_headings.add(heading)
        tasks = [str(t) for t in sec.get("tasks") or [] if str(t) in usable]
        outline.append({"heading": heading, "tasks": tasks, "synthesis": not tasks})

    body = [sec for sec in outline if not sec["synthesis"]]
    if not body:
        outline = [{"heading": plan.get(t).question, "tasks": [t], "synthesis": False} for t in usable] + [
            sec for sec in outline if sec["synthesis"]
        ][:1]
    else:
        assigned = {t for sec in body for t in sec["tasks"]}
        for tid in usable:
            if tid not in assigned:  # never let a finding silently drop out of the report
                min(body, key=lambda sec: len(sec["tasks"]))["tasks"].append(tid)
        outline = body + [sec for sec in outline if sec["synthesis"]][:1]
    return title, outline


async def _condense(llm: LLM, heading: str, ids: list[str], ledger: EvidenceLedger, budget: ContextBudget) -> str:
    """Map step: turn batches of evidence into short cited notes."""
    batches = [ids[i : i + budget.digest_batch] for i in range(0, len(ids), budget.digest_batch)]

    async def digest(batch: list[str]) -> str:
        cards = ledger.cards(batch, max_chars=budget.card_chars)
        _record_context(llm, "digest", cards)
        user = f"Section topic: {heading}\n\nEvidence:\n{cards}"
        reply = await llm.complete(prompts.DIGEST_SYSTEM, user, purpose="digest", json_mode=True)
        try:
            return str(parse_json_object(reply.text).get("notes", "")).strip()
        except LLMFormatError:
            return ""

    notes = await asyncio.gather(*(digest(b) for b in batches))
    return "\n".join(n for n in notes if n)


async def _evidence_block(
    llm: LLM, heading: str, ids: list[str], ledger: EvidenceLedger, budget: ContextBudget
) -> tuple[str, bool]:
    """Fit a section's evidence into its budget: full cards, shorter cards, or condensed notes."""
    if not ids:
        return "(none)", False
    per_card = min(budget.card_chars, budget.section_chars // len(ids))
    if per_card >= budget.min_card_chars:
        return ledger.cards(ids, max_chars=per_card), False
    notes = await _condense(llm, heading, ids, ledger, budget)
    return (notes or ledger.cards(ids[: budget.section_chars // budget.min_card_chars], max_chars=budget.min_card_chars)), True


async def write_section(
    llm: LLM,
    question: str,
    title: str,
    sec: dict,
    plan: Plan,
    findings: dict[str, Finding],
    ledger: EvidenceLedger,
    *,
    budget: ContextBudget,
    others: list[Section] | None = None,
    previous: str | None = None,
    problems: list[str] | None = None,
) -> Section:
    repair = ""
    if previous is not None and problems:
        repair = prompts.SECTION_REPAIR.format(previous=previous, problems="\n".join(f"- {p}" for p in problems))
    if sec["synthesis"]:
        summaries = "\n\n".join(f"### {o.heading}\n{o.body}" for o in others or [])
        user = f"Research question: {question}\n\nSection summaries:\n{summaries}{repair}"
        reply = await llm.complete(prompts.SYNTHESIS_SYSTEM, user, purpose="synthesis", json_mode=True)
    else:
        tasks = sec["tasks"] if budget.route else list(findings)
        evidence, condensed = await _evidence_block(llm, sec["heading"], evidence_for(tasks, findings), ledger, budget)
        _record_context(llm, "section", evidence)
        found = "\n".join(f"- {plan.get(t).question}\n  {findings[t].summary}" for t in sec["tasks"] if t in findings)
        user = prompts.SECTION_USER.format(
            question=question, title=title, heading=sec["heading"], findings=found or "(none)",
            evidence=("(condensed notes)\n" if condensed else "") + evidence, repair=repair,
        )
        reply = await llm.complete(prompts.SECTION_SYSTEM, user, purpose="section", json_mode=True)
    body = str(parse_json_object(reply.text).get("body", "")).strip()
    if not body:
        raise LLMFormatError(f'section "{sec["heading"]}" came back empty')
    return Section(sec["heading"], body, synthesis=sec["synthesis"])


async def write_sectioned_report(
    llm: LLM,
    question: str,
    plan: Plan,
    findings: dict[str, Finding],
    ledger: EvidenceLedger,
    *,
    budget: ContextBudget | None = None,
) -> tuple[Report, list[dict]]:
    budget = budget or ContextBudget()
    title, outline = await make_outline(llm, question, plan, findings)
    body_specs = [sec for sec in outline if not sec["synthesis"]]
    body = await asyncio.gather(
        *(write_section(llm, question, title, sec, plan, findings, ledger, budget=budget) for sec in body_specs)
    )
    sections = list(body)
    for sec in outline:
        if sec["synthesis"]:
            sections.append(await write_section(llm, question, title, sec, plan, findings, ledger,
                                                budget=budget, others=list(body)))
    return Report(title, sections), outline


async def repair_sections(
    llm: LLM,
    question: str,
    report: Report,
    outline: list[dict],
    plan: Plan,
    findings: dict[str, Finding],
    ledger: EvidenceLedger,
    problems: list[Issue],
    *,
    budget: ContextBudget | None = None,
) -> tuple[Report, list[str]]:
    """Rewrite only the sections that have problems; keep the rest verbatim.

    Returns the new report and the headings that were rewritten.
    """
    budget = budget or ContextBudget()
    by_heading: dict[str, list[str]] = {}
    for issue in problems:
        by_heading.setdefault(issue.section or "", []).append(issue.detail)
    specs = {sec["heading"]: sec for sec in outline}
    if "" in by_heading or not set(by_heading) <= set(specs):
        # Some problem is not tied to a known section: rewrite every section with all notes.
        notes = [n for ns in by_heading.values() for n in ns]
        by_heading = {sec["heading"]: notes for sec in outline}

    current = {s.heading: s for s in report.sections}
    targets = [h for h in specs if h in by_heading and not specs[h]["synthesis"]]
    rewritten = await asyncio.gather(*(
        write_section(llm, question, report.title, specs[h], plan, findings, ledger, budget=budget,
                      previous=current[h].body if h in current else None, problems=by_heading[h])
        for h in targets
    ))
    new = {s.heading: s for s in rewritten}
    sections = [new.get(sec["heading"], current.get(sec["heading"])) for sec in outline if not sec["synthesis"]]
    sections = [s for s in sections if s is not None]
    for sec in outline:
        if sec["synthesis"]:
            if new or sec["heading"] in by_heading or sec["heading"] not in current:
                prev = current.get(sec["heading"])
                sections.append(await write_section(
                    llm, question, report.title, sec, plan, findings, ledger, budget=budget, others=sections,
                    previous=prev.body if prev and sec["heading"] in by_heading else None,
                    problems=by_heading.get(sec["heading"]),
                ))
                targets.append(sec["heading"])
            else:
                sections.append(current[sec["heading"]])
    return Report(report.title, sections), targets


def strip_unknown_citations(report: Report, ledger: EvidenceLedger) -> Report:
    """Last-resort cleanup: drop citations that do not resolve to ledger entries."""

    def clean(body: str) -> str:
        return CITATION_RE.sub(lambda m: m.group(0) if m.group(1) in ledger else "", body)

    return Report(report.title, [Section(s.heading, clean(s.body), s.synthesis) for s in report.sections])


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
