"""Dense (Faiss) and hybrid (RRF) retrieval, with a deterministic stand-in embedder."""

import asyncio

import pytest

pytest.importorskip("faiss")

from deeptrace_agent.retrieval import HashingEmbedder, HybridSearch, VectorCorpusSearch  # noqa: E402
from deeptrace_agent.search import LocalCorpusSearch, SearchHit  # noqa: E402


@pytest.fixture
def corpus(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "pumps.md").write_text("# Heat pumps\n\nA heat pump moves heat with a refrigerant cycle.\n\n"
                                   "Its coefficient of performance falls in cold weather.")
    (docs / "solar.md").write_text("# Solar\n\nPhotovoltaic panels convert sunlight into electricity.")
    (docs / "tea.md").write_text("# Tea\n\nGreen tea is made from unoxidised leaves.")
    return docs


def test_vector_search_ranks_the_relevant_chunk_first_and_caches_the_index(corpus, tmp_path):
    cache = tmp_path / "cache"
    first = VectorCorpusSearch(corpus, HashingEmbedder(), cache_dir=cache)
    hits = asyncio.run(first.search("refrigerant cycle heat pump", 2))
    assert hits[0].title == "Heat pumps" and not first.built_from_cache

    again = VectorCorpusSearch(corpus, HashingEmbedder(), cache_dir=cache)
    assert again.built_from_cache and again.index.ntotal == first.index.ntotal

    (corpus / "tea.md").write_text("# Tea\n\nBlack tea is fully oxidised.")  # corpus changed: rebuild
    assert not VectorCorpusSearch(corpus, HashingEmbedder(), cache_dir=cache).built_from_cache


def test_hashing_embedder_is_normalised_and_deterministic():
    e = HashingEmbedder(64)
    a, b = e.encode(["heat pump efficiency", "heat pump efficiency"])
    assert abs(float((a * a).sum()) - 1.0) < 1e-5 and (a == b).all()


class Fixed:
    def __init__(self, name, urls):
        self.name, self.urls = name, urls

    async def search(self, query, k):
        return [SearchHit(u, u, u) for u in self.urls[:k]]


def test_reciprocal_rank_fusion_rewards_documents_ranked_well_by_both():
    hybrid = HybridSearch(Fixed("a", ["x", "y", "z"]), Fixed("b", ["y", "w", "x"]))
    assert hybrid.name == "hybrid(a+b)"
    assert [h.url for h in asyncio.run(hybrid.search("q", 4))] == ["y", "x", "w", "z"]


def test_hybrid_over_bm25_and_vectors_runs_end_to_end(corpus, tmp_path):
    hybrid = HybridSearch(LocalCorpusSearch(corpus), VectorCorpusSearch(corpus, HashingEmbedder(), cache_dir=tmp_path))
    assert asyncio.run(hybrid.search("photovoltaic sunlight", 1))[0].title == "Solar"
