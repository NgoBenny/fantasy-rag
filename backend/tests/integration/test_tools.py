"""Agent tools on a synthetic database: ID-safe names, read-only role, waivers, validation."""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from fantasy_rag.agent.tools import run_tool
from fantasy_rag.analytics.queries import AmbiguousPlayer, resolve_player, weekly_rows
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
        by_team = run_tool(
            conn, "get_player_stats", {"player": {"name": "Mike Williams", "team": "NYJ"}}
        )
        assert by_team["player"]["id"] == "00-3"


def test_a_wrong_team_never_swaps_in_another_player(db: str) -> None:
    # A stale team hint (player traded) must not pick a teammate who shares a name word.
    with get_readonly_engine(db).connect() as conn:
        r = run_tool(conn, "get_player_stats", {"player": {"name": "Zero Guy", "team": "LV"}})
        assert r["player"]["id"] == "00-6"
        r = run_tool(
            conn, "get_player_stats", {"player": {"name": "Justin Jefferson", "team": "WAS"}}
        )
        assert r["player"]["id"] == "00-1"


def test_no_team_hint_never_picks_a_player_with_no_team(db: str) -> None:
    # Omitted hint (None) must not "match" a player whose team is unknown (None).
    with create_engine(db).connect() as conn, conn.begin() as tx:
        conn.execute(text("UPDATE players SET team = NULL WHERE id = '00-3'"))
        with pytest.raises(AmbiguousPlayer) as err:
            resolve_player(conn, "Mike Williams")
        assert {c.id for c in err.value.candidates} == {"00-3", "00-4"}
        tx.rollback()


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


def test_bye_and_ruled_out_players_project_zero_and_never_start(db: str) -> None:
    engine = create_engine(db)
    with engine.begin() as c:
        run = c.execute(text("SELECT max(id) FROM sync_runs")).scalar_one()
        # Week 3 (next week): MIN and NYJ play; NE (Free Agent's team) is on bye.
        c.execute(
            text(
                "INSERT INTO schedules (game_id, season, week, game_type, home_team, away_team,"
                " sync_run_id) VALUES ('g3', 2026, 3, 'REG', 'MIN', 'NYJ', :r)"
            ),
            {"r": run},
        )
        c.execute(
            text(
                "INSERT INTO injury_snapshots (player_id, status, source, sync_run_id) VALUES"
                " ('00-1', 'Questionable', 'sleeper', :r), ('00-3', 'Out', 'sleeper', :r)"
            ),
            {"r": run},
        )
    engine.dispose()
    with get_readonly_engine(db).connect() as conn:
        bye = run_tool(conn, "project_player", {"player": {"name": "Free Agent"}})
        assert bye["projected_points"] == 0.0 and bye["availability"]["status"] == "bye"
        q = run_tool(conn, "project_player", {"player": {"name": "Justin Jefferson"}})
        assert q["projected_points"] > 0 and q["availability"]["status"] == "Questionable"
        out = run_tool(
            conn, "project_player", {"player": {"name": "Mike Williams", "position": "WR"}}
        )
        assert out["projected_points"] == 0.0 and out["availability"]["status"] == "Out"
        waivers = run_tool(conn, "rank_waivers", {"league_id": 1, "position": "WR"})
        free_agent = next(c for c in waivers["candidates"] if c["id"] == "00-5")
        assert free_agent["projected_points"] == 0.0  # healthy but on bye
        lineup = run_tool(conn, "optimize_lineup", {"league_id": 1, "roster_id": 1})
        assert lineup["recommended"][0]["player"]["name"] == "Justin Jefferson"  # Q still starts


def test_router_refuses_a_player_the_question_does_not_name(db: str) -> None:
    from fantasy_rag.agent.router import swapped_player

    swap = {"player": {"name": "Free Agent"}}  # a roster WR, while the question names another
    with get_readonly_engine(db).connect() as conn:
        msg = swapped_player(conn, "is jefferson actually a safe start?", swap)
        assert msg is not None and "Justin Jefferson (WR, MIN)" in msg
        assert swapped_player(conn, "is my receiver a safe start?", swap) is None
        assert swapped_player(conn, "is jefferson better than agent?", swap) is None
        # Named player looked up in another call: the roster player is a fair comparison.
        also = [{"player": {"name": "Justin Jefferson"}}]
        assert swapped_player(conn, "start jefferson over my guy?", swap, also) is None
        assert (
            swapped_player(
                conn, "start jefferson over my tight end?", {"player": {"name": "Zero Guy"}}
            )
            is None
        )  # different position: never a swap
