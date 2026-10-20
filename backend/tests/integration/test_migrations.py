from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.db

ALL_TABLES = {
    "sync_runs", "sources", "players", "player_external_ids", "injury_snapshots", "trending",
    "fantasy_leagues", "fantasy_rosters", "projections", "model_runs", "documents", "chunks",
    "query_log", "schedules", "player_week_stats", "snap_counts", "ff_opportunity",
    "depth_charts", "injury_history", "unresolved_players", "team_week_defense",
    "player_week_special_teams",
}  # fmt: skip
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text("select tablename from pg_tables where schemaname = 'public'"))
        tables = {r[0] for r in rows} - {"alembic_version"}
    engine.dispose()
    return tables


def test_upgrade_creates_empty_tables_and_downgrade_removes_them(
    scratch_db_url: str,
) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))

    command.upgrade(cfg, "head")
    assert _tables(scratch_db_url) == ALL_TABLES
    engine = create_engine(scratch_db_url)
    with engine.connect() as conn:
        exts = {r[0] for r in conn.execute(text("select extname from pg_extension"))}
        assert {"vector", "pg_trgm"} <= exts
        for table in ALL_TABLES:
            assert conn.execute(text(f"select count(*) from {table}")).scalar() == 0
    engine.dispose()

    command.downgrade(cfg, "base")
    assert _tables(scratch_db_url) == set()

    command.upgrade(cfg, "head")  # round trip: downgrade left nothing behind
    assert _tables(scratch_db_url) == ALL_TABLES
