"""Phase 8 (review fix): calibration errors keyed by the scoring rules that produced them.

Errors were filed by scoring key ("league:3"), so a league that changed its rules would
have calibrated new predictions with errors made under the old rules. They are now keyed
by a fingerprint of the scored weights (ScoringRules.fingerprint). The table is recreated
(its rows are insert-only and can't be re-keyed); the seed, recorded earlier the same day
on the unmerged Phase 8 branch and never used by an on-time run, is recorded again.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

COLUMNS = """
    policy text NOT NULL,
    {key} text NOT NULL,
    position text NOT NULL,
    player_id text NOT NULL,
    season integer NOT NULL,
    week integer NOT NULL,
    kickoff timestamptz NOT NULL,
    error double precision NOT NULL,
    source text NOT NULL CHECK (source IN ('seed', 'forward')),
    run_id integer REFERENCES forward_runs(id),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (policy, {key}, player_id, season, week)
"""


def _create(key: str) -> None:
    op.execute(f"CREATE TABLE calibration_errors ({COLUMNS.format(key=key)})")
    op.execute(
        "CREATE TRIGGER calibration_errors_insert_only BEFORE UPDATE OR DELETE"
        " ON calibration_errors FOR EACH ROW EXECUTE FUNCTION reject_change()"
    )


def upgrade() -> None:
    op.execute("DROP TABLE calibration_errors")
    _create("rules_fp")


def downgrade() -> None:
    op.execute("DROP TABLE calibration_errors")
    _create("scoring_key")
