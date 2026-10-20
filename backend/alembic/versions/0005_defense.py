"""Team defense and special-teams player stats derived from nflverse play-by-play.

Raw play-by-play is not stored: `ingest.pbp` aggregates it per game into the stats Sleeper
scores (team DEF keys, and st_ff / st_fum_rec per player). `team` uses Sleeper's team
DEF IDs ("LAR", not nflverse's "LA").

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INGESTED = """
    ingested_at timestamptz NOT NULL DEFAULT now(),
    sync_run_id bigint NOT NULL REFERENCES sync_runs(id)
"""


def upgrade() -> None:
    op.execute(
        f"""CREATE TABLE team_week_defense (
            season integer NOT NULL,
            week integer NOT NULL,
            season_type text NOT NULL,
            team text NOT NULL,
            game_id text NOT NULL,
            opponent text NOT NULL,
            stats jsonb NOT NULL,
            {INGESTED},
            PRIMARY KEY (season, week, season_type, team)
        )"""
    )
    op.execute(
        f"""CREATE TABLE player_week_special_teams (
            season integer NOT NULL,
            week integer NOT NULL,
            season_type text NOT NULL,
            player_id text NOT NULL REFERENCES players(id),
            game_id text NOT NULL,
            stats jsonb NOT NULL,
            {INGESTED},
            PRIMARY KEY (season, week, season_type, player_id)
        )"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE player_week_special_teams")
    op.execute("DROP TABLE team_week_defense")
