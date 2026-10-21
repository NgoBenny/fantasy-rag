"""One owner per scheduled job: Postgres session advisory locks with fixed IDs.

Each CLI command takes its lock once, at the top level (cli.main); nothing underneath
locks. Commands that read and write the same stats share STRUCTURED, so a Tuesday
ingest, league import and forward prediction never overlap. The lock lives on its own
connection for the whole job and is released explicitly, even on failure.

Most jobs skip when their lock is taken (the next scheduled run catches up). `predict`
waits up to PREDICT_WAIT instead: a catch-up ingest after boot must not cost the week's
one on-time prediction, and a wait that runs out fails loudly (cli exits 1).
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

STRUCTURED = 81001  # ingest structured, league import, predict, calibration-seed
NEWS = 81002  # ingest news
TRANSACTIONS = 81003  # league sync-transactions
BACKUP = 81004  # backup
PREDICT_WAIT = 15 * 60  # seconds; the forward task starts 1.5 h before the deadline


@contextmanager
def job_lock(engine: Engine, lock_id: int, wait: float = 0) -> Iterator[bool]:
    """Yields True if this process now owns the job, False if another holder has it (still,
    after waiting up to `wait` seconds)."""
    conn = engine.connect()
    got = False
    try:
        if wait:
            try:  # SET LOCAL: the timeout ends with this transaction, never reaches the pool
                conn.execute(text(f"SET LOCAL lock_timeout = '{int(wait * 1000)}ms'"))
                conn.execute(text("SELECT pg_advisory_lock(:i)"), {"i": lock_id})
                got = True
            except OperationalError as e:
                if getattr(e.orig, "sqlstate", None) != "55P03":  # lock_not_available
                    raise
                conn.rollback()
        else:
            got = bool(
                conn.execute(text("SELECT pg_try_advisory_lock(:i)"), {"i": lock_id}).scalar()
            )
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
