"""News job on hand-written fixture stories: documents, tags, chunks, vectors, re-run no-op."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from fantasy_rag.index.embedder import DIM
from fantasy_rag.ingest import news
from fantasy_rag.ingest.news_job import coverage, run_news

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[3]
STORIES = json.loads((ROOT / "data" / "fixtures" / "news" / "stories.json").read_text("utf-8"))


class FakeEmbedder:
    """Deterministic 384-d vectors from a hash: no model download in CI."""

    model_name = "fake"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[b / 255 for b in hashlib.sha256(t.encode()).digest()] * (DIM // 32) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        return self.embed([query])[0]


FEEDS = [news.Feed("fixture", "fixture://full"), news.Feed("fixture_link", "fixture://link")]
EXTRA = [
    ("fixture", "hand-written test content, CC0", "fixtures", "summary", "n/a"),
    ("fixture_link", "test: a source we may only link to", "fixtures", "link", "n/a"),
]


def _fetch() -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    stories = [
        {**s, "published_at": datetime.fromisoformat(s["published_at"].replace("Z", "+00:00"))}
        for s in STORIES["stories"]
    ]
    link_only = [{**stories[0], "url": "https://fixtures.invalid/news/link-only"}]
    return {"fixture://full": stories, "fixture://link": link_only}, []


def test_news_job_end_to_end(scratch_db_url: str) -> None:
    cfg = Config(str(ROOT / "backend" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    engine = create_engine(scratch_db_url)
    with engine.begin() as c:
        run = c.execute(
            text("INSERT INTO sync_runs (source, status) VALUES ('t', 'ok') RETURNING id")
        ).scalar_one()
        for pid, name, pos, team in [
            ("00-1", "Tyler Huntley", "QB", "BAL"),
            ("00-2", "Derrick Henry", "RB", "BAL"),
            ("00-3", "Mike Williams", "WR", "NYJ"),
            ("00-4", "Mike Williams", "TE", "WAS"),
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

    first = run_news(engine, FakeEmbedder(), _fetch, FEEDS, EXTRA)
    assert (first.stories, first.chunks_written, first.embedded) == (5, 5, 5)
    assert first.ambiguous_names == ["mike williams"]  # two of them, no team in the story

    with engine.connect() as c:
        docs = {
            u: (s, p, t)
            for u, s, p, t in c.execute(
                text("SELECT url, summary, player_ids, teams FROM documents")
            )
        }
        dims = c.execute(text("SELECT DISTINCT vector_dims(embedding) FROM chunks")).scalars().all()
        report = coverage(c)
    assert docs["https://fixtures.invalid/news/2"][1] == ["00-1", "00-2"]  # back-to-back names
    assert docs["https://fixtures.invalid/news/2"][2] == ["BAL"]
    assert docs["https://fixtures.invalid/news/1"][1] == ["00-5"]
    assert docs["https://fixtures.invalid/news/3"][1] == []  # ambiguous: not guessed
    assert docs["https://fixtures.invalid/news/link-only"][0] is None  # registry: link only
    assert dims == [DIM]
    assert report["documents_by_source"] == [("fixture", 4, 4), ("fixture_link", 1, 1)]

    again = run_news(engine, FakeEmbedder(), _fetch, FEEDS, EXTRA)
    assert (again.documents_changed, again.chunks_written, again.embedded) == (0, 0, 0)
    engine.dispose()


class BrokenEmbedder:
    model_name = "broken"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("model crashed")

    def embed_query(self, query: str) -> list[float]:
        raise RuntimeError("model crashed")


def test_storage_limits_hold_and_failures_are_recorded(scratch_db_url: str) -> None:
    cfg = Config(str(ROOT / "backend" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    engine = create_engine(scratch_db_url)

    # 1. Every feed down: the run is recorded as failed, not silently skipped.
    with pytest.raises(RuntimeError, match="every news feed failed"):
        run_news(engine, FakeEmbedder(), lambda: ({}, ["all down"]), FEEDS, EXTRA)

    # 2. Embedding breaks: ingest is ok, the embed run is failed, chunks stay unembedded.
    with pytest.raises(RuntimeError, match="model crashed"):
        run_news(engine, BrokenEmbedder(), _fetch, FEEDS, EXTRA)
    with engine.connect() as c:
        runs = dict(c.execute(text("SELECT source, status FROM sync_runs ORDER BY id")).all())
        missing = c.execute(text("SELECT count(*) FROM chunks WHERE embedding IS NULL")).scalar()
    assert runs == {"news": "ok", "news.embed": "failed"}  # the last row per source wins
    assert missing == 5
    assert run_news(engine, FakeEmbedder(), _fetch, FEEDS, EXTRA).embedded == 5  # caught up

    # 3. A source tightened to link-only stays that way, and loses stored summaries.
    with engine.begin() as c:
        c.execute(text("UPDATE sources SET storage_allowed = 'link' WHERE name = 'fixture'"))
    run_news(engine, FakeEmbedder(), _fetch, FEEDS, EXTRA)
    with engine.connect() as c:
        policy = c.execute(text("SELECT storage_allowed FROM sources WHERE name = 'fixture'"))
        kept = c.execute(
            text("SELECT count(*) FROM documents WHERE source = 'fixture' AND summary IS NOT NULL")
        ).scalar()
        chunk_text = c.execute(
            text(
                "SELECT c.text FROM chunks c JOIN documents d ON d.id = c.document_id"
                " WHERE d.url = 'https://fixtures.invalid/news/1'"
            )
        ).scalar()
    assert policy.scalar() == "link" and kept == 0
    assert chunk_text == STORIES["stories"][0]["title"]  # rebuilt without the summary
    engine.dispose()
