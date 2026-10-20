from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError

from fantasy_rag.config import Settings


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
