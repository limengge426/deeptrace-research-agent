"""Deterministic stand-ins for the LLM and search provider."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from typing import Any, Callable

from researchloop.llm import Completion
from researchloop.search import SearchHit

Handler = Callable[[str, str], Any]


def _sub_question(user: str) -> str:
    return re.search(r"Sub-question: (.+)", user).group(1).strip()


def plan_three(system: str, user: str) -> dict:
    return {
        "tasks": [
            {"id": "t1", "question": "What is a heat pump?", "depends_on": []},
            {"id": "t2", "question": "How efficient are heat pumps?", "depends_on": []},
            {"id": "t3", "question": "Are heat pumps worth it in cold climates?", "depends_on": ["t1", "t2"]},
        ]
    }


def queries(system: str, user: str) -> dict:
    q = _sub_question(user)
    return {"queries": [q, f"{q} evidence"]}


def finding(system: str, user: str) -> dict:
    ids = re.findall(r"^\[(E\d+)\]", user, re.MULTILINE)
    return {"summary": f"{_sub_question(user)} Answered by [{ids[0]}].", "used": ids[:2]}


def report(system: str, user: str) -> dict:
    evidence = user.split("Evidence:", 1)[1]
    ids = re.findall(r"^\[(E\d+)\]", evidence, re.MULTILINE)
    return {
        "title": "Heat pumps",
        "sections": [{"heading": f"Point {i}", "body": f"A sourced claim [{eid}]."} for i, eid in enumerate(ids)],
    }


def replan(system: str, user: str) -> dict:
    prefix = re.search(r'ids starting with "([^"]+)"', user).group(1)
    return {"tasks": [{"id": f"{prefix}1", "question": "Heat pump performance below -15C", "depends_on": []}]}


def outline(system: str, user: str) -> dict:
    tasks = re.findall(r"^- \[(\S+?)\]", user, re.MULTILINE)
    return {
        "title": "Heat pumps",
        "sections": [{"heading": f"About {t}", "tasks": [t]} for t in tasks] + [{"heading": "Conclusion", "tasks": []}],
    }


def section(system: str, user: str) -> dict:
    evidence = user.split("Evidence:", 1)[1].split("Your previous draft", 1)[0]
    ids = list(dict.fromkeys(re.findall(r"\[(E\d+)\]", evidence)))
    return {"body": " ".join(f"A sourced claim [{e}]." for e in ids) or "Nothing to add."}


def digest(system: str, user: str) -> dict:
    ids = re.findall(r"^\[(E\d+)\]", user, re.MULTILINE)
    return {"notes": "\n".join(f"- condensed fact [{e}]" for e in ids)}


def judge_all(user: str, label: str) -> dict:
    ids = re.findall(r"^(\d+)\. ", user.split("Claims:", 1)[1], re.MULTILINE)
    return {"verdicts": [{"id": int(i), "label": label, "reason": "scripted"} for i in ids]}


def plan_or_replan(system: str, user: str) -> dict:
    return replan(system, user) if "verifier found these gaps" in user else plan_three(system, user)


DEFAULT_HANDLERS: dict[str, Handler] = {
    "plan": plan_or_replan,
    "queries": queries,
    "finding": finding,
    "report": report,
    "outline": outline,
    "section": section,
    "synthesis": lambda s, u: {"body": "In short, the sections above answer the question."},
    "digest": digest,
    "critic": lambda s, u: {"gaps": []},
    "judge": lambda s, u: judge_all(u, "supported"),
}


class ScriptedLLM:
    """Answers each call with the handler registered for its ``purpose``.

    Handlers may return a dict (serialized to JSON), a string, or raise.
    """

    def __init__(self, **overrides: Handler) -> None:
        self.handlers = {**DEFAULT_HANDLERS, **overrides}
        self.calls: Counter[str] = Counter()

    async def complete(self, system: str, user: str, *, purpose: str, json_mode: bool = False) -> Completion:
        self.calls[purpose] += 1
        out = self.handlers[purpose](system, user)
        text = out if isinstance(out, str) else json.dumps(out)
        return Completion(text=text, tokens=100)


class FakeSearch:
    name = "fake"

    def __init__(self, *, empty_for: tuple[str, ...] = (), delay: float = 0.0) -> None:
        self.empty_for = empty_for
        self.delay = delay
        self.calls: Counter[str] = Counter()
        self.in_flight = 0
        self.max_in_flight = 0

    async def search(self, query: str, k: int) -> list[SearchHit]:
        self.calls[query] += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if any(word in query for word in self.empty_for):
                return []
            slug = re.sub(r"\W+", "-", query.lower()).strip("-")
            return [
                SearchHit(url=f"https://example.org/{slug}/{i}", title=f"{query} #{i}", content=f"Facts about {query} ({i}).")
                for i in range(2)
            ]
        finally:
            self.in_flight -= 1
