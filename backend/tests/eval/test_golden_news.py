"""Live check (PLAN Phase 5 "Done when"), on the ingested dev DB with the real embedder:
recall@5 >= 0.8 on the labelled news questions, and keyword search beats vector-only on
player-name lookups."""

import pytest

from fantasy_rag.db import get_readonly_engine
from fantasy_rag.index.embedder import LocalBGE
from fantasy_rag.retrieval.evaluate import evaluate, evaluate_heldout

pytestmark = [pytest.mark.live, pytest.mark.db]


def test_news_retrieval_meets_targets() -> None:
    with get_readonly_engine().connect() as conn:
        r = evaluate(conn, LocalBGE())
    print("\n", {m: r[m]["all"] for m in ("hybrid", "vector", "keyword")}, r["name_only"])
    assert r["hybrid"]["all"]["recall_at_k"] >= 0.8
    assert r["name_only"]["keyword"]["mrr"] > r["name_only"]["vector"]["mrr"]


def test_heldout_news_cases_through_the_tool() -> None:
    """Harder held-out cases via `search_news` (9/9 on Oct 20, 2026). Guards regressions."""
    with get_readonly_engine().connect() as conn:
        r = evaluate_heldout(conn)
    print("\n", r["by_category"], "failed:", list(r["failed"]))
    assert r["passed"] == r["total"], r["failed"]
