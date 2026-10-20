"""`fantasy-rag ingest news`: feeds -> documents -> chunks -> embeddings. Safe to re-run."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import polars as pl
from sqlalchemy import Connection, Engine, text

from fantasy_rag.index.chunker import chunk
from fantasy_rag.index.embedder import Embedder
from fantasy_rag.ingest import news
from fantasy_rag.ingest.base import sync_run, upsert
from fantasy_rag.ingest.entity_link import Linker, Player, norm

Fetched = dict[str, list[dict[str, Any]]]


@dataclass
class NewsReport:
    stories: int = 0
    documents_changed: int = 0
    chunks_written: int = 0
    embedded: int = 0
    ambiguous_names: list[str] = field(default_factory=list)
    feed_errors: list[str] = field(default_factory=list)


def _linker(conn: Connection) -> Linker:
    """Current fantasy players: skill positions with recent stats or on a fantasy roster."""
    rows = conn.execute(
        text(
            """
            SELECT p.id, p.full_name, p.team FROM players p
            WHERE (p.position IN ('QB','RB','WR','TE','K','FB')
                   OR p.fantasy_positions && ARRAY['QB','RB','WR','TE','K'])
              AND (EXISTS (SELECT 1 FROM player_week_stats s WHERE s.player_id = p.id
                           AND s.season >= (SELECT max(season) - 1 FROM player_week_stats))
                   OR EXISTS (SELECT 1 FROM fantasy_rosters r WHERE p.id = ANY(r.player_ids)))
            """
        )
    )
    players = [Player(pid, norm(name).strip(), team) for pid, name, team in rows]
    espn = {
        str(ext): str(pid)
        for ext, pid in conn.execute(
            text("SELECT external_id, player_id FROM player_external_ids WHERE provider = 'espn'")
        )
    }
    return Linker(players, espn)


def _arr(values: list[str]) -> str:
    return "{" + ",".join(values) + "}"


def run_news(
    engine: Engine,
    embedder: Embedder,
    fetch: Callable[[], tuple[Fetched, list[str]]] = news.fetch_all,
    feeds: list[news.Feed] = news.FEEDS,
    extra_sources: list[tuple[str, str, str, str]] = (),  # type: ignore[assignment]
) -> NewsReport:
    report = NewsReport()
    fetched, report.feed_errors = fetch()
    if not fetched:
        raise RuntimeError(f"every news feed failed: {report.feed_errors[:3]}")
    by_url = {f.url: f for f in feeds}

    with sync_run(engine, "news") as run:
        conn = run.conn
        registry = pl.DataFrame(
            [(n, lic, att, "summary", "every 30-60 min in season") for n, lic, att in news.SOURCES]
            + [tuple(s) for s in extra_sources],
            schema=["name", "license", "attribution", "storage_allowed", "refresh_cadence"],
            orient="row",
        )
        upsert(conn, "sources", registry, ["name"], None)
        allowed = {
            str(n): str(a)
            for n, a in conn.execute(text("SELECT name, storage_allowed FROM sources"))
        }
        linker = _linker(conn)

        docs: dict[str, dict[str, Any]] = {}  # url -> row; first feed to carry a story wins
        for feed_url, stories in fetched.items():
            feed = by_url[feed_url]
            for s in stories:
                if s["url"] in docs:
                    continue
                body = f"{s['title']}. {s['summary']}"
                teams = linker.teams_in(body, s["team_names"] + ([feed.team] if feed.team else []))
                players, ambiguous = linker.players_in(
                    body + " " + " ".join(s["tags"]), teams, s["espn_athlete_ids"]
                )
                report.ambiguous_names += ambiguous
                keep = allowed.get(feed.source, "link")
                docs[s["url"]] = {
                    "source": feed.source,
                    "url": s["url"],
                    "title": s["title"],
                    "summary": s["summary"] if keep in ("summary", "full") else None,
                    "published_at": s["published_at"],
                    "teams": _arr(teams),
                    "player_ids": _arr(players),
                }
        report.stories = len(docs)
        frame = pl.DataFrame(list(docs.values()), schema_overrides={"summary": pl.String})
        report.documents_changed = upsert(conn, "documents", frame, ["url"], run.id)
        report.chunks_written = _rechunk(conn, list(docs), linker)
        run.rows = report.documents_changed

    report.embedded = embed_missing(engine, embedder)
    return report


def _rechunk(conn: Connection, urls: list[str], linker: Linker) -> int:
    """Rebuild chunks for these stories only where the text or tags changed."""
    rows = conn.execute(
        text(
            """
            SELECT d.id, d.title, d.summary, d.player_ids, d.published_at,
                   coalesce(json_agg(c.text ORDER BY c.id) FILTER (WHERE c.id IS NOT NULL),
                            '[]'),
                   -- json, not array_agg: Postgres can't stack empty text[] values
                   coalesce(json_agg(c.player_ids ORDER BY c.id) FILTER (WHERE c.id IS NOT NULL),
                            '[]')
            FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
            WHERE d.url = ANY(:urls) GROUP BY d.id
            """
        ),
        {"urls": urls},
    ).fetchall()
    written = 0
    for doc_id, title, summary, story_players, published, old_text, old_players in rows:
        pieces = chunk(title, summary or "")
        tagged = [linker.chunk_players(p, story_players) for p in pieces]
        if story_players and pieces and not any(tagged):
            tagged[0] = list(story_players)  # tagged by ID (ESPN) but not named in the text
        else:  # a story player not named in any chunk still belongs to the first one
            missing = sorted(set(story_players) - {p for t in tagged for p in t})
            if missing and tagged:
                tagged[0] = sorted(set(tagged[0]) | set(missing))
        if old_text == pieces and [sorted(p) for p in old_players] == [sorted(t) for t in tagged]:
            continue
        conn.execute(text("DELETE FROM chunks WHERE document_id = :d"), {"d": doc_id})
        for piece, players in zip(pieces, tagged, strict=True):
            conn.execute(
                text(
                    "INSERT INTO chunks (document_id, text, player_ids, published_at)"
                    " VALUES (:d, :t, :p, :at)"
                ),
                {"d": doc_id, "t": piece, "p": players, "at": published},
            )
            written += 1
    # A story whose date changed keeps its chunks; their date must follow.
    conn.execute(
        text(
            "UPDATE chunks c SET published_at = d.published_at FROM documents d"
            " WHERE c.document_id = d.id AND d.url = ANY(:urls)"
            " AND c.published_at IS DISTINCT FROM d.published_at"
        ),
        {"urls": urls},
    )
    return written


def embed_missing(engine: Engine, embedder: Embedder, batch: int = 64) -> int:
    """Embed every chunk that has no vector yet (new or rebuilt chunks)."""
    done = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id, text FROM chunks WHERE embedding IS NULL ORDER BY id LIMIT :n"),
                {"n": batch},
            ).fetchall()
            if not rows:
                return done
            vectors = embedder.embed([t for _, t in rows])
            for (cid, _), vec in zip(rows, vectors, strict=True):
                conn.execute(
                    text("UPDATE chunks SET embedding = CAST(:v AS vector) WHERE id = :id"),
                    {"v": "[" + ",".join(f"{x:.6f}" for x in vec) + "]", "id": cid},
                )
            done += len(rows)


def coverage(conn: Connection, hours: int = 48) -> dict[str, Any]:
    """PLAN Phase 4 check: of chunks that name a rostered player in full, how many carry his ID.

    "Rostered" = on any imported fantasy roster's latest week. Named = the player's full
    name appears in the chunk text (independent of how the linker tagged it).
    """
    rostered = conn.execute(
        text(
            """
            SELECT DISTINCT p.id, p.full_name FROM fantasy_rosters r
            JOIN players p ON p.id = ANY(r.player_ids)
            WHERE r.week = (SELECT max(week) FROM fantasy_rosters x WHERE x.league_id = r.league_id)
            """
        )
    ).fetchall()
    by_name: dict[str, set[str]] = {}
    for pid, name in rostered:
        by_name.setdefault(norm(name).strip(), set()).add(pid)
    named = tagged = 0
    misses: list[str] = []
    chunks = conn.execute(
        text(
            "SELECT c.text, c.player_ids FROM chunks c JOIN documents d ON d.id = c.document_id"
            " WHERE d.ingested_at > now() - make_interval(hours => :h)"
        ),
        {"h": hours},
    ).fetchall()
    for body, ids in chunks:
        t = norm(body)
        for name, pids in by_name.items():
            if f" {name} " in t:
                named += 1
                if pids & set(ids):
                    tagged += 1
                elif len(misses) < 10:
                    misses.append(f"{name}: {body[:90]}")
    docs = conn.execute(
        text(
            "SELECT source, count(*), count(DISTINCT url) FROM documents"
            " WHERE ingested_at > now() - make_interval(hours => :h) GROUP BY source ORDER BY 1"
        ),
        {"h": hours},
    ).fetchall()
    return {
        "hours": hours,
        "chunks": len(chunks),
        "named_rostered_mentions": named,
        "tagged": tagged,
        "coverage": round(tagged / named, 4) if named else None,
        "sample_misses": misses,
        "documents_by_source": [(s, n, d) for s, n, d in docs],
    }
