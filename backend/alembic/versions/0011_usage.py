"""Phase 10 usage: per player-game targets/carries and red-zone looks from play-by-play.

`player_week_pbp_usage` holds `ingest.pbp.usage` counts (targets_pbp, carries_pbp,
rz_targets, rz_carries, gl_carries, rz_unknown). `pbp_games` lists every game whose
play-by-play was loaded, so a player with no usage row in a covered game is a known zero,
and a game missing here is unknown (never 0). The reader role gets SELECT through the
default privileges from 0004.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INGESTED = """
    ingested_at timestamptz NOT NULL DEFAULT now(),
    sync_run_id bigint NOT NULL REFERENCES sync_runs(id)
"""


def upgrade() -> None:
    op.execute(
        f"""CREATE TABLE player_week_pbp_usage (
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
    op.execute(
        f"""CREATE TABLE pbp_games (
            season integer NOT NULL,
            week integer NOT NULL,
            season_type text NOT NULL,
            game_id text NOT NULL,
            plays integer NOT NULL,
            {INGESTED},
            PRIMARY KEY (season, week, season_type, game_id)
        )"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE pbp_games")
    op.execute("DROP TABLE player_week_pbp_usage")
