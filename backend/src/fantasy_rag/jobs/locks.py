"""One owner per scheduled job: Postgres session advisory locks with fixed IDs.

Each CLI command takes its lock once, at the top level (cli.main); nothing underneath
locks. Commands that read and write the same stats share STRUCTURED, so a Tuesday
ingest, league import and forward prediction never overlap. The lock lives on its own
connection for the whole job and is released explicitly, even on failure.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text

STRUCTURED = 81001  # ingest structured, league import, predict, calibration-seed
NEWS = 81002  # ingest news
TRANSACTIONS = 81003  # league sync-transactions
BACKUP = 81004  # backup


@contextmanager
def job_lock(engine: Engine, lock_id: int) -> Iterator[bool]:
    """Yields True if this process now owns the job, False if another holder has it."""
    conn = engine.connect()
    got = False
    try:
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:i)"), {"i": lock_id}).scalar())
        conn.commit()  # end the transaction; the session-level lock stays held
        yield got
    finally:
        try:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:i)"), {"i": lock_id})
                conn.commit()
            conn.close()
        except Exception:
            # Unlock failed (connection broken): discard the session, which frees its locks.
            conn.invalidate()
