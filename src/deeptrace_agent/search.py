"""Search providers: a local BM25 corpus (no API key needed) and Tavily web search."""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]")
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have how in is it its of on or that the this to was what when "
    "where which who why will with does do did can".split()
)


@dataclass
class SearchHit:
    url: str
    title: str
    content: str


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, k: int) -> list[SearchHit]: ...


def tokenize(text: str) -> list[str]:
    """Lower-cased word tokens; CJK text is split per character."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


class LocalCorpusSearch:
    """BM25 over paragraph-sized chunks of the .md/.txt files in a directory."""

    name = "local"

    def __init__(self, root: str | Path, *, chunk_chars: int = 900, k1: float = 1.5, b: float = 0.75) -> None:
        self.root = Path(root)
        self.k1, self.b = k1, b
        self.chunks: list[SearchHit] = []
        for path in sorted(self.root.rglob("*")):
            if path.suffix.lower() in {".md", ".txt"} and path.is_file():
                self._add_file(path, chunk_chars)
        if not self.chunks:
            raise ValueError(f"no .md or .txt files found under {self.root}")

        self._docs = [Counter(tokenize(f"{c.title} {c.content}")) for c in self.chunks]
        self._lengths = [sum(d.values()) for d in self._docs]
        self._avg_len = sum(self._lengths) / len(self._lengths)
        df: Counter[str] = Counter()
        for doc in self._docs:
            df.update(doc.keys())
        n = len(self._docs)
        self._idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    def _add_file(self, path: Path, chunk_chars: int) -> None:
        text = path.read_text(encoding="utf-8", errors="ignore")
        title = path.stem.replace("_", " ").replace("-", " ")
        heading_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        if heading_match:
            title = heading_match.group(1).strip()

        buf: list[str] = []
        size = 0
        part = 0

        def flush() -> None:
            nonlocal buf, size, part
            if buf:
                rel = path.relative_to(self.root).as_posix()
                self.chunks.append(SearchHit(url=f"{rel}#{part}", title=title, content="\n\n".join(buf)))
                part += 1
                buf, size = [], 0

        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if not para:
                continue
            if size + len(para) > chunk_chars:
                flush()
            buf.append(para)
            size += len(para)
        flush()

    async def search(self, query: str, k: int) -> list[SearchHit]:
        terms = tokenize(query)
        scored: list[tuple[float, int]] = []
        for i, doc in enumerate(self._docs):
            norm = self.k1 * (1 - self.b + self.b * self._lengths[i] / self._avg_len)
            score = sum(
                self._idf[t] * doc[t] * (self.k1 + 1) / (doc[t] + norm) for t in terms if t in doc
            )
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [self.chunks[i] for _, i in scored[:k]]


class TavilySearch:
    """Web search through the Tavily API (https://tavily.com)."""

    name = "tavily"

    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0) -> None:
        key = api_key or os.getenv("TAVILY_API_KEY")
        if not key:
            raise ValueError("set TAVILY_API_KEY to use web search")
        self._client = httpx.AsyncClient(
            base_url="https://api.tavily.com",
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )

    async def search(self, query: str, k: int) -> list[SearchHit]:
        resp = await self._client.post("/search", json={"query": query, "max_results": k})
        resp.raise_for_status()
        return [
            SearchHit(url=r["url"], title=r.get("title") or r["url"], content=r.get("content") or "")
            for r in resp.json().get("results", [])
            if r.get("url")
        ]

    async def aclose(self) -> None:
        await self._client.aclose()
