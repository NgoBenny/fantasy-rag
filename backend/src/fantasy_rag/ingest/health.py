"""Source health from sync_runs: fresh, stale (last success too old) or failing.

`failing` means the last FAILING_AFTER finished attempts all failed, whatever the age,
so one transient failure shows only as age, and a dead source is called out even while
its last good data is still recent. The page header shows every non-fresh source.
A kind with several sync_runs sources (news: fetching and embedding) shows the worst of
them, so it is fresh only when every one of them is.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text

FAILING_AFTER = 2
# Header kind -> (sync_runs source, how old its last success may be)
SOURCES: dict[str, tuple[str, timedelta]] = {
    "stats": ("nflverse.player_stats", timedelta(hours=48)),
    "injuries": ("snapshot.sleeper_injuries", timedelta(hours=30)),
    "news": ("news", timedelta(hours=3)),
    "rosters": ("sleeper.league", timedelta(hours=30)),
    "transactions": ("sleeper.transactions", timedelta(hours=3)),
    "usage": ("nflverse.snap_counts", timedelta(hours=48)),
}
# Further sources a kind depends on, same age limit: stories without vectors can't be found.
ALSO = {"news": ["news.embed"], "usage": ["nflverse.pbp_usage"]}
RANK = {"fresh": 0, "stale": 1, "failing": 2}


def health(conn: Connection, now: datetime) -> dict[str, dict[str, Any]]:
    out = {}
    for kind, (source, limit) in SOURCES.items():
        states = [_state(conn, src, limit, now) for src in [source, *ALSO.get(kind, [])]]
        out[kind] = max(states, key=lambda st: RANK[st["state"]])  # first wins a tie
    return out


def _state(conn: Connection, source: str, limit: timedelta, now: datetime) -> dict[str, Any]:
    recent = conn.execute(
        text(
            "SELECT status, started_at, finished_at, error FROM sync_runs"
            " WHERE source = :s AND status <> 'running'"
            " ORDER BY started_at DESC LIMIT :n"
        ),
        {"s": source, "n": FAILING_AFTER},
    ).all()
    last_ok = conn.execute(
        text("SELECT max(finished_at) FROM sync_runs WHERE source = :s AND status = 'ok'"),
        {"s": source},
    ).scalar()
    failed = [r for r in recent if r.status != "ok"]
    if len(recent) == FAILING_AFTER and len(failed) == FAILING_AFTER:
        state = "failing"
    elif last_ok is None or now - last_ok > limit:
        state = "stale"
    else:
        state = "fresh"
    return {
        "state": state,
        "source": source,
        "last_ok": last_ok,
        "last_error_at": failed[0].finished_at if failed else None,
        "error": (failed[0].error or "")[:120] if failed else None,
    }
