"""News search: pgvector cosine + Postgres full-text, merged with reciprocal rank fusion.

Both searches use the same filters (players, recency). Results are one chunk per story,
and a story whose normalized headline repeats an earlier hit (the same news on two
sites) is dropped.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy import Connection, text

from fantasy_rag.index.embedder import Embedder

Mode = Literal["hybrid", "vector", "keyword"]
RERANK_POOL = 20  # fused hits a reranker reorders (PLAN: "top 20 hybrid hits")


class Reranker(Protocol):
    def scores(self, query: str, texts: list[str]) -> list[float]: ...


RRF_K = 60  # standard reciprocal-rank-fusion constant
POOL = 50  # candidates taken from each search before fusing


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    url: str
    title: str
    source: str
    published_at: datetime | None
    text: str
    player_ids: list[str]
    score: float


_FILTER = """
    (CAST(:players AS text[]) IS NULL OR c.player_ids && CAST(:players AS text[]))
    AND (CAST(:since AS timestamptz) IS NULL OR c.published_at >= :since)
"""
_SELECT = """
    SELECT c.id, d.id, d.url, d.title, d.source, c.published_at, c.text, c.player_ids
    FROM chunks c JOIN documents d ON d.id = c.document_id
"""


def _vector(conn: Connection, qvec: list[float], params: dict[str, object]) -> list[tuple]:  # type: ignore[type-arg]
    rows = conn.execute(
        text(
            f"{_SELECT} WHERE c.embedding IS NOT NULL AND {_FILTER}"
            " ORDER BY c.embedding <=> CAST(:q AS vector) LIMIT :n"
        ),
        {**params, "q": "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"},
    )
    return list(rows)


def _keyword(conn: Connection, query: str, params: dict[str, object]) -> list[tuple]:  # type: ignore[type-arg]
    # OR the query's terms (plainto_tsquery ANDs them, which drops most long questions);
    # ts_rank_cd still rewards chunks matching more terms, closer together.
    rows = conn.execute(
        text(
            f"""
            WITH q AS (SELECT CAST(replace(plainto_tsquery('english', :query)::text, '&', '|')
                               AS tsquery) AS tq)
            {_SELECT}, q WHERE q.tq::text <> '' AND c.tsv @@ q.tq AND {_FILTER}
            ORDER BY ts_rank_cd(c.tsv, q.tq) DESC, c.published_at DESC NULLS LAST LIMIT :n
            """
        ),
        {**params, "query": query},
    )
    return list(rows)


def _headline(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def search(
    conn: Connection,
    embedder: Embedder,
    query: str,
    player_ids: list[str] | None = None,
    since: datetime | None = None,
    limit: int = 5,
    mode: Mode = "hybrid",
    reranker: Reranker | None = None,
    fill_unfiltered: bool = False,
) -> list[Hit]:
    """Top `limit` stories for `query`.

    `player_ids` keeps only chunks tagged with those players. With `fill_unfiltered`, slots
    the tagged stories can't fill are filled from an unfiltered search, so a story whose
    player wasn't tagged (e.g. a practice-squad signing) can still be found.
    """
    hits = _search(conn, embedder, query, player_ids, since, limit, mode, reranker)
    if fill_unfiltered and player_ids and len(hits) < limit:
        have = {h.document_id for h in hits} | {_headline(h.title) for h in hits}
        for h in _search(conn, embedder, query, None, since, limit, mode, reranker):
            if h.document_id not in have and _headline(h.title) not in have:
                hits.append(h)
                have |= {h.document_id, _headline(h.title)}
            if len(hits) == limit:
                break
    return hits


def _search(
    conn: Connection,
    embedder: Embedder,
    query: str,
    player_ids: list[str] | None,
    since: datetime | None,
    limit: int,
    mode: Mode,
    reranker: Reranker | None,
) -> list[Hit]:
    params: dict[str, object] = {"players": player_ids or None, "since": since, "n": POOL}
    ranked: list[list[tuple]] = []  # type: ignore[type-arg]
    if mode in ("hybrid", "vector"):
        ranked.append(_vector(conn, embedder.embed_query(query), params))
    if mode in ("hybrid", "keyword"):
        ranked.append(_keyword(conn, query, params))

    scores: dict[int, float] = {}
    rows: dict[int, tuple] = {}  # type: ignore[type-arg]
    for result in ranked:
        for rank, row in enumerate(result, start=1):
            scores[row[0]] = scores.get(row[0], 0.0) + 1.0 / (RRF_K + rank)
            rows[row[0]] = row

    hits: list[Hit] = []
    seen_docs: set[int] = set()
    seen_headlines: set[str] = set()
    for cid in sorted(scores, key=lambda c: -scores[c]):
        r = rows[cid]
        if r[1] in seen_docs or _headline(r[3]) in seen_headlines:
            continue
        seen_docs.add(r[1])
        seen_headlines.add(_headline(r[3]))
        hits.append(Hit(r[0], r[1], r[2], r[3], r[4], r[5], r[6], list(r[7] or []), scores[cid]))
        if len(hits) == (RERANK_POOL if reranker else limit):
            break
    if reranker and hits:
        new = reranker.scores(query, [f"{h.title}. {h.text}" for h in hits])
        for h, s in zip(hits, new, strict=True):
            h.score = s
        hits.sort(key=lambda h: -h.score)
    return hits[:limit]
