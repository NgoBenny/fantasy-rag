from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError

from fantasy_rag.config import Settings

pytestmark = pytest.mark.db

OWNED_TABLES = {
    "sync_runs", "sources", "players", "player_external_ids", "injury_snapshots", "trending",
    "fantasy_leagues", "fantasy_rosters", "projections", "model_runs", "documents", "chunks",
    "query_log",
}  # fmt: skip
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture
def scratch_db_url() -> Iterator[str]:
    """A throwaway `<db>_migtest` database, so the test never touches dev data."""
    base = make_url(Settings().database_url)
    name = f"{base.database}_migtest"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
        pytest.skip(f"no Postgres reachable at {base.render_as_string(hide_password=True)}")
    yield base.set(database=name).render_as_string(hide_password=False)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text("select tablename from pg_tables where schemaname = 'public'"))
        tables = {r[0] for r in rows} - {"alembic_version"}
    engine.dispose()
    return tables


def test_upgrade_creates_empty_owned_tables_and_downgrade_removes_them(
    scratch_db_url: str,
) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))

    command.upgrade(cfg, "head")
    assert _tables(scratch_db_url) == OWNED_TABLES
    engine = create_engine(scratch_db_url)
    with engine.connect() as conn:
        exts = {r[0] for r in conn.execute(text("select extname from pg_extension"))}
        assert {"vector", "pg_trgm"} <= exts
        for table in OWNED_TABLES:
            assert conn.execute(text(f"select count(*) from {table}")).scalar() == 0
    engine.dispose()

    command.downgrade(cfg, "base")
    assert _tables(scratch_db_url) == set()

    command.upgrade(cfg, "head")  # round trip: downgrade left nothing behind
    assert _tables(scratch_db_url) == OWNED_TABLES
