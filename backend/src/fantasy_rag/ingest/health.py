"""Source health from sync_runs: fresh, stale (last success too old) or failing.

`failing` means the last FAILING_AFTER finished attempts all failed, whatever the age,
so one transient failure shows only as age, and a dead source is called out even while
its last good data is still recent. The page header shows every non-fresh source.
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
}


def health(conn: Connection, now: datetime) -> dict[str, dict[str, Any]]:
    out = {}
    for kind, (source, limit) in SOURCES.items():
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
        out[kind] = {
            "state": state,
            "source": source,
            "last_ok": last_ok,
            "last_error_at": failed[0].finished_at if failed else None,
            "error": (failed[0].error or "")[:120] if failed else None,
        }
    return out
