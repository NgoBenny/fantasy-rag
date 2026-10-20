"""Phase 3: read-only role for agent tools, trigram index for player name search.

`fantasy_reader` is a cluster-wide NOLOGIN role: tool connections switch to it with
SET ROLE and run read-only transactions, so a tool bug can't write. Downgrade revokes
this database's grants but keeps the role, since other databases may use it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fantasy_reader')"
        " THEN CREATE ROLE fantasy_reader NOLOGIN; END IF; END $$"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO fantasy_reader")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO fantasy_reader")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fantasy_reader")
    op.execute("GRANT fantasy_reader TO CURRENT_USER")
    op.execute(
        "CREATE INDEX ix_players_search_name_trgm ON players USING gin (search_name gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_players_search_name_trgm")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM fantasy_reader"
    )
    op.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM fantasy_reader")
    op.execute("REVOKE USAGE ON SCHEMA public FROM fantasy_reader")
