"""Agent tools on a synthetic database: ID-safe names, read-only role, waivers, validation."""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from fantasy_rag.agent.tools import run_tool
from fantasy_rag.analytics.queries import AmbiguousPlayer
from fantasy_rag.db import get_readonly_engine

pytestmark = pytest.mark.db
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

PLAYERS = [  # id, name, position, team
    ("00-1", "Justin Jefferson", "WR", "MIN"),
    ("00-2", "Justin Jefferson", "LB", "CLE"),  # same name, not a fantasy position
    ("00-3", "Mike Williams", "WR", "NYJ"),
    ("00-4", "Mike Williams", "TE", "WAS"),  # both active fantasy players
    ("00-5", "Free Agent", "WR", "NE"),
]
STATS = {  # player -> week -> stats
    "00-1": {1: {"receptions": 8, "receiving_yards": 100, "targets": 10},
             2: {"receptions": 6, "receiving_yards": 80, "targets": 9}},
    "00-3": {1: {"receptions": 2, "receiving_yards": 20}},
    "00-4": {2: {"receptions": 3, "receiving_yards": 30}},
    "00-5": {1: {"receptions": 5, "receiving_yards": 70},
             2: {"receptions": 4, "receiving_yards": 50}},
}  # fmt: skip


@pytest.fixture
def db(scratch_db_url: str) -> str:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    engine = create_engine(scratch_db_url)
    with engine.begin() as c:
        run = c.execute(
            text("INSERT INTO sync_runs (source, status) VALUES ('t', 'ok') RETURNING id")
        ).scalar_one()
        for pid, name, pos, team in PLAYERS:
            c.execute(
                text(
                    "INSERT INTO players (id, full_name, search_name, position, team, sync_run_id)"
                    " VALUES (:i, :n, lower(:n), :p, :t, :r)"
                ),
                {"i": pid, "n": name, "p": pos, "t": team, "r": run},
            )
        for pid, weeks in STATS.items():
            for week, st in weeks.items():
                c.execute(
                    text(
                        "INSERT INTO player_week_stats (season, week, season_type, player_id,"
                        " opponent, stats, sync_run_id) VALUES (2026, :w, 'REG', :p, 'DAL', :s, :r)"
                    ),
                    {"w": week, "p": pid, "s": json.dumps(st), "r": run},
                )
        league = c.execute(
            text(
                "INSERT INTO fantasy_leagues (provider, external_league_id, season,"
                " scoring_settings, roster_positions, sync_run_id)"
                " VALUES ('sleeper', 'x', 2026, :s, '[]', :r) RETURNING id"
            ),
            {"s": json.dumps({"rec": 1, "rec_yd": 0.1}), "r": run},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO fantasy_rosters (league_id, roster_id, week, player_ids, sync_run_id)"
                " VALUES (:l, 1, 2, '{00-1}', :r)"
            ),
            {"l": league, "r": run},
        )
    engine.dispose()
    return scratch_db_url


def test_names_resolve_by_fantasy_position_and_ambiguity_is_an_error(db: str) -> None:
    with get_readonly_engine(db).connect() as conn:
        r = run_tool(conn, "get_player_stats", {"player": {"name": "Justin Jefferson"}})
        assert r["player"]["id"] == "00-1" and r["totals"]["targets"] == 19
        assert r["total_points"] == pytest.approx(14 + 18)  # PPR: 14 rec + 180 yds
        with pytest.raises(AmbiguousPlayer) as err:
            run_tool(conn, "get_player_stats", {"player": {"name": "Mike Williams"}})
        assert {c.id for c in err.value.candidates} == {"00-3", "00-4"}
        narrowed = run_tool(
            conn, "get_player_stats", {"player": {"name": "Mike Williams", "position": "TE"}}
        )
        assert narrowed["player"]["id"] == "00-4"


def test_waivers_exclude_rostered_players(db: str) -> None:
    with get_readonly_engine(db).connect() as conn:
        r = run_tool(conn, "rank_waivers", {"league_id": 1, "position": "WR"})
    ids = [c["id"] for c in r["candidates"]]
    assert "00-1" not in ids and ids[0] == "00-5"
    assert r["rostered_players_excluded"] == 1


def test_tools_cannot_write_and_reject_bad_input(db: str) -> None:
    with get_readonly_engine(db).connect() as conn:
        with pytest.raises(DBAPIError):
            conn.execute(text("DELETE FROM players"))
        conn.rollback()
        with pytest.raises(ValueError, match="unknown tool"):
            run_tool(conn, "run_sql", {"sql": "DROP TABLE players"})
        with pytest.raises(ValidationError):
            run_tool(
                conn, "get_player_stats", {"player": {"name": "X"}, "last_n": 3, "start_week": 1}
            )
        with pytest.raises(ValidationError):
            run_tool(conn, "get_trending", {"limit": 500})
