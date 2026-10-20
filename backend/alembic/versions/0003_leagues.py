"""Phase 2: league name/settings and per-week matchup results on rosters.

`fantasy_rosters.raw` keeps Sleeper's matchup entry (players, starters, players_points
keyed by Sleeper ID) so scoring can be checked against Sleeper's own numbers; team
defenses and unresolved players live there because `player_ids` holds GSIS IDs only.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE fantasy_leagues ADD COLUMN name text, ADD COLUMN settings jsonb")
    op.execute(
        "ALTER TABLE fantasy_rosters ADD COLUMN matchup_id integer,"
        " ADD COLUMN points double precision, ADD COLUMN raw jsonb"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE fantasy_rosters DROP COLUMN matchup_id, DROP COLUMN points, DROP COLUMN raw"
    )
    op.execute("ALTER TABLE fantasy_leagues DROP COLUMN name, DROP COLUMN settings")
