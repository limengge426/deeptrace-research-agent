"""Build search providers and researchers from configuration (shared by the CLI and the API server)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .search import LocalCorpusSearch, SearchProvider, TavilySearch

RETRIEVAL = ("bm25", "vector", "hybrid", "graph", "hybrid+graph")
RESEARCHERS = ("pipeline", "agent")


def make_search(corpus: str | Path | None, retrieval: str = "bm25") -> SearchProvider:
    """Tavily web search without a corpus; otherwise the chosen retrieval over the local corpus.

    vector/hybrid need the ``rag`` extra; graph needs a Neo4j built with ``deeptrace graph build``.
    """
    if retrieval not in RETRIEVAL:
        raise ValueError(f"retrieval must be one of {', '.join(RETRIEVAL)}")
    if not corpus:
        if retrieval != "bm25":
            raise ValueError("--retrieval applies to a local corpus; web search always uses Tavily")
        return TavilySearch()
    bm25 = LocalCorpusSearch(corpus)
    if retrieval == "bm25":
        return bm25
    providers: list[SearchProvider] = []
    if retrieval in ("vector", "hybrid", "hybrid+graph"):
        from .retrieval import SentenceTransformerEmbedder, VectorCorpusSearch

        vector = VectorCorpusSearch(corpus, SentenceTransformerEmbedder())
        if retrieval == "vector":
            return vector
        providers += [bm25, vector]
    if retrieval in ("graph", "hybrid+graph"):
        from .graph import GraphSearch

        graph = GraphSearch.from_env()
        if retrieval == "graph":
            return graph
        providers.append(graph)
    from .retrieval import HybridSearch

    return HybridSearch(*providers)


def make_researcher(kind: str = "pipeline") -> Any:
    """None for the fixed pipeline, or a LangGraph AgentResearcher on the configured chat model."""
    if kind not in RESEARCHERS:
        raise ValueError(f"researcher must be one of {', '.join(RESEARCHERS)}")
    if kind == "pipeline":
        return None
    from .agent import AgentResearcher, chat_model_from_env

    return AgentResearcher(chat_model_from_env())
