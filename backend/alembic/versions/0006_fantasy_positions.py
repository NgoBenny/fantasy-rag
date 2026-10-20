"""Sleeper's fantasy slot eligibility per player (e.g. a fullback counts as RB).

Filled by the daily Sleeper step; nflverse ingestion never touches this column.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE players ADD COLUMN fantasy_positions text[]")


def downgrade() -> None:
    op.execute("ALTER TABLE players DROP COLUMN fantasy_positions")
