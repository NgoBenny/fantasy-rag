"""Phase 8 (review fixes): frozen calibration errors, and the policy each row really used.

calibration_errors: insert-only. Seeded once from out-of-sample predictions, then grown
only from saved forward predictions whose outcomes are final; a recorded error never
changes. projections.effective_policy: "baseline" on a challenger's row when the player
had too little history and the challenger fell back to the baseline.

(0008 is already applied to the shared dev database with immutable rows, so these
changes are a second Phase 8 migration instead of an edit to 0008.)

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE projections ADD COLUMN effective_policy text")
    op.execute(
        """CREATE TABLE calibration_errors (
            policy text NOT NULL,
            scoring_key text NOT NULL,
            position text NOT NULL,
            player_id text NOT NULL,
            season integer NOT NULL,
            week integer NOT NULL,
            kickoff timestamptz NOT NULL,
            error double precision NOT NULL,
            source text NOT NULL CHECK (source IN ('seed', 'forward')),
            run_id integer REFERENCES forward_runs(id),
            recorded_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (policy, scoring_key, player_id, season, week)
        )"""
    )
    op.execute(
        "CREATE TRIGGER calibration_errors_insert_only BEFORE UPDATE OR DELETE"
        " ON calibration_errors FOR EACH ROW EXECUTE FUNCTION reject_change()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE calibration_errors")
    op.execute("ALTER TABLE projections DROP COLUMN effective_policy")
