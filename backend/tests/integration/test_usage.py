"""Usage context on the synthetic database: shares, coverage, completed weeks, batching."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text

from fantasy_rag.agent.router import ToolCall
from fantasy_rag.agent.tools import StatsWindow, _player_block, run_tool
from fantasy_rag.analytics import usage as u
from fantasy_rag.analytics.queries import players_by_id
from fantasy_rag.api.chat import _href
from fantasy_rag.db import get_readonly_engine

pytestmark = pytest.mark.db
NOW = datetime.now(UTC)
# Jefferson (00-1, MIN) weeks 3-8: targets 4, 5, 3 then 6, 8, 10 of 40 team targets each.
TARGETS = {3: 4, 4: 5, 5: 3, 6: 6, 7: 8, 8: 10}


@pytest.fixture
def season(db: str) -> str:
    eng = create_engine(db)
    with eng.begin() as c:
        run = c.execute(text("SELECT min(id) FROM sync_runs")).scalar_one()

        def ins(table: str, row: dict[str, Any]) -> None:
            cols = ", ".join([*row, "sync_run_id"])
            vals = ", ".join([f":{k}" for k in row] + [str(run)])
            c.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({vals})"), row)

        for week in range(1, 10):
            # Week 9: Thursday's game is over, Sunday's hasn't started: week 9 isn't complete.
            kick = NOW - timedelta(days=7 * (10 - week)) if week < 9 else NOW - timedelta(days=1)
            ins("schedules", {"game_id": f"g{week}", "season": 2026, "week": week,
                              "game_type": "REG", "home_team": "MIN", "away_team": "DAL",
                              "kickoff_at": kick})  # fmt: skip
        ins("schedules", {"game_id": "g9b", "season": 2026, "week": 9, "game_type": "REG",
                          "home_team": "NE", "away_team": "NYJ",
                          "kickoff_at": NOW + timedelta(days=2)})  # fmt: skip
        for week, tg in [*TARGETS.items(), (9, 12)]:
            gid = f"g{week}"
            ins("player_week_stats", {"season": 2026, "week": week, "season_type": "REG",
                                      "player_id": "00-1", "game_id": gid, "team": "MIN",
                                      "opponent": "DAL",
                                      "stats": json.dumps({"targets": tg})})  # fmt: skip
            # A teammate takes the rest of the targets and played every snap (exact total).
            ins("player_week_stats", {"season": 2026, "week": week, "season_type": "REG",
                                      "player_id": "00-3", "game_id": gid, "team": "MIN",
                                      "opponent": "DAL",
                                      "stats": json.dumps({"targets": 40 - tg})})  # fmt: skip
            ins("snap_counts", {"season": 2026, "week": week, "game_type": "REG",
                                "player_id": "00-3", "game_id": gid, "team": "MIN",
                                "offense_snaps": 60, "offense_pct": 1.0})  # fmt: skip
            ins("snap_counts", {"season": 2026, "week": week, "game_type": "REG",
                                "player_id": "00-1", "game_id": gid, "team": "MIN",
                                "offense_snaps": 30 + 3 * week, "offense_pct": None})  # fmt: skip
            if week != 4:  # week 4's play-by-play never loaded: red-zone looks unknown
                ins("pbp_games", {"season": 2026, "week": week, "season_type": "REG",
                                  "game_id": gid, "plays": 150})  # fmt: skip
        for week, rz in [(6, 1), (7, 2)]:  # week 8: covered, no row -> a known zero
            ins("player_week_pbp_usage", {"season": 2026, "week": week, "season_type": "REG",
                                          "player_id": "00-1", "game_id": f"g{week}",
                                          "stats": json.dumps({"rz_targets": rz, "rz_carries": 0,
                                                               "gl_carries": 0,
                                                               "rz_unknown": 0})})  # fmt: skip
    eng.dispose()
    return db


def test_get_usage_shares_coverage_and_facts(season: str) -> None:
    with get_readonly_engine(season).connect() as conn:  # the reader role can read it all
        r = run_tool(conn, "get_usage", {"players": [{"name": "Justin Jefferson"}]})
    assert r["as_of"]["through_week"] == 8  # Thursday of week 9 doesn't complete the week
    p = r["players"][0]
    assert [g["week"] for g in p["games"]] == [5, 6, 7, 8] and r["through_week"] == 8
    # Weeks 1-4 are recorded but outside the requested window; none inside it is missing.
    assert p["window"]["season_games_recorded"] == 8  # fixture weeks 1-2 plus 3-8
    assert p["window"]["games_outside_requested_window"] == 4
    assert p["window"]["weeks_without_a_game"] == []
    by_week = {g["week"]: g for g in p["games"]}
    assert by_week[8]["target_share_pct"] == 25.0  # 10 of 40
    assert by_week[8]["rz_looks"] == 0 and by_week[7]["rz_looks"] == 2  # known zero vs count
    # Window share is totals: (6 + 8 + 10) / 120 = 20.0%; snaps (48+51+54)/180 = 85.0%.
    assert (p["last_3"]["target_share_pct"], p["last_3"]["snap_pct"]) == (20.0, 85.0)
    texts = [f["text"] for f in p["facts"]]
    assert "Targets rose in each of the last 3 games: 6, 8, 10 (weeks 6, 7, 8)" in texts
    with get_readonly_engine(season).connect() as conn:
        full = u.usage_for(conn, ["00-1"], 2026, 8)["00-1"]
    assert next(g for g in full if g["week"] == 4)["rz_looks"] is None  # unknown, never 0


def test_usage_queries_are_batched(season: str) -> None:
    eng = get_readonly_engine(season)
    counts = []
    for ids in (["00-1"], ["00-1", "00-3", "00-4", "00-5", "00-6", "00-7"]):
        seen: list[str] = []

        def count(*args: Any, seen: list[str] = seen) -> None:
            seen.append(args[2])  # the SQL statement

        with eng.connect() as conn:
            event.listen(conn, "before_cursor_execute", count)
            u.usage_for(conn, ids, 2026, 8)
            event.remove(conn, "before_cursor_execute", count)
        counts.append(len([s for s in seen if "FROM" in s]))
    assert counts[0] == counts[1] <= 6  # the same handful of queries for 1 or 6 players


def test_start_sit_and_waivers_carry_usage(season: str) -> None:
    with get_readonly_engine(season).connect() as conn:
        pair = [{"name": "Justin Jefferson"}, {"name": "Free Agent"}]
        ss = run_tool(conn, "compare_start_sit", {"players": pair})
        wv = run_tool(conn, "rank_waivers", {"league_id": 1, "position": "WR"})
    usage = {p["player"]["id"]: p["usage"] for p in ss["ranked"]}
    assert usage["00-1"]["window"]["games"] == 3 and usage["00-1"]["facts"]
    assert usage["00-5"]["window"]["games"] == 2 and usage["00-5"]["facts"] == []  # < 3 games
    assert all("usage" in c for c in wv["candidates"]) and wv["usage_as_of"]["through_week"] == 8


def test_a_usage_citation_shows_each_players_own_games(season: str) -> None:
    """Two players with different playing histories: the cited page shows, per player,
    exactly the games the tool showed (not one shared calendar range)."""
    eng = create_engine(season)
    with eng.begin() as c:  # Free Agent (00-5) played weeks 1-2 (fixture) and then 6 and 8
        run = c.execute(text("SELECT min(id) FROM sync_runs")).scalar_one()
        for week in (6, 8):
            c.execute(text(
                "INSERT INTO player_week_stats (season, week, season_type, player_id, game_id,"
                " team, opponent, stats, sync_run_id) VALUES (2026, :w, 'REG', '00-5', :g,"
                " 'NE', 'NYJ', '{\"targets\": 3}', :r)"),
                {"w": week, "g": f"ne{week}", "r": run})  # fmt: skip
    eng.dispose()
    with get_readonly_engine(season).connect() as conn:
        args = {"players": [{"name": "Justin Jefferson"}, {"name": "Free Agent"}], "last_n": 3}
        r = run_tool(conn, "get_usage", args)
        shown = {p["player"]["id"]: [g["week"] for g in p["games"]] for p in r["players"]}
        assert shown == {"00-1": [6, 7, 8], "00-5": [2, 6, 8]}
        href = _href(ToolCall("r1", "get_usage", args, r))
        assert href == "/compare?ids=00-1,00-5&season=2026&end_week=8&usage_games=3#usage"
        # What /api/compare returns for that link's parameters, per player:
        w = StatsWindow(season=2026, end_week=8, usage_games=3)
        refs = players_by_id(conn, ["00-1", "00-5"])
        page = {i: [g["week"] for g in _player_block(conn, refs[i], w)["usage"]["games"]]
                for i in refs}  # fmt: skip
    assert page == shown


def test_usage_citation_opens_the_same_players_and_weeks() -> None:
    one = {"players": [{"player": {"id": "00-1"}}], "season": 2026, "through_week": 8,
           "last_n": 4}  # fmt: skip
    assert _href(ToolCall("r1", "get_usage", {}, one)) == (
        "/players/00-1?season=2026&end_week=8&usage_games=4#usage")  # fmt: skip
