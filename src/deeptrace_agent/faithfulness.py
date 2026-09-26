"""Claim-level faithfulness: does the cited evidence actually support each sentence?

The deterministic verifier only checks that citations *exist*. This module
checks that they *support* the claim they are attached to:

1. split the report into sentences and keep those that cite evidence,
2. ask a judge model, section by section, to label each claim against the full
   text of the evidence it cites (supported / partial / unsupported / contradicted),
3. turn every unsupported or contradicted claim into a precise repair note.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .ledger import EvidenceLedger
from .llm import LLM, LLMFormatError, parse_json_object
from .models import CITATION_RE, Issue, Report

LABELS = ("supported", "partial", "unsupported", "contradicted")
FAILING = ("unsupported", "contradicted")

# Sentence boundary: ., !, ? (or CJK equivalents) followed by whitespace or end,
# allowing a trailing citation group like "... is 3 [E2][E5]." to stay attached.
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+|(?<=[。！？])")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)

JUDGE_SYSTEM = """You check whether research claims are supported by the sources they cite.
For each claim, read ONLY the cited evidence text and label the claim:

- "supported": the evidence states or directly implies everything the claim says
- "partial": the evidence supports part of the claim, but some detail (a number, scope,
  qualifier, or causal link) goes beyond it
- "unsupported": the evidence does not say this (even if it might be true in general)
- "contradicted": the evidence says something incompatible with the claim

Judge strictly: general knowledge does not count, only the cited evidence.

Reply with JSON only:
{"verdicts": [{"id": 1, "label": "supported", "reason": "one short sentence"}]}"""


@dataclass
class Claim:
    id: int
    section: str
    text: str
    citations: list[str]


@dataclass
class Verdict:
    claim: Claim
    label: str
    reason: str


@dataclass
class FaithfulnessReport:
    verdicts: list[Verdict] = field(default_factory=list)

    def count(self, label: str) -> int:
        return sum(v.label == label for v in self.verdicts)

    @property
    def support_rate(self) -> float:
        """Share of cited claims fully supported by their evidence."""
        return self.count("supported") / len(self.verdicts) if self.verdicts else 1.0

    @property
    def failing(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.label in FAILING]

    def summary(self) -> dict[str, float]:
        out: dict[str, float] = {label: self.count(label) for label in LABELS}
        out["claims"] = len(self.verdicts)
        out["support_rate"] = round(self.support_rate, 4)
        return out

    def issues(self, *, include_partial: bool = False) -> list[Issue]:
        """Repair notes for failing claims, and optionally for claims that go beyond their evidence."""
        out = [
            Issue(
                "report",
                f"{v.label}_claim",
                f'In "{v.claim.section}", the claim "{_clip(v.claim.text, 220)}" is {v.label} by '
                f"{', '.join(v.claim.citations)}: {v.reason} Rewrite it to match the evidence or remove it.",
                note=f'"{_clip(CITATION_RE.sub("", v.claim.text).strip(), 220)}": not supported by the cited '
                f"source ({', '.join(v.claim.citations)}; {v.label}). {v.reason}",
                section=v.claim.section,
            )
            for v in self.failing
        ]
        if include_partial:
            out += [
                Issue(
                    "report",
                    "partial_claim",
                    f'In "{v.claim.section}", the claim "{_clip(v.claim.text, 220)}" goes beyond what '
                    f"{', '.join(v.claim.citations)} says: {v.reason} Narrow it to exactly what the evidence "
                    "states, keeping the citation, or drop the part the evidence does not support.",
                    section=v.claim.section,
                )
                for v in self.verdicts
                if v.label == "partial"
            ]
        return out

    def to_dict(self) -> dict:
        return {"summary": self.summary(), "verdicts": [asdict(v) for v in self.verdicts]}


def annotate_report(report: Report, verdicts: list[dict]) -> list[dict]:
    """Split each section into paragraphs of sentences, attaching the verdict of every judged claim.

    ``verdicts`` are dicts with ``section``, ``text``, ``label`` and ``reason`` (as stored on the
    run state). Used by the web console to colour sentences by how well their sources support them.
    """
    by_key = {(v["section"], " ".join(v["text"].split())): v for v in verdicts}
    out = []
    for section in report.sections:
        paragraphs = []
        for block in re.split(r"\n\s*\n", section.body.strip()):
            sentences = []
            for sentence in split_sentences(block):
                v = by_key.get((section.heading, " ".join(sentence.split())))
                sentences.append({
                    "text": sentence,
                    "citations": list(dict.fromkeys(CITATION_RE.findall(sentence))),
                    "label": v["label"] if v else None,
                    "reason": v["reason"] if v else None,
                })
            if sentences:
                paragraphs.append(sentences)
        out.append({"heading": section.heading, "synthesis": section.synthesis, "paragraphs": paragraphs})
    return out


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def split_sentences(body: str) -> list[str]:
    """Sentences and list items of a Markdown body, with trailing citations kept attached."""
    sentences: list[str] = []
    for block in re.split(r"\n\s*\n|\n", _LIST_ITEM_RE.sub("\n", body)):
        current: list[str] = []
        for piece in _SENTENCE_RE.split(block.strip()):
            piece = piece.strip()
            # "... is 3. [E2]" splits into "... is 3." and "[E2]": reattach the citation.
            if current and piece and not CITATION_RE.sub("", piece).strip(" .;,"):
                current[-1] += " " + piece
            elif piece:
                current.append(piece)
        sentences += current
    return sentences


def extract_claims(report: Report) -> list[Claim]:
    """Every sentence (or list item) that cites at least one evidence id."""
    claims: list[Claim] = []
    for section in report.sections:
        for sentence in split_sentences(section.body):
            cites = list(dict.fromkeys(CITATION_RE.findall(sentence)))
            if cites and len(CITATION_RE.sub("", sentence).strip(" .;,")) > 3:
                claims.append(Claim(len(claims) + 1, section.heading, sentence, cites))
    return claims


async def judge_claims(
    llm: LLM,
    claims: list[Claim],
    ledger: EvidenceLedger,
    *,
    batch_size: int = 8,
    evidence_chars: int = 1500,
) -> FaithfulnessReport:
    """Label claims in batches; each batch shows the full text of the evidence it cites."""
    result = FaithfulnessReport()
    for start in range(0, len(claims), batch_size):
        batch = claims[start : start + batch_size]
        cited = list(dict.fromkeys(c for claim in batch for c in claim.citations if c in ledger))
        evidence = "\n\n".join(
            f"[{eid}] {ledger.get(eid).title}\n{_clip(ledger.get(eid).content, evidence_chars)}" for eid in cited
        )
        listing = "\n".join(f"{c.id}. {CITATION_RE.sub('', c.text).strip()}  (cites {', '.join(c.citations)})"
                            for c in batch)
        user = f"Evidence:\n{evidence or '(none)'}\n\nClaims:\n{listing}"
        reply = await llm.complete(JUDGE_SYSTEM, user, purpose="judge", json_mode=True)
        try:
            raw = parse_json_object(reply.text).get("verdicts") or []
        except LLMFormatError:
            raw = []
        by_id = {int(v["id"]): v for v in raw if isinstance(v, dict) and str(v.get("id", "")).isdigit()}
        for claim in batch:
            v = by_id.get(claim.id, {})
            label = str(v.get("label", "")).lower()
            if claim.citations and not any(c in ledger for c in claim.citations):
                label, reason = "unsupported", "cites evidence that does not exist"
            elif label not in LABELS:
                # A judge that skips a claim must not silently pass it.
                label, reason = "unsupported", "the judge returned no verdict for this claim"
            else:
                reason = str(v.get("reason", "")).strip()
            result.verdicts.append(Verdict(claim, label, reason))
    return result


async def check_faithfulness(llm: LLM, report: Report, ledger: EvidenceLedger) -> FaithfulnessReport:
    return await judge_claims(llm, extract_claims(report), ledger)
