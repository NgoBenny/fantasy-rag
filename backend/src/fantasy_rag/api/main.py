"""FastAPI: thin HTTP layer over the same functions the agent tools use, plus chat.

Run: `uv run uvicorn fantasy_rag.api.main:app --port 8010 --reload`. Everything except
`/api/chat` works without an LLM. With DEMO_MODE=true every route serves labelled
sample data (api/demo.py) and no database connection or API key is ever used.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from functools import cache
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from fantasy_rag.agent import tools
from fantasy_rag.agent.llm import AnthropicLLM, LLMClient
from fantasy_rag.analytics import queries as q
from fantasy_rag.api import demo
from fantasy_rag.api.chat import ChatRequest, ChatServices, process_budget, stream_chat
from fantasy_rag.config import Settings
from fantasy_rag.db import get_log_engine, get_readonly_engine
from fantasy_rag.ingest.health import SOURCES
from fantasy_rag.ingest.health import health as source_health
from fantasy_rag.ml import serve

app = FastAPI(title="fantasy-rag", version="0.1")
DEMO = Settings().demo_mode

# The header's "last refresh" per kind of data, from sync_runs source names.
STATUS_SOURCES = {kind: source for kind, (source, _) in SOURCES.items()}


@cache
def _engine() -> Engine:
    return get_readonly_engine()


@cache
def _log_engine() -> Engine:
    return get_log_engine()


def get_conn() -> Iterator[Connection | None]:
    if DEMO:  # demo mode never opens a database connection
        yield None
        return
    with _engine().connect() as conn:
        yield conn


Conn = Annotated[Connection, Depends(get_conn)]
Scoring = Literal["ppr", "half", "std"]


def _window(
    season: int | None,
    last_n: int | None,
    scoring: Scoring,
    league_id: int | None,
    start_week: int | None = None,
    end_week: int | None = None,
) -> tools.StatsWindow:
    try:
        return tools.StatsWindow(season=season, last_n=last_n, scoring=scoring,
                                 league_id=league_id, start_week=start_week,
                                 end_week=end_week)  # fmt: skip
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
    if DEMO:
        s = demo.get("status")
        return {"ok": True, "season": s["season"], "latest_week": s["latest_week"], "demo": True}
    season, week = q.latest_week(conn)
    return {"ok": True, "season": season, "latest_week": week}


def _last_ok(conn: Connection) -> dict[str, Any]:
    rows = conn.execute(
        text(
            "SELECT source, max(finished_at) AS last_ok,"
            " round(extract(epoch FROM now() - max(finished_at)) / 3600, 1) AS age_hours"
            " FROM sync_runs WHERE status = 'ok' GROUP BY source ORDER BY source"
        )
    )
    return {r.source: {"last_ok": r.last_ok, "age_hours": r.age_hours} for r in rows}


@app.get("/api/freshness")
def freshness(conn: Conn) -> list[dict[str, Any]]:
    if DEMO:
        rows: list[dict[str, Any]] = demo.get("freshness")
        return rows
    return [{"source": s, **v} for s, v in _last_ok(conn).items()]


@app.get("/api/status")
def status(conn: Conn) -> dict[str, Any]:
    """Season, week and last successful refresh per kind of data (the page header)."""
    if DEMO:
        return {**demo.get("status"), "demo": True}
    season, week = q.latest_week(conn)
    last = _last_ok(conn)
    return {
        "demo": False,
        "season": season,
        "latest_week": week,
        "next_week": week + 1,
        "sources": {k: last.get(src) for k, src in STATUS_SOURCES.items()},
        # fresh / stale / failing per source: the header warns on anything not fresh
        "health": source_health(conn, datetime.now(UTC)),
    }


@app.get("/api/players")
def players(
    conn: Conn,
    q_: Annotated[str, Query(alias="q", max_length=60)] = "",
    position: Literal["QB", "RB", "WR", "TE", "K"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[dict[str, Any]]:
    if DEMO:
        wanted = q_.lower()
        rows: list[dict[str, Any]] = demo.get("players")
        hits = [p for p in rows if wanted in p["name"].lower()]
        return [p for p in hits if not position or p["position"] == position][:limit]
    return q.search_players(conn, q_, position, limit)


@app.get("/api/players/{player_id}")
def player(
    conn: Conn,
    player_id: str,
    season: int | None = None,
    last_n: Annotated[int | None, Query(ge=1, le=18)] = None,
    scoring: Scoring = "ppr",
    league_id: int | None = None,
    start_week: Annotated[int | None, Query(ge=1, le=18)] = None,
    end_week: Annotated[int | None, Query(ge=1, le=18)] = None,
) -> dict[str, Any]:
    if DEMO:
        return demo.stats(player_id, scoring, season, last_n or start_week or end_week, league_id)
    ref = q.players_by_id(conn, [player_id]).get(player_id)
    if ref is None:
        raise HTTPException(404, f"no player {player_id}")
    w = _window(season, last_n, scoring, league_id, start_week, end_week)
    return _guard(tools._player_block, conn, ref, w)


def _ids(ids: str) -> list[str]:
    wanted = list(dict.fromkeys(i for i in ids.split(",") if i))
    if not 2 <= len(wanted) <= 4:
        raise HTTPException(422, "need 2-4 player IDs")
    return wanted


@app.get("/api/compare")
def compare(
    conn: Conn,
    ids: Annotated[str, Query(description="comma-separated player IDs, 2-4")],
    season: int | None = None,
    last_n: Annotated[int | None, Query(ge=1, le=18)] = None,
    scoring: Scoring = "ppr",
    league_id: int | None = None,
    start_week: Annotated[int | None, Query(ge=1, le=18)] = None,
    end_week: Annotated[int | None, Query(ge=1, le=18)] = None,
) -> dict[str, Any]:
    wanted = _ids(ids)
    if DEMO:
        window = last_n or start_week or end_week
        return {"players": [demo.stats(i, scoring, season, window, league_id) for i in wanted]}
    refs = q.players_by_id(conn, wanted)
    if missing := [i for i in wanted if i not in refs]:
        raise HTTPException(422, f"unknown player IDs: {missing}")
    w = _window(season, last_n, scoring, league_id, start_week, end_week)
    return {"players": [_guard(tools._player_block, conn, refs[i], w) for i in wanted]}


@app.get("/api/start-sit")
def start_sit(
    conn: Conn,
    ids: Annotated[str, Query(description="comma-separated player IDs, 2-4")],
    league_id: int | None = None,
    scoring: Scoring = "ppr",
) -> dict[str, Any]:
    """Next week's projections, league-scored, with bye/injury status (same code as the
    agent's compare_start_sit tool)."""
    wanted = _ids(ids)
    if DEMO:
        key = f"league:{league_id}" if league_id is not None else scoring
        saved = [demo.get("projection", key, i) for i in wanted]
        return {**tools.rank_projections(saved), "demo": True}
    refs = q.players_by_id(conn, wanted)
    if missing := [i for i in wanted if i not in refs]:
        raise HTTPException(422, f"unknown player IDs: {missing}")
    choice = tools.ScoringChoice(league_id=league_id, scoring=scoring)
    return _guard(tools.rank_start_sit, conn, [refs[i] for i in wanted], choice)


@app.get("/api/trending")
def trending(
    conn: Conn,
    kind: Literal["add", "drop"] = "add",
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> dict[str, Any]:
    if DEMO:
        found: dict[str, Any] = demo.get("trending", kind)
        return found
    return tools.get_trending(conn, tools.GetTrending(kind=kind, limit=limit))


@app.get("/api/leagues")
def leagues(conn: Conn) -> list[dict[str, Any]]:
    if DEMO:
        rows: list[dict[str, Any]] = demo.get("leagues")
        return rows
    return q.leagues(conn)


class LineupRequest(BaseModel):
    roster_id: int
    locked_player_ids: list[str] = Field(default_factory=list, max_length=20)


@app.post("/api/leagues/{league_id}/lineup")
def lineup(conn: Conn, league_id: int, body: LineupRequest) -> dict[str, Any]:
    if DEMO:
        found: dict[str, Any] = demo.get("lineup", f"{league_id}:{body.roster_id}")
        return found
    args = tools.OptimizeLineup(league_id=league_id, **body.model_dump())
    return _guard(tools.optimize_lineup, conn, args)


@app.get("/api/leagues/{league_id}/waivers")
def waivers(
    conn: Conn,
    league_id: int,
    position: Literal["QB", "RB", "WR", "TE", "K"] | None = None,
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> dict[str, Any]:
    """Best unrostered players by projection, with how current availability is."""
    if DEMO:
        found: dict[str, Any] = demo.get("waivers", f"{league_id}:{position or 'ALL'}")
        return found
    args = tools.RankWaivers(league_id=league_id, position=position, limit=limit)
    return _guard(tools.rank_waivers, conn, args)


@app.get("/api/model-runs")
def model_runs(conn: Conn) -> list[dict[str, Any]]:
    if DEMO:
        rows: list[dict[str, Any]] = demo.get("model_runs")
        return rows
    found = conn.execute(
        text(
            "SELECT model_version, training_window, features, metrics, created_at"
            " FROM model_runs ORDER BY created_at DESC"
        )
    )
    return [dict(r._mapping) for r in found]


@app.get("/api/forward-runs")
def forward_runs(conn: Conn) -> dict[str, Any]:
    """Phase 8: the Tuesday forward runs (newest first) and which policy serves each
    position (baseline unless a reviewed change activated another)."""
    if DEMO:
        return {"active": {}, "runs": demo.data().get("forward_runs", []), "demo": True}
    found = conn.execute(
        text(
            "SELECT id, season, week, status, cutoff_at, snapshot_at, predicted_at,"
            " newest_game_kickoff, models,"
            " (SELECT count(*) FROM forward_inputs i WHERE i.run_id = f.id) AS players"
            " FROM forward_runs f ORDER BY id DESC LIMIT 20"
        )
    )
    return {"active": serve.ACTIVE, "runs": [dict(r._mapping) for r in found]}


@app.get("/api/forward-results")
def forward_results(
    conn: Conn, scoring: Annotated[str, Query(max_length=20)] = "ppr"
) -> dict[str, Any]:
    """Baseline vs TE Ridge on finalized on-time forward weeks, under one set of scoring
    rules, plus every Tuesday's status (on_time / late / early_only / missing)."""
    if DEMO:
        return {"scoring": scoring, "rows": [], "matched_total": {"n": 0}, "tuesdays": [],
                "demo": True}  # fmt: skip
    from fantasy_rag.ml import forward_results as fr

    try:
        out = fr.results(conn, scoring)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return out | {"tuesdays": fr.tuesdays(conn, datetime.now(UTC))}


@app.get("/api/chat/samples")
def chat_samples() -> list[dict[str, Any]]:
    """Questions the demo can answer (none in live mode, where anything can be asked)."""
    if not DEMO:
        return []
    return [{"question": c["question"], "scenario": bool(c.get("scenario"))}
            for c in demo.data()["chat"]]  # fmt: skip


def _llm() -> LLMClient:
    return AnthropicLLM(budget=process_budget(Settings().llm_budget_usd))


def chat_services() -> ChatServices | None:
    """Live chat dependencies (None in demo mode); tests override this."""
    if DEMO:
        return None
    return ChatServices(_engine(), _log_engine(), _llm, Settings().sleeper_username)


@app.post("/api/chat")
async def chat(
    req: ChatRequest, svc: Annotated[ChatServices | None, Depends(chat_services)]
) -> StreamingResponse:
    """SSE: progress* -> answer | withheld | error -> done (see api/chat.py)."""
    body = demo.stream(req.question) if svc is None else stream_chat(svc, req)
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
