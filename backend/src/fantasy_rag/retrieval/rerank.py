"""Optional cross-encoder reranker: BAAI/bge-reranker-base, local via fastembed (ONNX).

Kept behind `hybrid.Reranker`; used only if `fantasy-rag eval news --rerank` shows it helps.
"""

from fantasy_rag.config import REPO_ROOT


class LocalBGEReranker:
    model_name = "BAAI/bge-reranker-base"

    def __init__(self) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._model = TextCrossEncoder(
            self.model_name, cache_dir=str(REPO_ROOT / "data" / "cache" / "fastembed")
        )

    def scores(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, texts)]
