"""Phase 8: immutable forward log of Tuesday predictions, and model artifacts.

forward_runs: one row per `fantasy-rag predict` run (snapshot time, status, models,
scoring rules, range offsets per policy/position). forward_inputs: the feature values
each prediction used. projections (replaces the empty Phase 0 table): one row per run,
policy, scoring key and player. model_artifacts: the trained model bytes by SHA-256.
All four are insert-only: a trigger rejects UPDATE and DELETE.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TABLES = ["model_artifacts", "forward_runs", "forward_inputs", "projections"]


def upgrade() -> None:
    op.execute("DROP TABLE projections")  # Phase 0 placeholder, never written
    op.execute(
        """CREATE TABLE model_artifacts (
            sha256 text PRIMARY KEY,
            name text NOT NULL,
            body bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        """CREATE TABLE forward_runs (
            id serial PRIMARY KEY,
            season integer NOT NULL,
            week integer NOT NULL,
            cutoff_at timestamptz NOT NULL,
            snapshot_at timestamptz NOT NULL,
            ingest_finished_at timestamptz,
            predicted_at timestamptz NOT NULL,
            status text NOT NULL CHECK (status IN ('on_time', 'late', 'early')),
            newest_game_kickoff timestamptz,
            sync_runs jsonb NOT NULL,
            models jsonb NOT NULL,
            scoring jsonb NOT NULL,
            ranges jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute(
        """CREATE TABLE forward_inputs (
            run_id integer NOT NULL REFERENCES forward_runs(id),
            player_id text NOT NULL,
            position text NOT NULL,
            features jsonb NOT NULL,
            games_used integer NOT NULL,
            latest_input timestamptz,
            PRIMARY KEY (run_id, player_id)
        )"""
    )
    op.execute(
        """CREATE TABLE projections (
            run_id integer NOT NULL REFERENCES forward_runs(id),
            policy text NOT NULL,
            scoring_key text NOT NULL,
            player_id text NOT NULL,
            points double precision,
            p10 double precision,
            p90 double precision,
            components jsonb,
            PRIMARY KEY (run_id, policy, scoring_key, player_id)
        )"""
    )
    op.execute("CREATE INDEX ON forward_runs (season, week, snapshot_at)")
    op.execute(
        """CREATE FUNCTION reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is insert-only (forward log)', TG_TABLE_NAME;
        END $$"""
    )
    for t in TABLES:
        op.execute(
            f"CREATE TRIGGER {t}_insert_only BEFORE UPDATE OR DELETE ON {t}"
            " FOR EACH ROW EXECUTE FUNCTION reject_change()"
        )


def downgrade() -> None:
    for t in reversed(TABLES):
        op.execute(f"DROP TABLE {t}")
    op.execute("DROP FUNCTION reject_change()")
    op.execute(
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
        )"""
    )
