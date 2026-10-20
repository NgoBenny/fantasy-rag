"""Live check (PLAN Phase 5 "Done when"), on the ingested dev DB with the real embedder:
recall@5 >= 0.8 on the labelled news questions, and keyword search beats vector-only on
player-name lookups."""

import pytest

from fantasy_rag.db import get_readonly_engine
from fantasy_rag.index.embedder import LocalBGE
from fantasy_rag.retrieval.evaluate import evaluate

pytestmark = [pytest.mark.live, pytest.mark.db]


def test_news_retrieval_meets_targets() -> None:
    with get_readonly_engine().connect() as conn:
        r = evaluate(conn, LocalBGE())
    print("\n", {m: r[m]["all"] for m in ("hybrid", "vector", "keyword")}, r["name_only"])
    assert r["hybrid"]["all"]["recall_at_k"] >= 0.8
    assert r["name_only"]["keyword"]["mrr"] > r["name_only"]["vector"]["mrr"]
