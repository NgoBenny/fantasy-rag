"""FastAPI: thin read-only HTTP layer over the same functions the agent tools use.

Run: `uv run uvicorn fantasy_rag.api.main:app --reload` (port 8000). No LLM involved.
"""

from collections.abc import Iterator
from functools import cache
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from fantasy_rag.agent import tools
from fantasy_rag.analytics import queries as q
from fantasy_rag.db import get_readonly_engine

app = FastAPI(title="fantasy-rag", version="0.1")


@cache
def _engine() -> Engine:
    return get_readonly_engine()


def get_conn() -> Iterator[Connection]:
    with _engine().connect() as conn:
        yield conn


Conn = Annotated[Connection, Depends(get_conn)]
Scoring = Literal["ppr", "half", "std"]


def _window(
    season: int | None, last_n: int | None, scoring: Scoring, league_id: int | None
) -> tools.StatsWindow:
    try:
        return tools.StatsWindow(season=season, last_n=last_n, scoring=scoring, league_id=league_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _guard(fn: Any, *args: Any) -> dict[str, Any]:
    """Domain errors (unknown player/league, ambiguous name) become 404 / 422, not 500."""
    try:
        result: dict[str, Any] = fn(*args)
    except q.AmbiguousPlayer as e:
        raise HTTPException(422, str(e)) from e
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return result


@app.get("/api/health")
def health(conn: Conn) -> dict[str, Any]:
    season, week = q.latest_week(conn)
    return {"ok": True, "season": season, "latest_week": week}


@app.get("/api/freshness")
def freshness(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT source, max(finished_at) AS last_ok,"
            " round(extract(epoch FROM now() - max(finished_at)) / 3600, 1) AS age_hours"
            " FROM sync_runs WHERE status = 'ok' GROUP BY source ORDER BY source"
        )
    )
    return [dict(r._mapping) for r in rows]


@app.get("/api/players")
def players(
    conn: Conn,
    q_: Annotated[str, Query(alias="q", max_length=60)] = "",
    position: Literal["QB", "RB", "WR", "TE", "K"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[dict[str, Any]]:
    return q.search_players(conn, q_, position, limit)


@app.get("/api/players/{player_id}")
def player(
    conn: Conn,
    player_id: str,
    season: int | None = None,
    last_n: Annotated[int | None, Query(ge=1, le=18)] = None,
    scoring: Scoring = "ppr",
    league_id: int | None = None,
) -> dict[str, Any]:
    ref = q.players_by_id(conn, [player_id]).get(player_id)
    if ref is None:
        raise HTTPException(404, f"no player {player_id}")
    w = _window(season, last_n, scoring, league_id)
    return _guard(tools._player_block, conn, ref, w)


@app.get("/api/compare")
def compare(
    conn: Conn,
    ids: Annotated[str, Query(description="comma-separated player IDs, 2-4")],
    season: int | None = None,
    last_n: Annotated[int | None, Query(ge=1, le=18)] = None,
    scoring: Scoring = "ppr",
    league_id: int | None = None,
) -> dict[str, Any]:
    wanted = [i for i in ids.split(",") if i][:4]
    refs = q.players_by_id(conn, wanted)
    missing = [i for i in wanted if i not in refs]
    if len(wanted) < 2 or missing:
        raise HTTPException(422, f"need 2-4 known player IDs; unknown: {missing}")
    w = _window(season, last_n, scoring, league_id)
    return {"players": [_guard(tools._player_block, conn, refs[i], w) for i in wanted]}


@app.get("/api/trending")
def trending(
    conn: Conn,
    kind: Literal["add", "drop"] = "add",
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> dict[str, Any]:
    return tools.get_trending(conn, tools.GetTrending(kind=kind, limit=limit))


@app.get("/api/leagues")
def leagues(conn: Conn) -> list[dict[str, Any]]:
    return q.leagues(conn)


class LineupRequest(BaseModel):
    roster_id: int
    locked_player_ids: list[str] = Field(default_factory=list, max_length=20)


@app.post("/api/leagues/{league_id}/lineup")
def lineup(conn: Conn, league_id: int, body: LineupRequest) -> dict[str, Any]:
    args = tools.OptimizeLineup(league_id=league_id, **body.model_dump())
    return _guard(tools.optimize_lineup, conn, args)
