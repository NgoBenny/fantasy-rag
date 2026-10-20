"""Phase 1: nflverse-mirroring tables, snapshot keys, unresolved-player report.

Shapes follow docs/data_sources.md: raw stat columns live in a `stats` jsonb keyed by
nflverse column names (fantasy points are never stored); depth charts are keyed by
`week` up to 2024 and by snapshot time `as_of` from 2025 on.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INGESTED = """
    ingested_at timestamptz NOT NULL DEFAULT now(),
    sync_run_id bigint NOT NULL REFERENCES sync_runs(id)
"""

UPGRADE = [
    f"""CREATE TABLE schedules (
        game_id text PRIMARY KEY,
        season integer NOT NULL,
        week integer NOT NULL,
        game_type text NOT NULL,
        home_team text NOT NULL,
        away_team text NOT NULL,
        kickoff_at timestamptz,
        home_score integer,
        away_score integer,
        result integer,
        spread_line double precision,
        total_line double precision,
        roof text,
        surface text,
        stadium text,
        {INGESTED}
    )""",
    "CREATE INDEX ix_schedules_season_week ON schedules (season, week)",
    f"""CREATE TABLE player_week_stats (
        season integer NOT NULL,
        week integer NOT NULL,
        season_type text NOT NULL,
        player_id text NOT NULL REFERENCES players(id),
        game_id text,
        team text,
        opponent text,
        stats jsonb NOT NULL,
        {INGESTED},
        PRIMARY KEY (season, week, season_type, player_id)
    )""",
    "CREATE INDEX ix_player_week_stats_player ON player_week_stats (player_id, season, week)",
    f"""CREATE TABLE snap_counts (
        season integer NOT NULL,
        week integer NOT NULL,
        game_type text NOT NULL,
        player_id text NOT NULL REFERENCES players(id),
        game_id text,
        team text,
        offense_snaps integer,
        offense_pct double precision,
        st_snaps integer,
        st_pct double precision,
        {INGESTED},
        PRIMARY KEY (season, week, game_type, player_id)
    )""",
    f"""CREATE TABLE ff_opportunity (
        season integer NOT NULL,
        week integer NOT NULL,
        player_id text NOT NULL REFERENCES players(id),
        game_id text,
        team text,
        stats jsonb NOT NULL,
        {INGESTED},
        PRIMARY KEY (season, week, player_id)
    )""",
    f"""CREATE TABLE depth_charts (
        id bigserial PRIMARY KEY,
        season integer NOT NULL,
        week integer,
        as_of timestamptz,
        team text NOT NULL,
        position text NOT NULL,
        depth integer NOT NULL,
        player_id text NOT NULL REFERENCES players(id),
        {INGESTED},
        CHECK (week IS NOT NULL OR as_of IS NOT NULL),
        UNIQUE NULLS NOT DISTINCT (season, week, as_of, team, position, player_id)
    )""",
    "CREATE INDEX ix_depth_charts_player ON depth_charts (player_id, season)",
    f"""CREATE TABLE injury_history (
        season integer NOT NULL,
        game_type text NOT NULL,
        week integer NOT NULL,
        team text NOT NULL,
        player_id text NOT NULL REFERENCES players(id),
        report_status text,
        report_primary_injury text,
        report_secondary_injury text,
        practice_status text,
        practice_primary_injury text,
        date_modified timestamptz,
        {INGESTED},
        PRIMARY KEY (season, game_type, week, team, player_id)
    )""",
    # Snapshot rows say which weekly report they came from (nflverse) when known.
    "ALTER TABLE injury_snapshots ADD COLUMN season integer, ADD COLUMN week integer",
    # Trending is stored once per 6-hour window, so a re-run inside a window updates in place.
    "ALTER TABLE trending ADD COLUMN window_start timestamptz NOT NULL",
    "ALTER TABLE trending ADD CONSTRAINT uq_trending_window"
    " UNIQUE (window_start, kind, lookback_hours, player_id)",
    f"""CREATE TABLE unresolved_players (
        source text NOT NULL,
        provider text NOT NULL,
        external_id text NOT NULL,
        name text,
        team text,
        position text,
        candidates text,  -- hints for a human to confirm in data/id_overrides.csv; never applied
        {INGESTED},
        PRIMARY KEY (source, provider, external_id)
    )""",
]

DOWNGRADE = [
    "DROP TABLE unresolved_players",
    "ALTER TABLE trending DROP CONSTRAINT uq_trending_window",
    "ALTER TABLE trending DROP COLUMN window_start",
    "ALTER TABLE injury_snapshots DROP COLUMN season, DROP COLUMN week",
    "DROP TABLE injury_history",
    "DROP TABLE depth_charts",
    "DROP TABLE ff_opportunity",
    "DROP TABLE snap_counts",
    "DROP TABLE player_week_stats",
    "DROP TABLE schedules",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
