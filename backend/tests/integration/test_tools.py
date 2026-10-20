"""Agent tools on a synthetic database: ID-safe names, read-only role, waivers, validation."""

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from fantasy_rag.agent.tools import run_tool
from fantasy_rag.analytics.queries import AmbiguousPlayer, weekly_rows
from fantasy_rag.db import get_readonly_engine

pytestmark = pytest.mark.db


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


def test_projections_report_unscored_rules_and_rank_zero_above_negative(db: str) -> None:
    with get_readonly_engine(db).connect() as conn:
        proj = run_tool(conn, "project_player", {"player": {"name": "Free Agent"}, "league_id": 1})
        assert proj["unscored_rules"] == ["rec_0_4"]  # league 1 scores a rule we can't compute
        ss = run_tool(
            conn,
            "compare_start_sit",
            {"players": [{"name": "Negative Guy"}, {"name": "Zero Guy"}], "scoring": "ppr"},
        )
        assert [p["projected_points"] for p in ss["ranked"]] == [0.0, -2.0]
        assert ss["higher_projection"] == "Zero Guy"
        none = run_tool(
            conn,
            "compare_start_sit",
            {"players": [{"name": "Never Played"}, {"name": "Also Unplayed"}]},
        )
        assert none["higher_projection"] is None


def test_weekly_rows_merge_special_teams_stats(db: str) -> None:
    with get_readonly_engine(db).connect() as conn:
        rows = weekly_rows(conn, ["00-5"], [2026])
    assert [r["stats"].get("st_ff") for r in rows] == [None, 1]
    assert rows[1]["stats"]["receptions"] == 4
