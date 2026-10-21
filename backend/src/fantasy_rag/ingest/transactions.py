"""`fantasy-rag league sync-transactions`: Sleeper adds, drops, trades and chops per week.

For each imported league of the current season, fetches every week from the week
before the latest roster snapshot through the current week (so an outage that also
stalled roster imports is backfilled), records each attempt in `league_fetches` (time
taken just before the request, ok or not), and upserts the transactions with player IDs
resolved by ID only. A malformed record or a transaction from another leg fails that
week's fetch instead of being skipped silently. `raw` keeps the API record locally; it
holds Sleeper user IDs and is never exported.
"""

import json
from datetime import UTC, datetime
from typing import Any

import polars as pl
from sqlalchemy import Connection, Engine, text

from fantasy_rag.analytics.availability import Tx, parse_tx
from fantasy_rag.ingest.base import sync_run, upsert


def week_range(snapshot_week: int | None, current_week: int) -> range:
    first = max(1, (snapshot_week or current_week) - 1)
    return range(first, current_week + 1)


def parse_week(records: list[Any], week: int) -> list[Tx]:
    """Every record of one week's response, or ValueError for the whole week."""
    out = []
    for raw in records:
        if not isinstance(raw, dict):
            raise ValueError("malformed transaction: not an object")
        t = parse_tx(raw)
        if t.week != week:
            raise ValueError(f"transaction {t.transaction_id} is leg {t.week}, requested {week}")
        out.append(t)
    return out


def tx_rows(
    league_pk: int, txs: list[Tx], raws: list[dict[str, Any]], to_gsis: dict[str, str]
) -> pl.DataFrame:
    rows = []
    for t, raw in zip(txs, raws, strict=True):
        moved = list(t.adds) + list(t.drops)
        rows.append(
            {
                "league_id": league_pk,
                "transaction_id": t.transaction_id,
                "type": t.type,
                "status": t.status,
                "week": t.week,
                "status_updated": t.status_updated.isoformat(),
                "adds": json.dumps({to_gsis[p]: r for p, r in t.adds.items() if p in to_gsis}),
                "drops": json.dumps({to_gsis[p]: r for p, r in t.drops.items() if p in to_gsis}),
                "unresolved": "{" + ",".join(sorted({p for p in moved if p not in to_gsis})) + "}",
                "raw": json.dumps(raw, sort_keys=True),
            }
        )
    return pl.DataFrame(rows)


def fetch_row(league_pk: int, kind: str, week: int, at: datetime, ok: bool) -> dict[str, Any]:
    return {"league_id": league_pk, "kind": kind, "week": week, "fetched_at": at.isoformat(),
            "ok": ok}  # fmt: skip


def record_fetches(conn: Connection, rows: list[dict[str, Any]], run_id: int) -> None:
    """The fetch log changes on every sync by design; data tables stay idempotent."""
    upsert(conn, "league_fetches", pl.DataFrame(rows), ["league_id", "kind", "week"], run_id)


def sync_transactions(engine: Engine, api: Any) -> list[dict[str, Any]]:
    state = api.get("/state/nfl")
    season, current = int(state["season"]), int(state["week"])
    with engine.connect() as c:
        leagues = c.execute(
            text(
                "SELECT l.id, l.external_league_id, (SELECT max(week) FROM fantasy_rosters r"
                " WHERE r.league_id = l.id) FROM fantasy_leagues l"
                " WHERE l.provider = 'sleeper' AND l.season = :s ORDER BY l.id"
            ),
            {"s": season},
        ).fetchall()
    summaries = []
    with sync_run(engine, "sleeper.transactions") as run:
        ids = run.conn.execute(
            text(
                "SELECT external_id, player_id FROM player_external_ids WHERE provider = 'sleeper'"
            )
        )
        to_gsis: dict[str, str] = {str(e): str(p) for e, p in ids}
        changed = 0
        for league_pk, external_id, snapshot_week in leagues:
            fetches, failed = [], []
            for week in week_range(snapshot_week, current):
                at = datetime.now(UTC)  # before the request: the coverage boundary
                try:
                    raws = api.get(f"/league/{external_id}/transactions/{week}") or []
                    txs = parse_week(raws, week)
                except Exception as e:  # HTTP error or malformed week: recorded, not skipped
                    fetches.append(fetch_row(league_pk, "transactions", week, at, False))
                    failed.append(f"week {week}: {type(e).__name__}: {str(e)[:200]}")
                    continue
                changed += upsert(
                    run.conn,
                    "league_transactions",
                    tx_rows(league_pk, txs, raws, to_gsis),
                    ["league_id", "transaction_id"],
                    run.id,
                )
                fetches.append(fetch_row(league_pk, "transactions", week, at, True))
            record_fetches(run.conn, fetches, run.id)
            summaries.append({"league_pk": league_pk, "weeks": [f["week"] for f in fetches],
                              "failed": failed})  # fmt: skip
        run.rows = changed
    return summaries
