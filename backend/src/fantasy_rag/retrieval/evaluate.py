"""`fantasy-rag eval news`: recall@k of each search mode on the labelled news questions."""

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection

from fantasy_rag.analytics.queries import AmbiguousPlayer, resolve_player
from fantasy_rag.config import REPO_ROOT
from fantasy_rag.index.embedder import Embedder
from fantasy_rag.retrieval.hybrid import Mode, Reranker, search

GOLDEN = REPO_ROOT / "backend" / "tests" / "eval" / "golden_news.yaml"
MODES: tuple[Mode, ...] = ("hybrid", "vector", "keyword")


def evaluate(
    conn: Connection,
    embedder: Embedder,
    k: int = 5,
    golden: Path = GOLDEN,
    reranker: Reranker | None = None,
) -> dict[str, Any]:
    items = yaml.safe_load(golden.read_text(encoding="utf-8"))["news"]
    named = [it["id"] for it in items if it.get("player_name")]
    out: dict[str, Any] = {"k": k, "questions": len(items), "player_name_questions": len(named)}
    runs: list[tuple[str, Mode, Reranker | None]] = [(m, m, None) for m in MODES]
    if reranker is not None:
        runs.append(("hybrid+rerank", "hybrid", reranker))
    for label, mode, rr in runs:
        rank: dict[str, int | None] = {}  # 1-based rank of the first relevant story in top 10
        for it in items:
            found = search(conn, embedder, it["question"], limit=10, mode=mode, reranker=rr)
            rank[it["id"]] = next(
                (i for i, h in enumerate(found, start=1) if h.url in it["relevant"]), None
            )

        def stats(ids: list[str], rank: dict[str, int | None] = rank) -> dict[str, float]:
            r = [rank[i] for i in ids]
            return {
                "recall_at_k": round(sum(1 for x in r if x and x <= k) / len(ids), 3),
                "recall_at_1": round(sum(1 for x in r if x == 1) / len(ids), 3),
                "mrr": round(sum(1 / x for x in r if x) / len(ids), 3),
            }

        out[label] = {
            "all": stats([it["id"] for it in items]),
            "player_name": stats(named),
            "missed_top_k": [i for i, x in rank.items() if not x or x > k],
        }
    # Bare player names (how a tool call often searches). With `+player_preference` the name
    # is resolved to a player ID and stories tagged with him are preferred.
    by_name = [it for it in items if it.get("name_query")]
    name_runs: list[tuple[str, Mode, bool]] = [
        ("vector", "vector", False),
        ("keyword", "keyword", False),
        ("hybrid", "hybrid", False),
        ("hybrid+player_preference", "hybrid", True),
    ]
    for label, mode, use_filter in name_runs:
        ranks = []
        for it in by_name:
            ids = [resolve_player(conn, it["name_query"]).id] if use_filter else None
            found = search(
                conn, embedder, it["name_query"], limit=10, mode=mode, prefer_players=ids
            )
            ranks.append(next((i for i, h in enumerate(found, 1) if h.url in it["relevant"]), None))
        out.setdefault("name_only", {"questions": len(by_name)})[label] = {
            "recall_at_k": round(sum(1 for x in ranks if x and x <= k) / len(ranks), 3),
            "recall_at_1": round(sum(1 for x in ranks if x == 1) / len(ranks), 3),
            "mrr": round(sum(1 / x for x in ranks if x) / len(ranks), 3),
        }
    return out


HELDOUT = REPO_ROOT / "backend" / "tests" / "eval" / "golden_news_heldout.yaml"


def evaluate_heldout(
    conn: Connection, path: Path = HELDOUT, reranker: Reranker | None = None
) -> dict[str, Any]:
    """Run each held-out question through the real `search_news` tool and grade it.

    With `reranker`, the tool runs with that reranker for this evaluation only.
    """
    from fantasy_rag.agent import tools

    saved, tools.NEWS_RERANKER = tools.NEWS_RERANKER, reranker
    try:
        return _heldout(conn, path, tools.run_tool)
    finally:
        tools.NEWS_RERANKER = saved


def _heldout(conn: Connection, path: Path, run_tool: Any) -> dict[str, Any]:
    items = yaml.safe_load(path.read_text(encoding="utf-8"))["heldout"]
    results: dict[str, dict[str, Any]] = {}
    for it in items:
        if it["expect"] == "phase6":  # judged on the written answer, not on retrieval
            continue
        args = {
            "query": it["question"],
            "players": [{"name": n} for n in it.get("players", [])],
            "days": it.get("days", 14),
        }
        try:
            r = run_tool(conn, "search_news", args)
        except AmbiguousPlayer as e:
            results[it["id"]] = {"ok": it["expect"] == "ambiguous", "got": f"ambiguous: {e}"}
            continue
        urls = [x["url"] for x in r["results"]]
        expect = it["expect"]
        ok = {
            "hit": bool(set(urls) & set(it.get("relevant", []))),
            "top1": bool(urls) and urls[0] in it.get("relevant", []),
            "ambiguous": False,
        }[expect]
        results[it["id"]] = {
            "ok": ok,
            "category": it["category"],
            "top": urls[:3],
            "top_similarity": r["results"][0]["similarity"] if r["results"] else None,
        }
    by_cat: dict[str, list[bool]] = {}
    for it in items:
        if it["id"] in results:
            by_cat.setdefault(it["category"], []).append(results[it["id"]]["ok"])
    return {
        "passed": sum(v["ok"] for v in results.values()),
        "total": len(results),
        "deferred_to_phase6": [it["id"] for it in items if it["expect"] == "phase6"],
        "by_category": {c: f"{sum(v)}/{len(v)}" for c, v in by_cat.items()},
        "failed": {k: v for k, v in results.items() if not v["ok"]},
    }
