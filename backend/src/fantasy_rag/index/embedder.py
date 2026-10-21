"""Embedder protocol plus the local BAAI/bge-small-en-v1.5 implementation (384 dims).

The model runs on CPU through fastembed (ONNX); it downloads once (~130 MB) into
data/cache/fastembed. Swap implementations (e.g. Voyage) behind the same protocol.
"""

from typing import Protocol

from fantasy_rag.config import REPO_ROOT

DIM = 384


class Embedder(Protocol):
    model_name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


class LocalBGE:
    model_name = "BAAI/bge-small-en-v1.5"

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # heavy import, only when actually embedding

        self._model = TextEmbedding(
            self.model_name, cache_dir=str(REPO_ROOT / "data" / "cache" / "fastembed")
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(texts)]

    def embed_query(self, query: str) -> list[float]:
        """Questions use the model's retrieval-query form, unlike stored passages."""
        return [float(x) for x in next(iter(self._model.query_embed(query)))]
