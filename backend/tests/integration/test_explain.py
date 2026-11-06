"""Phase 11: recomputed baseline changes through the tool, start/sit and citation links."""

import json
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from fantasy_rag.agent.answer import _known, numbers
from fantasy_rag.agent.router import ToolCall
from fantasy_rag.agent.tools import run_tool
from fantasy_rag.api.chat import _href
from fantasy_rag.db import get_readonly_engine

pytestmark = pytest.mark.db


def _sql(db: str, sql: str, **params: Any) -> None:
    engine = create_engine(db)
    with engine.begin() as c:
        run = c.execute(text("SELECT max(id) FROM sync_runs")).scalar_one()
        c.execute(text(sql), {"r": run, **params})
    engine.dispose()


def _every_number_is_in_the_result(r: dict[str, Any]) -> None:
    known = _known(json.dumps({k: v for k, v in r.items() if k != "explanation"}, default=str))
    for line in [*r["explanation"], r["summary"]]:
        assert all(float(n) in known for n in numbers(line)), line


def test_one_new_game_is_explained_with_its_weight_and_labelled_recomputed(db: str) -> None:
    with get_readonly_engine(db).connect() as conn:
        r = run_tool(conn, "explain_projection", {"player": {"name": "Justin Jefferson"}})
    assert r["label"] == "recomputed baseline change" and r["available"]
    assert (r["season"], r["since_week"], r["week"]) == (2026, 2, 3)
    assert r["before"]["points"] == 18.0  # PPR: 8 rec + 100 yds in week 1
    (step,) = r["steps"]
    assert (step["week"], step["game_points"], step["projection_before"]) == (2, 14.0, 18.0)
    assert r["baseline_change"] == step["change"] < 0
    assert r["explanation"][0].startswith("Recomputed baseline change, week 2 to week 3:")
    assert "below his projection before it" in r["explanation"][1]
    assert r["explanation"][1].endswith(f"moved it down {abs(step['change']):.2f}.")
    assert any("not a record of the number shown" in c for c in r["caveats"])
    _every_number_is_in_the_result(r)


def test_league_rules_and_unscored_warnings_carry_through(db: str) -> None:
    with get_readonly_engine(db).connect() as conn:
        r = run_tool(conn, "explain_projection", {"player": {"name": "Free Agent"}, "league_id": 1})
    assert r["unscored_rules"] == ["rec_0_4"]
    assert r["before"]["points"] == 12.0  # league 1: 5 rec + 7.0 for 70 yds
    assert r["steps"][0]["game_points"] == 10.0  # 4 rec + 5.0 + st_ff 1 (league scores it)


def test_a_week_without_a_game_is_labelled_from_the_schedule(db: str) -> None:
    _sql(db, "UPDATE player_week_stats SET team = 'NYJ' WHERE player_id = '00-3'")
    # Week 2 played (NYJ off), week 3 still to come: the projection week is 3.
    _sql(db, "INSERT INTO schedules (game_id, season, week, game_type, home_team, away_team,"
             " kickoff_at, sync_run_id) VALUES ('g2', 2026, 2, 'REG', 'MIN', 'DAL',"
             " '2026-09-20 17:00+00', :r), ('g3', 2026, 3, 'REG', 'NYJ', 'DAL',"
             " '2099-09-27 17:00+00', :r)")  # fmt: skip
    with get_readonly_engine(db).connect() as conn:
        r = run_tool(conn, "explain_projection",
                     {"player": {"name": "Mike Williams", "position": "WR"},
                      "season": 2026, "since_week": 2, "week": 3})  # fmt: skip
    assert r["steps"] == [] and r["baseline_change"] == 0.0
    assert r["no_game_weeks"] == [{"week": 2, "status": "bye"}]
    assert "Week 2: no game (bye), no change." in r["explanation"]


def test_an_out_player_shows_the_served_zero_beside_a_positive_change(db: str) -> None:
    _sql(db, "UPDATE player_week_stats SET stats = :s WHERE player_id = '00-1' AND week = 2",
         s=json.dumps({"receptions": 10, "receiving_yards": 150}))  # fmt: skip
    _sql(db, "INSERT INTO injury_snapshots (player_id, status, source, sync_run_id)"
             " VALUES ('00-1', 'Out', 'sleeper', :r)")  # fmt: skip
    with get_readonly_engine(db).connect() as conn:
        r = run_tool(conn, "explain_projection", {"player": {"name": "Justin Jefferson"}})
        pair = [{"name": "Justin Jefferson"}, {"name": "Never Played"}]
        ss = run_tool(conn, "compare_start_sit", {"players": pair})
    assert r["baseline_change"] > 0 and r["served"]["points"] == 0.0
    assert (
        r["explanation"][-1]
        == "Separately, his served projection for 2026 week 3 is 0.0: Out (sleeper)."
    )
    _every_number_is_in_the_result(r)
    changes = {p["player"]["name"]: p["change"] for p in ss["ranked"]}
    assert changes["Justin Jefferson"]["served"]["points"] == 0.0
    never = changes["Never Played"]
    assert not never["available"] and never["summary"] == "comparison unavailable"
    assert never["reason"] == "no games before week 2"


def test_malformed_windows_are_rejected_and_unsupported_ones_are_results(db: str) -> None:
    with get_readonly_engine(db).connect() as conn:
        for bad in ({"since_week": 3, "week": 3}, {"week": 19}, {"season": 2021, "week": 3}):
            with pytest.raises(ValidationError):
                run_tool(conn, "explain_projection", {"player": {"name": "Zero Guy"}, **bad})
        far = run_tool(conn, "explain_projection",
                       {"player": {"name": "Zero Guy"}, "since_week": 1, "week": 9})  # fmt: skip
    # Week 9 is still ahead here (week 3 is current); "more than 6 weeks" is unit-tested.
    assert not far["available"] and far["reason"] == "week 9 has not been reached"
    with get_readonly_engine(db).connect() as conn:
        # A season alone means the current week; another season needs its week.
        same = run_tool(
            conn, "explain_projection", {"player": {"name": "Zero Guy"}, "season": 2026}
        )
        other = run_tool(
            conn, "explain_projection", {"player": {"name": "Zero Guy"}, "season": 2025}
        )
    assert same["week"] == 3
    assert not other["available"] and other["reason"] == "give the week to compare in 2025"


def test_the_citation_pins_the_interval_it_explained(db: str) -> None:
    with get_readonly_engine(db).connect() as conn:
        args = {"player": {"name": "Justin Jefferson"}, "scoring": "half"}
        r = run_tool(conn, "explain_projection", args)
    window = "season=2026&since=2&week=3"
    assert _href(ToolCall("r1", "explain_projection", args, r)) == (
        f"/compare?ids=00-1&scoring=half&{window}"
    )
    league = {**args, "league_id": 1}
    assert _href(ToolCall("r1", "explain_projection", league, r)) == (
        f"/league?league=1&view=startsit&ids=00-1&{window}"
    )
    # Once the projection week moves on, the same saved result still links to weeks 2 to 3.
    later = {**r, "served": {**r["served"], "week": 4}}
    assert window in (_href(ToolCall("r1", "explain_projection", args, later)) or "")


def test_unplayed_or_unloaded_weeks_are_unavailable_not_missed_games(db: str) -> None:
    # Weeks 2 and 3 have kicked off long ago, week 4 is ahead; week 3 has no stats yet.
    for w, kick in ((2, "2026-09-20 17:00+00"), (3, "2026-09-27 17:00+00"),
                    (4, "2099-10-04 17:00+00")):  # fmt: skip
        _sql(db, "INSERT INTO schedules (game_id, season, week, game_type, home_team, away_team,"
                 " kickoff_at, sync_run_id) VALUES (:g, 2026, :w, 'REG', 'MIN', 'DAL', :k, :r)",
             g=f"g{w}", w=w, k=kick)  # fmt: skip
    with get_readonly_engine(db).connect() as conn:
        now = run_tool(conn, "explain_projection", {"player": {"name": "Justin Jefferson"}})
        ahead = run_tool(conn, "explain_projection",
                         {"player": {"name": "Justin Jefferson"}, "week": 9})  # fmt: skip
    assert (now["since_week"], now["week"]) == (3, 4)
    assert not now["available"] and now["reason"] == "week 3 stats are not loaded yet"
    assert not ahead["available"] and ahead["reason"] == "week 9 has not been reached"
    assert ahead["explanation"] == ["Comparison unavailable: week 9 has not been reached."]


def test_one_player_start_sit_for_a_cited_change(db: str) -> None:
    from fastapi.testclient import TestClient

    from fantasy_rag.api.main import app, get_conn

    def conn() -> Any:
        with get_readonly_engine(db).connect() as c:
            yield c

    app.dependency_overrides[get_conn] = conn
    try:
        client = TestClient(app)
        one = client.get("/api/start-sit", params={"ids": "00-1", "since_week": 2, "week": 3})
        bad = client.get("/api/start-sit", params={"ids": "00-1", "since_week": 3, "week": 3})
    finally:
        app.dependency_overrides.clear()
    assert one.status_code == 200
    assert one.json()["ranked"][0]["change"]["since_week"] == 2
    assert bad.status_code == 422


def test_a_result_without_a_week_links_to_the_player_only() -> None:
    r = {"player": {"id": "00-1", "name": "Justin Jefferson"}, "season": 2025, "since_week": None,
         "week": None, "available": False}  # fmt: skip
    assert _href(ToolCall("r1", "explain_projection", {"season": 2025}, r)) == "/players/00-1"
