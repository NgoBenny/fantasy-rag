"""A partial Thursday week must not advance forecasts or unlock played players."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from fantasy_rag.agent.router import context_block
from fantasy_rag.agent.tools import run_tool
from fantasy_rag.analytics import queries as q
from fantasy_rag.api.main import status
from fantasy_rag.db import get_readonly_engine
from fantasy_rag.ml import serve

pytestmark = pytest.mark.db
NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture
def thursday(db: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(q, "datetime", Clock)
    engine = create_engine(db)
    with engine.begin() as c:
        run = c.execute(text("SELECT min(id) FROM sync_runs")).scalar_one()
        for gid, week, home, away, kickoff in [
            ("previous", 6, "MIN", "NE", NOW - timedelta(days=6)),
            ("thursday", 7, "MIN", "NE", NOW - timedelta(hours=12)),
            ("sunday", 7, "NYJ", "WAS", NOW + timedelta(days=2)),
            ("monday", 7, "SF", "SEA", NOW + timedelta(days=3)),
            ("next", 8, "MIN", "NE", NOW + timedelta(days=6)),
        ]:
            c.execute(
                text(
                    "INSERT INTO schedules (game_id, season, week, game_type, home_team,"
                    " away_team, kickoff_at, sync_run_id)"
                    " VALUES (:g, 2026, :w, 'REG', :h, :a, :k, :r)"
                ),
                {"g": gid, "w": week, "h": home, "a": away, "k": kickoff, "r": run},
            )
        # Thursday's freshly published score must not become input to a week-7 forecast.
        c.execute(
            text(
                "INSERT INTO player_week_stats (season, week, season_type, player_id, stats,"
                " sync_run_id) VALUES (2026, 7, 'REG', '00-5', :s, :r)"
            ),
            {"s": json.dumps({"receiving_yards": 10000}), "r": run},
        )
        for pid in ("00-1", "00-3", "00-5", "00-8"):
            c.execute(
                text(
                    "INSERT INTO player_external_ids"
                    " (provider, external_id, player_id, sync_run_id)"
                    " VALUES ('sleeper', :p, :p, :r)"
                ),
                {"p": pid, "r": run},
            )
        c.execute(
            text(
                "UPDATE fantasy_rosters SET player_ids = ARRAY['00-1','00-3','00-5','00-8'],"
                ' raw = \'{"starters":["00-1","00-3"]}\' WHERE league_id = 1'
            )
        )
        # Even an Out starter is fixed after kickoff: the optimizer cannot undo the slot.
        c.execute(
            text(
                "INSERT INTO injury_snapshots (player_id, status, source, sync_run_id)"
                " VALUES ('00-1', 'Out', 'sleeper', :r)"
            ),
            {"r": run},
        )
    engine.dispose()
    return db


def test_thursday_stats_do_not_advance_any_projection_caller(thursday: str) -> None:
    with get_readonly_engine(thursday).connect() as c:
        projection = run_tool(c, "project_player", {"player": {"name": "Free Agent"}})
        waivers = run_tool(c, "rank_waivers", {"league_id": 1})
        lineup = run_tool(c, "optimize_lineup", {"league_id": 1, "roster_id": 1})
        assert projection["week"] == waivers["week"] == lineup["week"] == 7
        assert projection["projected_points"] < 20  # not Thursday's newly published 1,000
        assert status(c)["next_week"] == 7
        context = context_block(c, [], None)
        assert "Projection week: 7" in context
        assert "complete through week 7" not in context


def test_started_starter_stays_and_started_bench_player_cannot_enter(thursday: str) -> None:
    with get_readonly_engine(thursday).connect() as c:
        lineup = run_tool(c, "optimize_lineup", {"league_id": 1, "roster_id": 1})
    first = lineup["recommended"][0]
    assert first["player"]["id"] == "00-1" and first["locked"] is True
    assert first["player"]["game_started_at"] is not None
    assert "00-5" not in [s["player"]["id"] for s in lineup["recommended"] if s["player"]]
    assert lineup["changes"] == {"start": [], "sit": []}
    assert next(p for p in lineup["bench"] if p["id"] == "00-5")["game_started_at"]
    assert (
        sum(s["player"]["projected_points"] for s in lineup["recommended"] if s["player"])
        == lineup["recommended_total"]
    )


def test_kickoff_locks_start_at_kickoff_not_before(thursday: str, monkeypatch) -> None:
    class BeforeKickoff(Clock):
        @classmethod
        def now(cls, tz=None):
            return NOW - timedelta(hours=13)

    monkeypatch.setattr(q, "datetime", BeforeKickoff)
    with get_readonly_engine(thursday).connect() as c:
        before = run_tool(c, "optimize_lineup", {"league_id": 1, "roster_id": 1})
    assert not any(s["locked"] for s in before["recommended"])
    assert "Free Agent" in before["changes"]["start"]

    class AtKickoff(Clock):
        @classmethod
        def now(cls, tz=None):
            return NOW - timedelta(hours=12)

    monkeypatch.setattr(q, "datetime", AtKickoff)
    with get_readonly_engine(thursday).connect() as c:
        after = run_tool(c, "optimize_lineup", {"league_id": 1, "roster_id": 1})
    assert after["recommended"][0]["locked"]
    assert "Free Agent" not in after["changes"]["start"]


@pytest.mark.parametrize("pid", ["00-5", "00-8", "00-7"])
def test_started_flex_stays_even_without_history_or_with_negative_points(thursday, pid) -> None:
    engine = create_engine(thursday)
    with engine.begin() as c:
        c.execute(text("UPDATE players SET team = 'MIN' WHERE id = :p"), {"p": pid})
        if pid == "00-7":
            c.execute(
                text(
                    "UPDATE fantasy_leagues SET scoring_settings = scoring_settings ||"
                    " '{\"fum_lost\": -2}'::jsonb WHERE id = 1"
                )
            )
            run = c.execute(text("SELECT min(id) FROM sync_runs")).scalar_one()
            c.execute(
                text(
                    "INSERT INTO player_external_ids"
                    " (provider, external_id, player_id, sync_run_id)"
                    " VALUES ('sleeper', :p, :p, :r)"
                ),
                {"p": pid, "r": run},
            )
        c.execute(
            text(
                "UPDATE fantasy_rosters SET player_ids = array_append(player_ids, :p),"
                " raw = CAST(:raw AS jsonb) WHERE league_id = 1"
            ),
            {"p": pid, "raw": json.dumps({"starters": ["00-1", pid]})},
        )
    engine.dispose()
    with get_readonly_engine(thursday).connect() as c:
        lineup = run_tool(c, "optimize_lineup", {"league_id": 1, "roster_id": 1})
    flex = lineup["recommended"][1]
    assert flex["locked"] and flex["player"]["id"] == pid
    assert lineup["changes"] == {"start": [], "sit": []}
    assert lineup["gain"] == 0
    if pid == "00-7":
        assert flex["player"]["projected_points"] == -2.0
    if pid == "00-8":
        assert flex["player"]["projected_points"] is None


def test_out_locked_starter_does_not_keep_a_healthy_scoring_range(thursday, monkeypatch) -> None:
    original = serve.project

    def with_ranges(*args, **kwargs):
        return {
            pid: replace(p, range_80=(2.0, 20.0)) if p else None
            for pid, p in original(*args, **kwargs).items()
        }

    monkeypatch.setattr(serve, "project", with_ranges)
    with get_readonly_engine(thursday).connect() as c:
        lineup = run_tool(c, "optimize_lineup", {"league_id": 1, "roster_id": 1})
    starter = lineup["recommended"][0]
    assert starter["locked"] and starter["player"]["projected_points"] == 0
    assert starter["player"]["range_80"] is None


def test_projection_week_uses_schedule_before_and_after_final_game(thursday: str) -> None:
    with get_readonly_engine(thursday).connect() as c:
        assert q.projection_week(c, NOW - timedelta(days=2)) == (2026, 7)
        assert q.projection_week(c, NOW + timedelta(days=3, hours=3)) == (2026, 7)
        assert q.projection_week(c, NOW + timedelta(days=3, hours=4)) == (2026, 8)
        # Never invent regular-season week 19 after the final known game.
        assert q.projection_week(c, NOW + timedelta(days=90)) == (2026, 8)
