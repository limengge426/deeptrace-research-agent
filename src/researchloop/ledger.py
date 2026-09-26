"""Append-only evidence ledger.

Every search hit that enters a run gets a stable id (E1, E2, ...). Findings and
the final report may only cite these ids, which is what lets the verifier check
that each claim traces back to a real source.
"""

from __future__ import annotations

import hashlib

from .models import Evidence
from .search import SearchHit


def _fingerprint(hit: SearchHit) -> str:
    normalized = " ".join(hit.content.split()).lower()
    return hashlib.sha256(f"{hit.url}\n{normalized}".encode()).hexdigest()


class EvidenceLedger:
    def __init__(self, items: list[Evidence] | None = None) -> None:
        self._items: dict[str, Evidence] = {}
        self._by_fingerprint: dict[str, str] = {}
        for ev in items or []:
            self._items[ev.id] = ev
            self._by_fingerprint[_fingerprint(SearchHit(ev.url, ev.title, ev.content))] = ev.id

    def add(self, hit: SearchHit, *, task_id: str, query: str) -> Evidence:
        """Record a hit, returning the existing entry if the same source was seen before."""
        fp = _fingerprint(hit)
        if fp in self._by_fingerprint:
            return self._items[self._by_fingerprint[fp]]
        ev = Evidence(
            id=f"E{len(self._items) + 1}",
            url=hit.url,
            title=hit.title,
            content=hit.content,
            task_id=task_id,
            query=query,
        )
        self._items[ev.id] = ev
        self._by_fingerprint[fp] = ev.id
        return ev

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._items

    def __len__(self) -> int:
        return len(self._items)

    def get(self, evidence_id: str) -> Evidence:
        return self._items[evidence_id]

    def items(self) -> list[Evidence]:
        return list(self._items.values())

    def cards(self, ids: list[str], *, max_chars: int = 600) -> str:
        return "\n\n".join(self._items[i].card(max_chars) for i in ids if i in self._items)
