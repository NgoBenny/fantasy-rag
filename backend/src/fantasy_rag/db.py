from typing import Any

from sqlalchemy import Engine, create_engine, event

from fantasy_rag.config import Settings


def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or Settings().database_url)


def _with_role(url: str | None, role: str, read_only: bool) -> Engine:
    engine = create_engine(url or Settings().database_url)

    @event.listens_for(engine, "connect")
    def _restrict(dbapi_conn: Any, _record: Any) -> None:
        with dbapi_conn.cursor() as cur:
            cur.execute(f"SET ROLE {role}")
            if read_only:
                cur.execute("SET default_transaction_read_only = on")
        dbapi_conn.commit()

    return engine


def get_log_engine(url: str | None = None) -> Engine:
    """Engine for chat logging: the role may only INSERT into query_log."""
    return _with_role(url, "fantasy_logger", read_only=False)


def get_readonly_engine(url: str | None = None) -> Engine:
    """Engine for agent tools: every connection is the SELECT-only role, read-only."""
    return _with_role(url, "fantasy_reader", read_only=True)
