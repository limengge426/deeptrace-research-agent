"""Knowledge-graph retrieval (GraphRAG) on Neo4j.

Graph model::

    (:Article {slug, title})-[:HAS_CHUNK]->(:Chunk {id, url, title, content})-[:MENTIONS]->(:Entity {name})
    (:Entity)-[:RELATED {weight, kinds}]-(:Entity)

Entities and relations come from an extractor:

* ``LLMExtractor`` asks the model for entities and typed relations in each chunk (the usual GraphRAG
  approach; costs one LLM call per batch of chunks).
* ``CooccurrenceExtractor`` needs no model: article titles plus proper-noun phrases that recur across
  the corpus are entities, and entities mentioned in the same chunk are related, weighted by how often.

Retrieval links the entities named in a query, expands to their most strongly related neighbours,
and ranks chunks by the entities they mention (seed entities count more than neighbours).

Requires the ``rag`` extra (the ``neo4j`` driver) and a running Neo4j (NEO4J_URI/USER/PASSWORD).
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from .llm import LLM, LLMFormatError, parse_json_object
from .search import LocalCorpusSearch, SearchHit

_PROPER = re.compile(r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:[ -](?:of |de |von )?(?:[A-Z][a-z]+|[A-Z]{2,}|\d+))*\b")
_STOP_ENTITIES = frozenset("""
The This That These Those It Its In On At By For From With As An A And But Or If When While Although However
Some Many Most Other Such Both Each All Any No Not One Two Three First Second New Since After Before During
He She They We You His Her Their Our These Also There Here Where What Which Who Why How Figure Table See
January February March April May June July August September October November December
""".split())


@dataclass
class Extraction:
    entities: set[str] = field(default_factory=set)
    relations: list[tuple[str, str, str]] = field(default_factory=list)  # (source, kind, target)


class Extractor(Protocol):
    def extract(self, chunks: list[SearchHit]) -> list[Extraction]: ...


def _canon(name: str) -> str:
    return " ".join(name.replace("_", " ").split()).strip(" .,;:()'\"")


class CooccurrenceExtractor:
    """Entities: article titles and proper-noun phrases seen in at least ``min_count`` chunks."""

    def __init__(self, titles: Iterable[str] = (), *, min_count: int = 3) -> None:
        self.titles = {_canon(t) for t in titles}
        self.min_count = min_count

    def _candidates(self, text: str) -> set[str]:
        found = set()
        for match in _PROPER.finditer(text):
            name = _canon(match.group(0))
            if name.split()[0] in _STOP_ENTITIES:
                name = " ".join(name.split()[1:])  # "The Federal Reserve" -> "Federal Reserve"
            if len(name) >= 3 and name not in _STOP_ENTITIES and not name.isdigit():
                found.add(name)
        lower = text.lower()
        found |= {t for t in self.titles if t.lower() in lower}
        return found

    def extract(self, chunks: list[SearchHit]) -> list[Extraction]:
        per_chunk = [self._candidates(f"{c.title}. {c.content}") | {_canon(c.title)} for c in chunks]
        counts = Counter(name for names in per_chunk for name in names)
        keep = {n for n, k in counts.items() if k >= self.min_count} | self.titles
        out = []
        for names in per_chunk:
            ents = sorted(names & keep)
            rels = [(a, "co_occurs", b) for i, a in enumerate(ents) for b in ents[i + 1:]]
            out.append(Extraction(set(ents), rels))
        return out


EXTRACT_SYSTEM = """You build a knowledge graph from encyclopedia passages.
For each numbered passage, list the important named entities (people, organisations, places, events,
technologies, diseases, laws, concepts with proper names) and the relations between them stated in
the passage. Use short canonical names ("Federal Reserve", not "the Fed's board").

Reply with JSON only:
{"passages": [{"id": 1, "entities": ["..."], "relations": [["source", "relation", "target"]]}]}"""


class LLMExtractor:
    def __init__(self, llm: LLM, *, batch: int = 6) -> None:
        self.llm = llm
        self.batch = batch

    def extract(self, chunks: list[SearchHit]) -> list[Extraction]:
        return asyncio.run(self._extract(chunks))

    async def _extract(self, chunks: list[SearchHit]) -> list[Extraction]:
        out: list[Extraction] = []
        for start in range(0, len(chunks), self.batch):
            batch = chunks[start : start + self.batch]
            user = "\n\n".join(f"{i + 1}. [{c.title}] {c.content[:1200]}" for i, c in enumerate(batch))
            reply = await self.llm.complete(EXTRACT_SYSTEM, user, purpose="extract", json_mode=True)
            try:
                by_id = {int(p["id"]): p for p in parse_json_object(reply.text).get("passages", [])}
            except (LLMFormatError, KeyError, TypeError, ValueError):
                by_id = {}
            for i, c in enumerate(batch):
                p = by_id.get(i + 1, {})
                ents = {_canon(e) for e in p.get("entities", []) if isinstance(e, str) and _canon(e)}
                rels = [(_canon(r[0]), str(r[1]), _canon(r[2])) for r in p.get("relations", [])
                        if isinstance(r, list) and len(r) == 3]
                ents |= {_canon(c.title)} | {r[0] for r in rels} | {r[2] for r in rels}
                out.append(Extraction(ents, rels))
        return out


def _driver(uri: str, user: str, password: str):
    from neo4j import GraphDatabase

    return GraphDatabase.driver(uri, auth=(user, password))


def env_credentials() -> tuple[str, str, str]:
    uri = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    if not password:
        raise ValueError("set NEO4J_PASSWORD (and NEO4J_URI / NEO4J_USER) to use the knowledge graph")
    return uri, user, password


def build_graph(root: str | Path, extractor: Extractor, *, uri: str, user: str, password: str,
                chunk_chars: int = 900, reset: bool = True) -> dict[str, int]:
    """Load a corpus into Neo4j as articles, chunks, entities and relations."""
    chunks = LocalCorpusSearch(root, chunk_chars=chunk_chars).chunks
    extractions = extractor.extract(chunks)
    weights: Counter[tuple[str, str]] = Counter()
    kinds: dict[tuple[str, str], set[str]] = {}
    for ex in extractions:
        for a, kind, b in ex.relations:
            if a != b and a in ex.entities and b in ex.entities:
                key = tuple(sorted((a, b)))
                weights[key] += 1
                kinds.setdefault(key, set()).add(kind)
    rows = [{"id": c.url, "url": c.url, "title": c.title, "content": c.content, "article": c.url.split("#")[0],
             "entities": sorted(ex.entities)} for c, ex in zip(chunks, extractions)]
    edges = [{"a": a, "b": b, "w": w, "kinds": sorted(kinds[(a, b)])} for (a, b), w in weights.items()]

    with _driver(uri, user, password) as driver, driver.session() as s:
        if reset:
            s.run("MATCH (n) WHERE n:Article OR n:Chunk OR n:Entity DETACH DELETE n").consume()
        for stmt in ("CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
                     "CREATE CONSTRAINT entity_name IF NOT EXISTS FOR (e:Entity) REQUIRE e.name IS UNIQUE",
                     "CREATE CONSTRAINT article_slug IF NOT EXISTS FOR (a:Article) REQUIRE a.slug IS UNIQUE"):
            s.run(stmt).consume()
        for start in range(0, len(rows), 500):
            s.run("""
                UNWIND $rows AS r
                MERGE (a:Article {slug: r.article}) SET a.title = r.title
                MERGE (c:Chunk {id: r.id}) SET c.url = r.url, c.title = r.title, c.content = r.content
                MERGE (a)-[:HAS_CHUNK]->(c)
                WITH c, r UNWIND r.entities AS name
                MERGE (e:Entity {name: name})
                MERGE (c)-[:MENTIONS]->(e)
            """, rows=rows[start : start + 500]).consume()
        for start in range(0, len(edges), 2000):
            s.run("""
                UNWIND $edges AS x
                MATCH (a:Entity {name: x.a}), (b:Entity {name: x.b})
                MERGE (a)-[r:RELATED]-(b) SET r.weight = x.w, r.kinds = x.kinds
            """, edges=edges[start : start + 2000]).consume()
        counts = s.run("""
            MATCH (c:Chunk) WITH count(c) AS chunks
            MATCH (e:Entity) WITH chunks, count(e) AS entities
            OPTIONAL MATCH ()-[r:RELATED]->() RETURN chunks, entities, count(r) AS relations
        """).single()
    return dict(counts)


def link_entities(query: str, names: Iterable[str]) -> list[str]:
    """Entities whose name appears in the query (whole words, case-insensitive), longest first.

    Shorter names contained in a longer match ("Woods" inside "Bretton Woods") are dropped.
    """
    lower = f" {re.sub(r'[^a-z0-9]+', ' ', query.lower())} "
    hits = [n for n in names if f" {re.sub(r'[^a-z0-9]+', ' ', n.lower()).strip()} " in lower]
    hits.sort(key=len, reverse=True)
    kept: list[str] = []
    for n in hits:
        if not any(n.lower() in k.lower() for k in kept):
            kept.append(n)
    return kept


class GraphSearch:
    """Entity-linked graph retrieval over the Neo4j knowledge graph."""

    name = "graph"

    def __init__(self, uri: str, user: str, password: str, *, neighbours: int = 5, neighbour_weight: float = 0.3):
        self._driver = _driver(uri, user, password)
        self.neighbours = neighbours
        self.neighbour_weight = neighbour_weight
        with self._driver.session() as s:
            self._names = [r["name"] for r in s.run("MATCH (e:Entity) RETURN e.name AS name")]

    @classmethod
    def from_env(cls) -> "GraphSearch":
        return cls(*env_credentials())

    def close(self) -> None:
        self._driver.close()

    def seeds(self, query: str) -> list[str]:
        return link_entities(query, self._names)

    def _search(self, query: str, k: int) -> list[SearchHit]:
        seeds = self.seeds(query)
        if not seeds:
            return []
        with self._driver.session() as s:
            records = s.run("""
                MATCH (e:Entity) WHERE e.name IN $seeds
                CALL (e) {
                    MATCH (e)-[r:RELATED]-(n:Entity)
                    RETURN n ORDER BY r.weight DESC LIMIT $neighbours
                }
                WITH collect(DISTINCT e) AS seedNodes, collect(DISTINCT n) AS near
                MATCH (c:Chunk)-[:MENTIONS]->(x:Entity)
                WHERE x IN seedNodes OR x IN near
                WITH c, sum(CASE WHEN x IN seedNodes THEN 1.0 ELSE $nw END) AS score
                RETURN c.url AS url, c.title AS title, c.content AS content
                ORDER BY score DESC, c.url LIMIT $k
            """, seeds=seeds, neighbours=self.neighbours, nw=self.neighbour_weight, k=k)
            return [SearchHit(r["url"], r["title"], r["content"]) for r in records]

    async def search(self, query: str, k: int) -> list[SearchHit]:
        return await asyncio.to_thread(self._search, query, k)
