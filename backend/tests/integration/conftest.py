"""A small synthetic league database shared by the tools and API tests."""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

PLAYERS = [  # id, name, position, team
    ("00-1", "Justin Jefferson", "WR", "MIN"),
    ("00-2", "Justin Jefferson", "LB", "CLE"),  # same name, not a fantasy position
    ("00-3", "Mike Williams", "WR", "NYJ"),
    ("00-4", "Mike Williams", "TE", "WAS"),  # both active fantasy players
    ("00-5", "Free Agent", "WR", "NE"),
    ("00-6", "Zero Guy", "TE", "KC"),
    ("00-7", "Negative Guy", "TE", "LV"),
    ("00-8", "Never Played", "TE", "SF"),
    ("00-9", "Also Unplayed", "TE", "SEA"),
]
STATS = {  # player -> week -> stats
    "00-6": {1: {}},  # played, scored 0
    "00-7": {1: {"fumbles_lost_total": 1}},  # -2
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
        c.execute(  # a special-teams forced fumble, stored apart from player_week_stats
            text(
                "INSERT INTO player_week_special_teams (season, week, season_type, player_id,"
                " game_id, stats, sync_run_id) VALUES (2026, 2, 'REG', '00-5', 'g', :s, :r)"
            ),
            {"s": json.dumps({"st_ff": 1, "st_fum_rec": 0}), "r": run},
        )
        league = c.execute(
            text(
                "INSERT INTO fantasy_leagues (provider, external_league_id, season,"
                " scoring_settings, roster_positions, sync_run_id)"
                " VALUES ('sleeper', 'x', 2026, :s, :pos, :r) RETURNING id"
            ),
            {
                "s": json.dumps({"rec": 1, "rec_yd": 0.1, "st_ff": 1, "rec_0_4": 0.5}),
                "pos": json.dumps(["WR", "FLEX", "BN"]),
                "r": run,
            },
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
