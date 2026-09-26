"""Dense and hybrid retrieval over a local corpus: sentence embeddings in a Faiss index, fused with BM25.

* ``VectorCorpusSearch`` embeds the same paragraph chunks as the BM25 search and searches them by
  cosine similarity with a Faiss inner-product index. The index is cached on disk, keyed by the
  corpus contents and the embedding model, so it is only rebuilt when either changes.
* ``HybridSearch`` merges several searches with Reciprocal Rank Fusion: BM25 catches exact terms
  and rare names, embeddings catch paraphrases.

Requires the ``rag`` extra (faiss-cpu, numpy; sentence-transformers for the real embedder).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

import numpy as np

from .search import LocalCorpusSearch, SearchHit, SearchProvider, tokenize


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray:
        """L2-normalised float32 vectors, one row per text."""
        ...


class SentenceTransformerEmbedder:
    """A sentence-transformers model (downloaded from Hugging Face on first use)."""

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2", *, batch_size: int = 64) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model, device="cpu")
        self.name = model
        self.dim = self._model.get_sentence_embedding_dimension()
        self.batch_size = batch_size

    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = self._model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                     show_progress_bar=False, convert_to_numpy=True)
        return vectors.astype(np.float32)


class HashingEmbedder:
    """Deterministic bag-of-words embedder (feature hashing). For tests; no model download."""

    def __init__(self, dim: int = 256) -> None:
        self.name = f"hashing-{dim}"
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for token in tokenize(text):
                h = int(hashlib.md5(token.encode()).hexdigest(), 16)
                out[i, h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.maximum(norms, 1e-9)


class VectorCorpusSearch:
    name = "vector"

    def __init__(self, root: str | Path, embedder: Embedder, *, cache_dir: str | Path | None = None,
                 chunk_chars: int = 900) -> None:
        import faiss

        self.root = Path(root)
        self.embedder = embedder
        self.chunks = LocalCorpusSearch(root, chunk_chars=chunk_chars).chunks
        cache = Path(cache_dir) if cache_dir else self.root / ".deeptrace_index"
        fingerprint = hashlib.sha256(
            json.dumps([embedder.name, [(c.url, c.content) for c in self.chunks]]).encode()
        ).hexdigest()[:16]
        path = cache / f"{fingerprint}.faiss"
        self.built_from_cache = path.exists()
        if self.built_from_cache:
            self.index = faiss.read_index(str(path))
        else:
            vectors = embedder.encode([f"{c.title}\n{c.content}" for c in self.chunks])
            self.index = faiss.IndexFlatIP(vectors.shape[1])  # inner product on unit vectors = cosine
            self.index.add(vectors)
            cache.mkdir(parents=True, exist_ok=True)
            faiss.write_index(self.index, str(path))

    async def search(self, query: str, k: int) -> list[SearchHit]:
        scores, ids = self.index.search(self.embedder.encode([query]), k)
        return [self.chunks[i] for i, s in zip(ids[0], scores[0]) if i >= 0]


class HybridSearch:
    """Reciprocal Rank Fusion of several searches: score(d) = sum over lists of 1 / (k + rank)."""

    def __init__(self, *providers: SearchProvider, k: int = 60, depth: int = 20) -> None:
        self.providers = providers
        self.k = k
        self.depth = depth
        self.name = "hybrid(" + "+".join(p.name for p in providers) + ")"

    async def search(self, query: str, k: int) -> list[SearchHit]:
        scores: dict[str, float] = {}
        hits: dict[str, SearchHit] = {}
        for provider in self.providers:
            for rank, hit in enumerate(await provider.search(query, self.depth)):
                scores[hit.url] = scores.get(hit.url, 0.0) + 1.0 / (self.k + rank + 1)
                hits.setdefault(hit.url, hit)
        ranked = sorted(scores, key=lambda url: -scores[url])[:k]
        return [hits[url] for url in ranked]
