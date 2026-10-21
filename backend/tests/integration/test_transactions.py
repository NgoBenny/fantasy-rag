"""Sleeper transaction sync + reconciliation against a scratch database (fake Sleeper)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from fantasy_rag.analytics import queries as q
from fantasy_rag.ingest.transactions import sync_transactions

pytestmark = pytest.mark.db
MS = 1000


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * MS)


class FakeSleeper:
    def __init__(self, weeks: dict[int, Any]) -> None:
        self.weeks = weeks  # week -> records, or an Exception to raise

    def get(self, path: str) -> Any:
        if path == "/state/nfl":
            return {"season": "2026", "week": 2}
        week = int(path.rsplit("/", 1)[1])
        got = self.weeks.get(week, [])
        if isinstance(got, Exception):
            raise got
        return got


def _setup(url: str) -> None:
    eng = create_engine(url)
    with eng.begin() as c:
        run = c.execute(text("SELECT max(id) FROM sync_runs")).scalar_one()
        for sid, gsis in [("101", "00-1"), ("103", "00-3"), ("105", "00-5")]:
            c.execute(
                text(
                    "INSERT INTO player_external_ids (provider, external_id, player_id,"
                    " sync_run_id) VALUES ('sleeper', :s, :g, :r)"
                ),
                {"s": sid, "g": gsis, "r": run},
            )
        # The roster snapshot (week 2: roster 1 holds 00-1) was requested an hour ago.
        c.execute(
            text(
                "INSERT INTO league_fetches (league_id, kind, week, fetched_at, ok, sync_run_id)"
                " VALUES (1, 'rosters', 2, :t, true, :r)"
            ),
            {"t": datetime.now(UTC) - timedelta(hours=1), "r": run},
        )


def _tx(tid: str, minutes_ago: float, week: int, **kw: Any) -> dict[str, Any]:
    t = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {"transaction_id": tid, "type": kw.get("type", "free_agent"),
            "status": kw.get("status", "complete"), "leg": week, "status_updated": _ms(t),
            "adds": kw.get("adds"), "drops": kw.get("drops"), "creator": "user-123"}  # fmt: skip


def test_sync_is_idempotent_and_corrects_availability(db: str) -> None:
    _setup(db)
    eng = create_engine(db)
    weeks = {
        1: [],
        2: [
            _tx("t1", 30, 2, adds={"105": 1}),  # 00-5 picked up
            _tx("t2", 20, 2, drops={"101": 1}, adds=None, type="chopped"),  # 00-1 freed
            _tx("t3", 10, 2, adds={"103": 2}, status="failed", type="waiver"),  # no effect
            _tx("t4", 5, 2, adds={"999": 1}),  # unknown Sleeper ID: reported
        ],
    }
    first = sync_transactions(eng, FakeSleeper(weeks))
    assert first[0]["failed"] == [] and first[0]["weeks"] == [1, 2]
    with eng.connect() as c:
        avail, week, _ = q.rostered(c, 1)
        unresolved = c.execute(
            text("SELECT unresolved FROM league_transactions WHERE transaction_id = 't4'")
        ).scalar_one()
    assert week == 2 and avail.owner == {"00-5": 1}
    assert avail.as_of["status"] == "current" and avail.as_of["applied"] == 3
    assert unresolved == ["999"]
    # Re-importing identical data changes no transaction rows.
    with eng.begin() as c:
        before = c.execute(text("SELECT max(ingested_at) FROM league_transactions")).scalar_one()
    sync_transactions(eng, FakeSleeper(weeks))
    with eng.connect() as c:
        after = c.execute(text("SELECT max(ingested_at) FROM league_transactions")).scalar_one()
        rows = c.execute(
            text("SELECT row_count FROM sync_runs ORDER BY id DESC LIMIT 1")
        ).scalar_one()
    assert before == after and rows == 0


def test_a_failed_or_malformed_week_is_recorded_and_makes_availability_stale(db: str) -> None:
    _setup(db)
    eng = create_engine(db)
    bad = _tx("m", 5, 2, adds={"105": "1"})  # roster_id must be an int: rejected
    out = sync_transactions(eng, FakeSleeper({1: RuntimeError("HTTP 503"), 2: [bad]}))
    assert len(out[0]["failed"]) == 2
    with eng.connect() as c:
        ok = {
            w: k
            for w, k in c.execute(
                text("SELECT week, ok FROM league_fetches WHERE kind = 'transactions'")
            )
        }
        count = c.execute(text("SELECT count(*) FROM league_transactions")).scalar_one()
        avail, _, _ = q.rostered(c, 1)
    assert ok == {1: False, 2: False} and count == 0  # malformed week stored nothing
    assert avail.as_of["status"] == "stale" and avail.as_of["missing_weeks"] == [1, 2]
