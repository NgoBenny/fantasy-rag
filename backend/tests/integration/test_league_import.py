"""League import + scoring check on a synthetic Sleeper league (no real league data)."""

import json
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from fantasy_rag.analytics.scoring_check import check_league
from fantasy_rag.ingest.league import import_leagues, weeks_to_import

pytestmark = pytest.mark.db
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

LEAGUE = {
    "league_id": "999", "season": "2026", "name": "Test League", "settings": {"num_teams": 2},
    "roster_positions": ["WR", "K", "DEF", "BN"],
    "scoring_settings": {"rec": 0.5, "rec_yd": 0.1, "rec_td": 6, "fgm": 3, "xpm": 1,
                         "idp_tkl": 0, "pts_allow_0": 10, "sack": 1, "st_ff": 1, "idp_sack": 1},
}  # fmt: skip


class FakeSleeper:
    def get(self, path: str, **params: Any) -> Any:
        matchups = [
            # roster 1: a WR, a kicker and a team defense; Sleeper's own points per player
            {"roster_id": 1, "matchup_id": 1, "points": 22.6, "players": ["11", "22", "DET"],
             "starters": ["11", "22", "DET"],
             "players_points": {"11": 15.6, "22": 8.0, "DET": 13.0}},
            # roster 2: a player nflverse can't map
            {"roster_id": 2, "matchup_id": 1, "points": 0.0, "players": ["404"],
             "starters": ["404", "0"], "players_points": {"404": 0.0}},
        ]  # fmt: skip
        return {
            "/state/nfl": {"season": "2026", "week": 1, "season_type": "regular"},
            "/user/someone": {"user_id": "u1"},
            "/user/u1/leagues/nfl/2026": [{"league_id": "999"}],
            "/league/999": LEAGUE,
            "/league/999/users": [{"user_id": "u1", "display_name": "Owner One"}],
            "/league/999/rosters": [{"roster_id": 1, "owner_id": "u1"}, {"roster_id": 2}],
            "/league/999/matchups/1": matchups,
        }[path]


def test_import_and_scoring_check(scratch_db_url: str) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    engine = create_engine(scratch_db_url)
    with engine.begin() as conn:
        run = conn.execute(
            text("INSERT INTO sync_runs (source, status) VALUES ('t', 'ok') RETURNING id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO players (id, full_name, search_name, position, sync_run_id) VALUES"
                " ('00-11', 'Wide Out', 'wide out', 'WR', :r),"
                " ('00-22', 'Kick Er', 'kick er', 'K', :r)"
            ),
            {"r": run},
        )
        conn.execute(
            text(
                "INSERT INTO player_external_ids (provider, external_id, player_id, sync_run_id)"
                " VALUES ('sleeper', '11', '00-11', :r), ('sleeper', '22', '00-22', :r)"
            ),
            {"r": run},
        )
        wr = {"receptions": 5, "receiving_yards": 76, "receiving_tds": 1}  # 2.5 + 7.6 + 6 = 16.1
        k = {"fg_made": 1, "pat_made": 4}  # 3 + 4 = 7
        conn.execute(
            text(
                "INSERT INTO player_week_stats (season, week, season_type, player_id, stats,"
                " sync_run_id) VALUES (2026, 1, 'REG', '00-11', :wr, :r),"
                " (2026, 1, 'REG', '00-22', :k, :r)"
            ),
            {"wr": json.dumps(wr), "k": json.dumps(k), "r": run},
        )
        conn.execute(  # the kicker forced a fumble on a kickoff: + 1 st_ff
            text(
                "INSERT INTO player_week_special_teams (season, week, season_type, player_id,"
                " game_id, stats, sync_run_id) VALUES (2026, 1, 'REG', '00-22', 'g', :s, :r)"
            ),
            {"s": json.dumps({"st_ff": 1, "st_fum_rec": 0}), "r": run},
        )
        conn.execute(  # shutout with 3 sacks: 10 + 3 = 13
            text(
                "INSERT INTO team_week_defense (season, week, season_type, team, game_id,"
                " opponent, stats, sync_run_id) VALUES (2026, 1, 'REG', 'DET', 'g', 'GB', :s, :r)"
            ),
            {"s": json.dumps({"pts_allow": 0, "sack": 3}), "r": run},
        )

    first = import_leagues(engine, FakeSleeper(), "someone", None)
    assert first[0]["unresolved"] == 1 and first[0]["unsupported_scoring"] == ["idp_sack"]
    assert import_leagues(engine, FakeSleeper(), "someone", None)[0]["changed"] == 0

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT player_ids, starter_ids, owner_name FROM fantasy_rosters"
                " WHERE roster_id = 1"
            )
        ).one()
        assert row == (["00-11", "00-22"], ["00-11", "00-22"], "Owner One")  # DEF kept out
        result = check_league(
            conn, conn.execute(text("SELECT id FROM fantasy_leagues")).scalar_one()
        )
    engine.dispose()

    # WR: ours 16.1 vs Sleeper 15.6 -> mismatch; K 8 and DEF 13 match; 404 skipped.
    assert (result.compared, result.within, result.def_within, result.skipped_unresolved) == (
        3,
        2,
        1,
        1,
    )
    assert [(m.name, m.sleeper, m.ours) for m in result.mismatches] == [("Wide Out", 15.6, 16.1)]


def test_weeks_follow_the_league_season() -> None:
    regular = {"season": "2026", "week": 2, "season_type": "regular"}
    assert weeks_to_import(2025, regular) == 18  # completed past season: every week
    assert weeks_to_import(2026, regular) == 2
    assert weeks_to_import(2026, {**regular, "season_type": "post"}) == 18
    assert weeks_to_import(2026, {**regular, "season_type": "pre"}) == 0
    assert weeks_to_import(2027, regular) == 0


class HistoricalSleeper(FakeSleeper):
    """A 2025 league imported during 2026 week 2."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def get(self, path: str, **params: Any) -> Any:
        self.paths.append(path)
        if path == "/state/nfl":
            return {"season": "2026", "week": 2, "season_type": "regular"}
        if path == "/league/999":
            return {**LEAGUE, "season": "2025"}
        if path.startswith("/league/999/matchups/"):
            return super().get("/league/999/matchups/1") if path.endswith("/17") else []
        return super().get(path)


def test_historical_league_imports_all_weeks(scratch_db_url: str) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    engine = create_engine(scratch_db_url)
    api = HistoricalSleeper()
    summary = import_leagues(engine, api, None, "999")[0]
    with engine.connect() as conn:
        weeks = conn.execute(text("SELECT DISTINCT week FROM fantasy_rosters")).scalars().all()
    engine.dispose()
    assert summary["weeks"] == 18
    assert "/league/999/matchups/18" in api.paths and weeks == [17]
