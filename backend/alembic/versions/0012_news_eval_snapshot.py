"""Frozen news-evaluation corpus: copies of stories and chunks, insert-only.

The golden news sets were labelled on the stories stored by a given time, but feeds
later revise those stories' dates and text, so grading on the live tables drifts. A named
snapshot (`fantasy-rag eval news-snapshot`) copies the permitted fields (title, summary,
URL, date, player tags, chunk text and embedding) once; `eval news` searches it. Rows
can't be updated or deleted (trigger `reject_change`, from 0008). The reader role gets
SELECT through the default privileges from 0004. No FK to the live tables: a snapshot
outlives the stories it copied.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("news_eval_documents", "news_eval_chunks")


def upgrade() -> None:
    op.execute(
        """CREATE TABLE news_eval_documents (
            snapshot text NOT NULL,
            id bigint NOT NULL,
            source text NOT NULL,
            url text NOT NULL,
            title text NOT NULL,
            summary text,
            published_at timestamptz,
            teams text[] NOT NULL,
            player_ids text[] NOT NULL,
            captured_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (snapshot, id)
        )"""
    )
    op.execute(
        """CREATE TABLE news_eval_chunks (
            snapshot text NOT NULL,
            id bigint NOT NULL,
            document_id bigint NOT NULL,
            text text NOT NULL,
            embedding vector(384),
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
            player_ids text[] NOT NULL,
            published_at timestamptz,
            PRIMARY KEY (snapshot, id),
            FOREIGN KEY (snapshot, document_id) REFERENCES news_eval_documents (snapshot, id)
        )"""
    )
    for t in TABLES:
        op.execute(
            f"CREATE TRIGGER {t}_insert_only BEFORE UPDATE OR DELETE ON {t}"
            " FOR EACH ROW EXECUTE FUNCTION reject_change()"
        )


def downgrade() -> None:
    op.execute("DROP TABLE news_eval_chunks")
    op.execute("DROP TABLE news_eval_documents")
