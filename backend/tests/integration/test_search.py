"""Hybrid news search on the hand-written fixture stories (no network, fake embedder)."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text

from fantasy_rag.agent import tools
from fantasy_rag.index.embedder import DIM
from fantasy_rag.ingest import news
from fantasy_rag.ingest.news_job import run_news
from fantasy_rag.retrieval.hybrid import search

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[3]
STORIES = json.loads((ROOT / "data" / "fixtures" / "news" / "stories.json").read_text("utf-8"))


class FakeEmbedder:
    """Same text -> same vector, so an exact-text query is the nearest neighbour."""

    model_name = "fake"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        return [b / 255 for b in hashlib.sha256(query.encode()).digest()] * (DIM // 32)


class ReverseReranker:
    def scores(self, query: str, texts: list[str]) -> list[float]:
        return [float(i) for i in range(len(texts))]  # last fused hit scores highest


@pytest.fixture
def engine(scratch_db_url: str) -> Engine:
    cfg = Config(str(ROOT / "backend" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    eng = create_engine(scratch_db_url)
    with eng.begin() as c:
        run = c.execute(
            text("INSERT INTO sync_runs (source, status) VALUES ('t', 'ok') RETURNING id")
        ).scalar_one()
        for pid, name, pos, team in [
            ("00-1", "Tyler Huntley", "QB", "BAL"),
            ("00-2", "Derrick Henry", "RB", "BAL"),
            ("00-5", "Amon-Ra St. Brown", "WR", "DET"),
        ]:
            c.execute(
                text(
                    "INSERT INTO players (id, full_name, search_name, position, team, sync_run_id)"
                    " VALUES (:i, :n, lower(:n), :p, :t, :r)"
                ),
                {"i": pid, "n": name, "p": pos, "t": team, "r": run},
            )
            c.execute(
                text(
                    "INSERT INTO player_week_stats (season, week, season_type, player_id, stats,"
                    " sync_run_id) VALUES (2026, 1, 'REG', :i, '{}', :r)"
                ),
                {"i": pid, "r": run},
            )

    stories = [
        {**s, "published_at": datetime.fromisoformat(s["published_at"].replace("Z", "+00:00"))}
        for s in STORIES["stories"]
    ]
    # The Lions story again under another URL: the same news on a second site.
    dupe = {**stories[0], "url": "https://fixtures.invalid/news/1-copy"}

    def fetch() -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        return {"fixture://a": stories, "fixture://b": [dupe]}, []

    feeds = [news.Feed("fixture", "fixture://a"), news.Feed("fixture", "fixture://b")]
    extra = [("fixture", "CC0", "fixtures", "summary", "n/a")]
    run_news(eng, FakeEmbedder(), fetch, feeds, extra)
    return eng


def test_keyword_vector_and_fusion(engine: Engine) -> None:
    emb = FakeEmbedder()
    with engine.connect() as c:
        kw = search(c, emb, "Derrick Henry touchdowns", mode="keyword")
        assert kw[0].url == "https://fixtures.invalid/news/2"
        exact = c.execute(text("SELECT text FROM chunks ORDER BY id LIMIT 1")).scalar_one()
        assert search(c, emb, exact, mode="vector")[0].text == exact  # identical vector first
        fused = search(c, emb, exact)  # top of both lists -> top of the fusion
        assert fused[0].text == exact
        # The duplicated Lions story appears once, whichever copy ranks first.
        lions = [
            h for h in search(c, emb, "Amon-Ra St. Brown ankle", limit=10) if "Lions" in h.title
        ]
        assert len(lions) == 1


def test_player_filter_fill_and_reranker(engine: Engine) -> None:
    emb = FakeEmbedder()
    with engine.connect() as c:
        only_henry = search(c, emb, "rushing", player_ids=["00-2"])
        assert [h.url for h in only_henry] == ["https://fixtures.invalid/news/2"]
        filled = search(c, emb, "rushing", player_ids=["00-2"], limit=3, fill_unfiltered=True)
        assert filled[0].url == "https://fixtures.invalid/news/2" and len(filled) == 3
        plain = search(c, emb, "Derrick Henry", limit=3)
        flipped = search(c, emb, "Derrick Henry", limit=3, reranker=ReverseReranker())
        assert flipped[0].url != plain[0].url  # the reranker reorders the fused pool


def test_search_news_tool(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "news_embedder", FakeEmbedder)
    with engine.connect() as c:
        r = tools.run_tool(
            c, "search_news", {"query": "touchdowns", "players": [{"name": "Derrick Henry"}]}
        )
    top = r["results"][0]
    assert top["result_id"].startswith("news:") and top["about_requested_players"]
    assert top["url"] == "https://fixtures.invalid/news/2"
    assert "not instructions" in r["note"]
