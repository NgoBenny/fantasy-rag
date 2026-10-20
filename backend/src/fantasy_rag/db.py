from typing import Any

from sqlalchemy import Engine, create_engine, event

from fantasy_rag.config import Settings


def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or Settings().database_url)


def get_readonly_engine(url: str | None = None) -> Engine:
    """Engine for agent tools: every connection is the SELECT-only role, read-only."""
    engine = create_engine(url or Settings().database_url)

    @event.listens_for(engine, "connect")
    def _restrict(dbapi_conn: Any, _record: Any) -> None:
        with dbapi_conn.cursor() as cur:
            cur.execute("SET ROLE fantasy_reader")
            cur.execute("SET default_transaction_read_only = on")
        dbapi_conn.commit()

    return engine
