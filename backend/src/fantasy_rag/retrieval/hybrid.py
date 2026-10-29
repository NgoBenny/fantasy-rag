"""News search: pgvector cosine + Postgres full-text, merged with reciprocal rank fusion.

Both searches use the same filters (players, recency). Results are one chunk per story,
and a story whose normalized headline repeats an earlier hit (the same news on two
sites) is dropped.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import Connection, text

from fantasy_rag.index.embedder import Embedder

Mode = Literal["hybrid", "vector", "keyword"]
RERANK_POOL = 20  # fused hits a reranker reorders (PLAN: "top 20 hybrid hits")


class Reranker(Protocol):
    def scores(self, query: str, texts: list[str]) -> list[float]: ...


RRF_K = 60  # standard reciprocal-rank-fusion constant
POOL = 50  # candidates taken from each search before fusing
PREFER_WEIGHT = 0.5  # a preferred player's stories rank higher, without hiding others
RESERVE = 2  # the best overall matches always stay in the results, tagged or not


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
    similarity: float | None = None  # cosine similarity to the query, if a vector candidate
    keyword_match: bool = False  # matched the full-text query


_FILTER = """
    (CAST(:players AS text[]) IS NULL OR c.player_ids && CAST(:players AS text[]))
    AND (CAST(:since AS timestamptz) IS NULL OR c.published_at >= :since)
"""
_COLUMNS = "SELECT c.id, d.id, d.url, d.title, d.source, c.published_at, c.text, c.player_ids"
_LIVE = " FROM chunks c JOIN documents d ON d.id = c.document_id"
# A frozen evaluation corpus (snapshot.py): same columns, never updated.
_SNAPSHOT = (
    " FROM news_eval_chunks c JOIN news_eval_documents d"
    " ON d.snapshot = c.snapshot AND d.id = c.document_id AND c.snapshot = :snapshot"
)


def _select(params: dict[str, object]) -> str:
    return _COLUMNS + (_LIVE if params["snapshot"] is None else _SNAPSHOT)


Row = tuple[Any, ...]


def _vector(conn: Connection, qvec: list[float], params: dict[str, object]) -> list[Row]:
    rows = conn.execute(
        text(
            f"""
            SELECT * FROM ({
                _select(params).replace(
                    "c.player_ids", "c.player_ids, 1 - (c.embedding <=> CAST(:q AS vector)) AS sim"
                )
            }
                           WHERE c.embedding IS NOT NULL AND {_FILTER}) x
            ORDER BY sim DESC LIMIT :n
            """
        ),
        {**params, "q": "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"},
    )
    return list(rows)


def _keyword(conn: Connection, query: str, params: dict[str, object]) -> list[Row]:
    # OR the query's terms (plainto_tsquery ANDs them, which drops most long questions);
    # ts_rank_cd still rewards chunks matching more terms, closer together.
    rows = conn.execute(
        text(
            f"""
            WITH q AS (SELECT CAST(replace(plainto_tsquery('english', :query)::text, '&', '|')
                               AS tsquery) AS tq)
            {_select(params)}, q WHERE q.tq::text <> '' AND c.tsv @@ q.tq AND {_FILTER}
            ORDER BY ts_rank_cd(c.tsv, q.tq) DESC, c.published_at DESC NULLS LAST LIMIT :n
            """
        ),
        {**params, "query": query},
    )
    return list(rows)


def _headline(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _lists(
    conn: Connection, embedder: Embedder, query: str, params: dict[str, object], mode: Mode
) -> tuple[list[Row], list[Row]]:
    vec = _vector(conn, embedder.embed_query(query), params) if mode != "keyword" else []
    kw = _keyword(conn, query, params) if mode != "vector" else []
    return vec, kw


def search(
    conn: Connection,
    embedder: Embedder,
    query: str,
    player_ids: list[str] | None = None,
    since: datetime | None = None,
    limit: int = 5,
    mode: Mode = "hybrid",
    reranker: Reranker | None = None,
    prefer_players: list[str] | None = None,
    snapshot: str | None = None,
) -> list[Hit]:
    """Top `limit` stories for `query`.

    `player_ids` is a hard filter. `prefer_players` only boosts stories tagged with those
    players: every story is still a candidate, and the RESERVE best overall matches always
    stay in the results, so an untagged story about the player is never hidden by tagged
    ones. `snapshot` searches that frozen evaluation corpus instead of the live stories.
    """
    params: dict[str, object] = {
        "players": player_ids or None,
        "since": since,
        "snapshot": snapshot,
        "n": POOL,
    }
    vec, kw = _lists(conn, embedder, query, params, mode)
    base: dict[int, float] = {}
    rows: dict[int, Row] = {}
    for result in (vec, kw):
        for rank, row in enumerate(result, start=1):
            base[row[0]] = base.get(row[0], 0.0) + 1.0 / (RRF_K + rank)
            rows.setdefault(row[0], row)
    sims = {r[0]: float(r[8]) for r in vec}
    keyword_ids = {r[0] for r in kw}

    score = dict(base)
    if prefer_players:
        pv, pk = _lists(conn, embedder, query, {**params, "players": prefer_players}, mode)
        for result, is_vector in ((pv, True), (pk, False)):
            for rank, row in enumerate(result, start=1):
                score[row[0]] = score.get(row[0], 0.0) + PREFER_WEIGHT / (RRF_K + rank)
                rows.setdefault(row[0], row)
                if is_vector:
                    sims.setdefault(row[0], float(row[8]))
                else:
                    keyword_ids.add(row[0])

    docs: set[int] = set()
    heads: set[str] = set()

    def take(order: list[int], n: int, out: list[int]) -> None:
        """Append up to n new stories from `order`, skipping repeated stories/headlines."""
        for cid in order:
            if n == 0:
                return
            r = rows[cid]
            if r[1] in docs or _headline(r[3]) in heads:
                continue
            docs.add(r[1])
            heads.add(_headline(r[3]))
            out.append(cid)
            n -= 1

    want = RERANK_POOL if reranker else limit
    chosen: list[int] = []
    # The best overall matches go in first, so the player preference can't push them out;
    # the remaining slots are filled from the preference-boosted ranking.
    take(sorted(base, key=lambda c: -base[c]), min(RESERVE, want), chosen)
    take(sorted(score, key=lambda c: -score[c]), want - len(chosen), chosen)

    hits = []
    for c in chosen:
        r = rows[c]
        hits.append(
            Hit(r[0], r[1], r[2], r[3], r[4], r[5], r[6], list(r[7] or []), score.get(c, 0.0),
                sims.get(c), c in keyword_ids)
        )  # fmt: skip
    hits.sort(key=lambda h: -h.score)
    if reranker and hits:
        for h, s in zip(
            hits, reranker.scores(query, [f"{h.title}. {h.text}" for h in hits]), strict=True
        ):
            h.score = s
        hits.sort(key=lambda h: -h.score)
    return hits[:limit]
