"""Phase 7: league transactions, a fetch log, chat logging and its narrow role.

- `league_transactions`: Sleeper adds/drops/trades/chops, resolved to GSIS IDs. `raw` is
  the API record (it holds Sleeper user IDs), kept for local debugging only.
- `league_fetches`: when each league/week was last requested from Sleeper (taken just
  before the HTTP call) and whether it succeeded. A log of attempts, so it changes on
  every sync; the data tables stay idempotent.
- `query_log`: status, validated answer, check results and timings for each chat request.
- `fantasy_logger`: a cluster-wide NOLOGIN role that may only INSERT into query_log.
  Chat logging switches to it with SET ROLE; tools keep the read-only `fantasy_reader`.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INGESTED = """
    ingested_at timestamptz NOT NULL DEFAULT now(),
    sync_run_id bigint NOT NULL REFERENCES sync_runs(id)
"""

QUERY_LOG_COLUMNS = [
    "status text",
    "answer_json jsonb",
    "problems jsonb NOT NULL DEFAULT '[]'",
    "first_problems jsonb NOT NULL DEFAULT '[]'",
    "league_id bigint",
    "error text",
    "progress_first_ms integer",
    "answer_ms integer",
    "usage jsonb NOT NULL DEFAULT '{}'",
    "cost_usd numeric(10, 6)",
    "demo boolean NOT NULL DEFAULT false",
]


def upgrade() -> None:
    op.execute(
        f"""CREATE TABLE league_transactions (
            league_id bigint NOT NULL REFERENCES fantasy_leagues(id) ON DELETE CASCADE,
            transaction_id text NOT NULL,
            type text NOT NULL,
            status text NOT NULL,
            week integer NOT NULL,
            status_updated timestamptz NOT NULL,
            adds jsonb NOT NULL DEFAULT '{{}}',
            drops jsonb NOT NULL DEFAULT '{{}}',
            unresolved text[] NOT NULL DEFAULT '{{}}',
            raw jsonb NOT NULL,
            {INGESTED},
            PRIMARY KEY (league_id, transaction_id)
        )"""
    )
    op.execute(
        "CREATE INDEX ix_league_transactions_updated"
        " ON league_transactions (league_id, status_updated)"
    )
    op.execute(
        f"""CREATE TABLE league_fetches (
            league_id bigint NOT NULL REFERENCES fantasy_leagues(id) ON DELETE CASCADE,
            kind text NOT NULL CHECK (kind IN ('rosters', 'transactions')),
            week integer NOT NULL,
            fetched_at timestamptz NOT NULL,
            ok boolean NOT NULL,
            {INGESTED},
            PRIMARY KEY (league_id, kind, week)
        )"""
    )
    for col in QUERY_LOG_COLUMNS:
        op.execute(f"ALTER TABLE query_log ADD COLUMN {col}")
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fantasy_logger')"
        " THEN CREATE ROLE fantasy_logger NOLOGIN; END IF; END $$"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO fantasy_logger")
    op.execute("GRANT INSERT ON query_log TO fantasy_logger")
    op.execute("GRANT USAGE ON SEQUENCE query_log_id_seq TO fantasy_logger")
    op.execute("GRANT fantasy_logger TO CURRENT_USER")


def downgrade() -> None:
    op.execute("REVOKE USAGE ON SEQUENCE query_log_id_seq FROM fantasy_logger")
    op.execute("REVOKE INSERT ON query_log FROM fantasy_logger")
    op.execute("REVOKE USAGE ON SCHEMA public FROM fantasy_logger")
    for col in reversed(QUERY_LOG_COLUMNS):
        op.execute(f"ALTER TABLE query_log DROP COLUMN {col.split()[0]}")
    op.execute("DROP TABLE league_fetches")
    op.execute("DROP TABLE league_transactions")
