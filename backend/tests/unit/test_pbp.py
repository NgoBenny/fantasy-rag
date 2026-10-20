"""Hand-built play-by-play -> team DEF and special-teams stats (Sleeper definitions)."""

import json
from typing import Any

import polars as pl

from fantasy_rag.ingest import pbp

GAME = {"season": 2026, "week": 1, "season_type": "REG", "game_id": "2026_01_GB_LA",
        "home_team": "LA", "away_team": "GB", "home_score": 21, "away_score": 13}  # fmt: skip
GB_OFF = {"posteam": "GB", "defteam": "LA"}


def _frame(plays: list[dict[str, Any]]) -> pl.DataFrame:
    rows = [{c: None for c in pbp.COLUMNS} | GAME | p for p in plays]
    text = {c for c in pbp.COLUMNS if c.endswith(("team", "_id", "type", "result"))}
    return pl.DataFrame(
        rows, schema={c: pl.String if c in text else pl.Float64 for c in pbp.COLUMNS}
    ).with_columns(pl.col("season", "week").cast(pl.Int64))


PLAYS = [
    # GB drive 1: run, sack (credited), punt; no first down -> a 3-and-out for LA.
    GB_OFF | {"play_type": "run", "rushing_yards": 5, "yards_gained": 5, "fixed_drive": 1,
              "tackle_for_loss_1_player_id": None},
    GB_OFF | {"play_type": "pass", "sack": 1, "yards_gained": -7, "sack_player_id": "00-1",
              "tackle_for_loss_1_player_id": "00-1", "fixed_drive": 1},
    GB_OFF | {"play_type": "punt", "fixed_drive": 1},
    # GB drive 2: 15-yard first down, two passes defended, then a pick-six on 4th down.
    GB_OFF | {"play_type": "pass", "passing_yards": 15, "yards_gained": 15, "first_down": 1,
              "fixed_drive": 3},
    GB_OFF | {"play_type": "pass", "pass_defense_1_player_id": "00-2",
              "pass_defense_2_player_id": "00-3", "fixed_drive": 3},
    GB_OFF | {"play_type": "pass", "interception": 1, "touchdown": 1, "td_team": "LA",
              "fourth_down_failed": 1, "fixed_drive": 3},
    # 4th-down failures: one wiped out by a defensive-penalty first down, one a real stop.
    GB_OFF | {"play_type": "pass", "fourth_down_failed": 1, "first_down_penalty": 1,
              "first_down": 1, "fixed_drive": 5},
    GB_OFF | {"play_type": "run", "fourth_down_failed": 1, "fixed_drive": 5},
    # LA kicks off (posteam is the receiver): LA forces and recovers a GB fumble.
    {"play_type": "kickoff", "posteam": "GB", "defteam": "LA",
     "forced_fumble_player_1_team": "LA", "forced_fumble_player_1_player_id": "00-4",
     "fumbled_1_team": "GB", "fumble_recovery_1_team": "LA",
     "fumble_recovery_1_player_id": "00-5"},
    # GB punts and LA returns it for a TD; GB muffs nothing. Return TDs are def_st_td.
    {"play_type": "punt", "posteam": "GB", "defteam": "LA", "touchdown": 1, "td_team": "LA",
     "punt_blocked": 1},
    # GB's own fumble recovery on a punt return: not a special-teams recovery for anyone.
    {"play_type": "punt", "posteam": "LA", "defteam": "GB", "fumbled_1_team": "GB",
     "fumble_recovery_1_team": "GB", "fumble_recovery_1_player_id": "00-9"},
]  # fmt: skip


def test_team_defense_follows_sleeper_definitions() -> None:
    out = pbp.team_defense(_frame(PLAYS))
    la = out.filter(pl.col("team") == "LAR").row(0, named=True)  # nflverse "LA" -> "LAR"
    assert la["opponent"] == "GB"
    assert {k: la[k] for k in la if k not in pbp.KEYS + ["team", "opponent"]} == {
        "sack": 1, "int": 1, "ff": 0, "fum_rec": 0, "def_st_ff": 1, "def_st_fum_rec": 1,
        "def_td": 1, "def_st_td": 1, "safe": 0, "blk_kick": 1, "def_2pt": 0,
        "def_4_and_stop": 1, "def_pass_def": 2, "tkl_loss": 1,
        "yds_allow": 13,  # 5 rushing + 15 passing - 7 sack
        "pts_allow": 13, "def_3_and_out": 1,
    }  # fmt: skip
    gb = out.filter(pl.col("team") == "GB").row(0, named=True)
    # LA's 21 minus its pick-six; the punt-return TD still counts against GB's defense.
    assert gb["pts_allow"] == 15


def test_special_teams_credits_forcer_and_opponent_recoveries_only() -> None:
    out = pbp.special_teams(_frame(PLAYS))
    assert out.select("player_id", "st_ff", "st_fum_rec").rows() == [
        ("00-4", 1, 0),
        ("00-5", 0, 1),
    ]
    packed = pbp.packed(out, [*pbp.KEYS, "player_id"])
    assert json.loads(packed["stats"][0]) == {"st_ff": 1, "st_fum_rec": 0}
