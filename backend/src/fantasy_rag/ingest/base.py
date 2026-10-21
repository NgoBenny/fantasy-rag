"""Shared ingestion adapter: validate, upsert, record sync_runs and unresolved players."""

import io
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import polars as pl
from sqlalchemy import Connection, Engine, text

STAMP_COLUMNS = ("ingested_at", "sync_run_id")
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
UNRESOLVED_COLUMNS = ["provider", "external_id", "name", "team", "position", "candidates"]


class SourceSchemaError(RuntimeError):
    """A source no longer has the columns we rely on: stop and look, don't guess."""


def require_columns(df: pl.DataFrame, columns: Sequence[str], source: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise SourceSchemaError(f"{source}: missing columns {missing}; got {df.columns}")


def search_name(col: str) -> pl.Expr:
    """Lowercase ASCII name without punctuation or suffixes, for fuzzy search (never for joins)."""
    return (
        pl.col(col)
        .str.normalize("NFKD")
        .str.replace_all(r"[^\x00-\x7f]", "")
        .str.to_lowercase()
        .str.replace_all(r"[^a-z0-9 ]", "")
        .str.replace_all(r"\b(jr|sr|ii|iii|iv|v)\b", "")
        .str.replace_all(r"\s+", " ")
        .str.strip_chars()
    )


@dataclass
class Run:
    id: int
    conn: Connection
    rows: int = 0


class SourceUnavailable(RuntimeError):
    pass


def _maybe_fail(source: str) -> None:
    """Outage drills only: FANTASY_RAG_FAIL_SOURCE=news (comma-separated sync_runs source
    names) makes that source fail exactly as a real outage would, recorded as failed."""
    injected = {s.strip() for s in os.environ.get("FANTASY_RAG_FAIL_SOURCE", "").split(",")}
    if source in injected:
        raise SourceUnavailable(f"injected outage for {source} (FANTASY_RAG_FAIL_SOURCE)")


@contextmanager
def sync_run(engine: Engine, source: str) -> Iterator[Run]:
    """One transaction per source; the sync_runs row survives a failure to record it."""
    with engine.begin() as c:
        run_id = c.execute(
            text("INSERT INTO sync_runs (source, status) VALUES (:s, 'running') RETURNING id"),
            {"s": source},
        ).scalar_one()
    try:
        _maybe_fail(source)
        with engine.begin() as conn:
            run = Run(run_id, conn)
            yield run
    except Exception as exc:
        with engine.begin() as c:
            c.execute(
                text(
                    "UPDATE sync_runs SET status = 'failed', finished_at = now(), error = :e"
                    " WHERE id = :id"
                ),
                {"e": repr(exc)[:4000], "id": run_id},
            )
        raise
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE sync_runs SET status = 'ok', finished_at = now(), row_count = :n"
                " WHERE id = :id"
            ),
            {"n": run.rows, "id": run_id},
        )


def stage(conn: Connection, table: str, df: pl.DataFrame) -> None:
    """COPY `df` into a temp table `stage` shaped like `table`'s matching columns."""
    bad = [n for n in [table, *df.columns] if not IDENTIFIER.match(n)]
    if bad:  # identifiers are interpolated into SQL, so only plain lowercase names pass
        raise ValueError(f"unsafe SQL identifiers: {bad}")
    col_list = ", ".join(df.columns)
    raw = conn.connection.driver_connection
    assert raw is not None
    with raw.cursor() as cur:
        cur.execute(f"CREATE TEMP TABLE stage AS SELECT {col_list} FROM {table} WITH NO DATA")
        buf = io.BytesIO()
        df.write_csv(buf, null_value=r"\N")
        copy_sql = f"COPY stage ({col_list}) FROM STDIN (FORMAT csv, HEADER true, NULL '\\N')"
        with cur.copy(copy_sql) as cp:
            cp.write(buf.getvalue())


def upsert(
    conn: Connection,
    table: str,
    df: pl.DataFrame,
    keys: Sequence[str],
    run_id: int | None,
) -> int:
    """Insert new rows and update changed ones; unchanged rows are left untouched.

    Returns rows inserted + updated, so a re-run over identical data returns 0.
    `run_id=None` is for tables without ingested_at/sync_run_id (the sources registry).
    """
    if df.is_empty():
        return 0
    df = df.unique(subset=list(keys), keep="last", maintain_order=True)
    cols = df.columns
    non_keys = [c for c in cols if c not in keys]
    col_list = ", ".join(cols)

    stage(conn, table, df)
    raw = conn.connection.driver_connection
    assert raw is not None
    with raw.cursor() as cur:
        insert_cols, select_cols = col_list, col_list
        updates = [f"{c} = EXCLUDED.{c}" for c in non_keys]
        if run_id is not None:
            insert_cols += ", sync_run_id"
            select_cols += f", {int(run_id)}"
            updates += ["ingested_at = now()", "sync_run_id = EXCLUDED.sync_run_id"]
        if non_keys:
            changed = ", ".join(f"{table}.{c}" for c in non_keys)
            excluded = ", ".join(f"EXCLUDED.{c}" for c in non_keys)
            # ROW() keeps the comparison valid when there is a single non-key column.
            on_conflict = (
                f"DO UPDATE SET {', '.join(updates)} "
                f"WHERE ROW({changed}) IS DISTINCT FROM ROW({excluded})"
            )
        else:
            on_conflict = "DO NOTHING"
        cur.execute(
            f"INSERT INTO {table} ({insert_cols}) SELECT {select_cols} FROM stage "
            f"ON CONFLICT ({', '.join(keys)}) {on_conflict}"
        )
        changed_rows = int(cur.rowcount)
        cur.execute("DROP TABLE stage")
    return changed_rows


def prune(
    conn: Connection,
    table: str,
    df: pl.DataFrame,
    keys: Sequence[str],
    scope: str,
    values: Sequence[Any],
) -> int:
    """Delete rows whose `scope` column is in `values` but whose key is not in `df`.

    For derived tables where `df` is the complete fresh result for those scopes (e.g. every
    season of play-by-play that loaded), so a corrected source can't leave stale rows.
    Pass only scopes that loaded in full. Returns rows deleted.
    """
    if scope not in keys:
        raise ValueError(f"scope {scope!r} must be a key column")
    if not values:
        return 0
    stage(conn, table, df.select(keys).unique())
    match = " AND ".join(f"s.{k} = t.{k}" for k in keys)
    raw = conn.connection.driver_connection
    assert raw is not None
    with raw.cursor() as cur:
        cur.execute(
            f"DELETE FROM {table} t WHERE t.{scope} = ANY(%s)"
            f" AND NOT EXISTS (SELECT 1 FROM stage s WHERE {match})",
            (list(values),),
        )
        deleted = int(cur.rowcount)
        cur.execute("DROP TABLE stage")
    return deleted


def record_unresolved(run: Run, source: str, df: pl.DataFrame) -> int:
    """Replace this source's unresolved list; returns rows inserted or changed."""
    df = df.select(
        [pl.lit(source).alias("source")]
        + [
            pl.col(c).cast(pl.String) if c in df.columns else pl.lit(None, pl.String).alias(c)
            for c in UNRESOLVED_COLUMNS
        ]
    )
    keep = [f"{p}:{e}" for p, e in df.select("provider", "external_id").iter_rows()]
    run.conn.execute(
        text(
            "DELETE FROM unresolved_players WHERE source = :s"
            " AND NOT (provider || ':' || external_id = ANY(:keep))"
        ),
        {"s": source, "keep": keep},
    )
    return upsert(run.conn, "unresolved_players", df, ["source", "provider", "external_id"], run.id)
