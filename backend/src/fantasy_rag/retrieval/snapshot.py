"""Frozen news-evaluation corpus (migration 0012): capture once, then search it.

A snapshot holds the stories stored up to `through_document` (document IDs follow
insertion order; story dates don't, because feeds revise them) as they read at capture
time. Content a feed overwrote before the capture can't be recovered, so a snapshot is
the corpus as captured, not as labelled.
"""

from typing import Any

from sqlalchemy import Connection, text


class SnapshotMissing(RuntimeError):
    pass


def capture(conn: Connection, name: str, through_document: int) -> dict[str, Any]:
    """Copy stories with ID <= through_document into snapshot `name`. Refused if it exists."""
    if exists(conn, name):
        raise ValueError(f"news snapshot {name!r} already exists; snapshots are never rebuilt")
    params = {"s": name, "t": through_document}
    docs = conn.execute(
        text(
            "INSERT INTO news_eval_documents"
            " (snapshot, id, source, url, title, summary, published_at, teams, player_ids)"
            " SELECT :s, id, source, url, title, summary, published_at, teams, player_ids"
            " FROM documents WHERE id <= :t"
        ),
        params,
    ).rowcount
    chunks = conn.execute(
        text(
            "INSERT INTO news_eval_chunks"
            " (snapshot, id, document_id, text, embedding, player_ids, published_at)"
            " SELECT :s, c.id, c.document_id, c.text, c.embedding, c.player_ids, c.published_at"
            " FROM chunks c WHERE c.document_id <= :t"
        ),
        params,
    ).rowcount
    return {"snapshot": name, "through_document": through_document, "documents": docs,
            "chunks": chunks}  # fmt: skip


def exists(conn: Connection, name: str) -> bool:
    return bool(
        conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM news_eval_documents WHERE snapshot = :s)"),
            {"s": name},
        ).scalar()
    )


def require(conn: Connection, name: str) -> None:
    if not exists(conn, name):
        raise SnapshotMissing(
            f"news snapshot {name!r} is missing: run `fantasy-rag eval news-snapshot` once"
        )
