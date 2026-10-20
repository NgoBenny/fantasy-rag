"""Phase 0: extensions plus the 13 tables whose shape we own.

The six nflverse-mirroring tables (schedules, player_week_stats, snap_counts,
ff_opportunity, depth_charts, injury_history) arrive in Phase 1, shaped by
docs/data_sources.md. Nothing here has a foreign key to them.

Revision ID: 0001
Revises:
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every ingested row records when it arrived and which sync run wrote it.
INGESTED = """
    ingested_at timestamptz NOT NULL DEFAULT now(),
    sync_run_id bigint NOT NULL REFERENCES sync_runs(id)
"""

TABLES = [
    """CREATE TABLE sync_runs (
        id bigserial PRIMARY KEY,
        source text NOT NULL,
        started_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz,
        status text NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
        row_count integer,
        error text
    )""",
    """CREATE TABLE sources (
        name text PRIMARY KEY,
        license text NOT NULL,
        attribution text NOT NULL,
        storage_allowed text NOT NULL CHECK (storage_allowed IN ('full', 'summary', 'link')),
        refresh_cadence text NOT NULL
    )""",
    f"""CREATE TABLE players (
        id text PRIMARY KEY,  -- nflverse GSIS ID
        full_name text NOT NULL,
        search_name text NOT NULL,
        position text,
        team text,
        status text,
        {INGESTED}
    )""",
    f"""CREATE TABLE player_external_ids (
        provider text NOT NULL,
        external_id text NOT NULL,
        player_id text NOT NULL REFERENCES players(id),
        {INGESTED},
        PRIMARY KEY (provider, external_id)
    )""",
    "CREATE INDEX ix_player_external_ids_player ON player_external_ids (player_id)",
    f"""CREATE TABLE injury_snapshots (
        id bigserial PRIMARY KEY,
        player_id text NOT NULL REFERENCES players(id),
        status text,
        body_part text,
        practice text,
        source text NOT NULL,
        reported_at timestamptz,
        {INGESTED}
    )""",
    "CREATE INDEX ix_injury_snapshots_player_time"
    " ON injury_snapshots (player_id, ingested_at DESC)",
    f"""CREATE TABLE trending (
        id bigserial PRIMARY KEY,
        player_id text NOT NULL REFERENCES players(id),
        kind text NOT NULL CHECK (kind IN ('add', 'drop')),
        count integer NOT NULL,
        lookback_hours integer NOT NULL,
        {INGESTED}
    )""",
    f"""CREATE TABLE fantasy_leagues (
        id bigserial PRIMARY KEY,
        provider text NOT NULL,
        external_league_id text NOT NULL,
        season integer NOT NULL,
        scoring_settings jsonb NOT NULL,
        scoring_rules jsonb,
        roster_positions jsonb NOT NULL,
        {INGESTED},
        UNIQUE (provider, external_league_id)
    )""",
    f"""CREATE TABLE fantasy_rosters (
        league_id bigint NOT NULL REFERENCES fantasy_leagues(id) ON DELETE CASCADE,
        roster_id integer NOT NULL,
        week integer NOT NULL,
        owner_name text,
        player_ids text[] NOT NULL DEFAULT '{{}}',
        starter_ids text[] NOT NULL DEFAULT '{{}}',
        {INGESTED},
        PRIMARY KEY (league_id, roster_id, week)
    )""",
    """CREATE TABLE projections (
        player_id text NOT NULL REFERENCES players(id),
        season integer NOT NULL,
        week integer NOT NULL,
        model_version text NOT NULL,
        predictions jsonb NOT NULL,
        p10 double precision,
        p50 double precision,
        p90 double precision,
        generated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (player_id, season, week, model_version)
    )""",
    """CREATE TABLE model_runs (
        model_version text PRIMARY KEY,
        training_window jsonb NOT NULL,
        features text[] NOT NULL,
        metrics jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    f"""CREATE TABLE documents (
        id bigserial PRIMARY KEY,
        source text NOT NULL REFERENCES sources(name),
        url text NOT NULL UNIQUE,
        title text NOT NULL,
        summary text,
        published_at timestamptz,
        teams text[] NOT NULL DEFAULT '{{}}',
        player_ids text[] NOT NULL DEFAULT '{{}}',
        {INGESTED}
    )""",
    """CREATE TABLE chunks (
        id bigserial PRIMARY KEY,
        document_id bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        text text NOT NULL,
        embedding vector(384),
        tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
        player_ids text[] NOT NULL DEFAULT '{}',
        published_at timestamptz
    )""",
    "CREATE INDEX ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)",
    "CREATE INDEX ix_chunks_player_ids ON chunks USING gin (player_ids)",
    """CREATE TABLE query_log (
        id bigserial PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT now(),
        question text NOT NULL,
        tools_called jsonb NOT NULL DEFAULT '[]',
        tool_inputs jsonb NOT NULL DEFAULT '[]',
        chunk_ids bigint[] NOT NULL DEFAULT '{}',
        answer text,
        latency_ms integer,
        feedback text
    )""",
]

DROP_ORDER = [
    "query_log",
    "chunks",
    "documents",
    "model_runs",
    "projections",
    "fantasy_rosters",
    "fantasy_leagues",
    "trending",
    "injury_snapshots",
    "player_external_ids",
    "players",
    "sources",
    "sync_runs",
]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for statement in TABLES:
        op.execute(statement)


def downgrade() -> None:
    for table in DROP_ORDER:
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
