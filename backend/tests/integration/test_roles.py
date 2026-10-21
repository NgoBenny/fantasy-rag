"""Database roles: tools read only; chat logging may only insert into query_log."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from fantasy_rag.db import get_log_engine, get_readonly_engine

pytestmark = pytest.mark.db
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture
def url(scratch_db_url: str) -> str:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    return scratch_db_url


def _fails(engine: object, sql: str) -> bool:
    try:
        with engine.begin() as c:  # type: ignore[attr-defined]
            c.execute(text(sql))
    except DBAPIError:
        return True
    return False


def test_logger_may_only_insert_into_query_log(url: str) -> None:
    log = get_log_engine(url)
    with log.begin() as c:
        # INSERT ... RETURNING would need SELECT; the sequence's currval needs only USAGE.
        c.execute(text("INSERT INTO query_log (question, status) VALUES ('q', 'ok')"))
        qid = c.execute(text("SELECT currval('query_log_id_seq')")).scalar_one()
    assert qid == 1
    for sql in (
        "SELECT * FROM query_log",
        "INSERT INTO query_log (question) VALUES ('q') RETURNING question",
        "UPDATE query_log SET answer = 'x'",
        "DELETE FROM query_log",
        "SELECT * FROM players",
        "INSERT INTO sources (name, license, attribution, storage_allowed, refresh_cadence)"
        " VALUES ('x', 'x', 'x', 'link', 'x')",
    ):
        assert _fails(log, sql), sql


def test_reader_cannot_write_query_log(url: str) -> None:
    assert _fails(get_readonly_engine(url), "INSERT INTO query_log (question) VALUES ('q')")
    with create_engine(url).connect() as c:  # the owner still sees what was logged
        assert c.execute(text("SELECT count(*) FROM query_log")).scalar_one() == 0
    assert _fails(get_readonly_engine(url), "UPDATE query_log SET answer = 'x'")
