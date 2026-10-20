import json
from datetime import UTC, datetime

import polars as pl

from fantasy_rag.analytics.scoring import ScoringRules, score
from fantasy_rag.ingest import nflverse as nv
from fantasy_rag.ingest import sleeper as sl
from fantasy_rag.ingest.base import search_name


def test_kickoff_is_eastern_wall_clock_converted_to_utc() -> None:
    raw = pl.DataFrame(
        {
            "game_id": ["a", "b", "c"], "season": [2026] * 3, "week": [1, 6, 12],
            "game_type": ["REG"] * 3, "home_team": ["LA", "JAX", "DET"],
            "away_team": ["SF", "HOU", "GB"], "gameday": ["2026-09-10", "2026-10-18", "2026-11-26"],
            "gametime": ["20:35", "09:30", None], "home_score": [1, 2, None],
            "away_score": [1, 2, None], "result": [0, 0, None], "spread_line": [1.0] * 3,
            "total_line": [40.0] * 3, "roof": ["outdoors"] * 3, "surface": ["grass"] * 3,
            "stadium": ["MCG", "Wembley", "Ford Field"],
        }
    )  # fmt: skip
    kick = nv.schedules(raw)["kickoff_at"].to_list()
    assert kick[0] == datetime(2026, 9, 11, 0, 35, tzinfo=UTC)  # EDT = UTC-4
    assert kick[1] == datetime(2026, 10, 18, 13, 30, tzinfo=UTC)  # London game, still ET
    assert kick[2] is None


def test_player_stats_drop_fantasy_points_and_nan() -> None:
    raw = pl.DataFrame(
        {
            "player_id": ["00-0000001"], "player_display_name": ["A B"], "position": ["WR"],
            "season": [2026], "week": [6], "season_type": ["REG"], "game_id": ["g"],
            "team": ["DET"], "opponent_team": ["GB"], "receptions": [5], "fg_pct": [float("nan")],
            "fantasy_points": [9.9], "fantasy_points_ppr": [14.9],
        }
    )  # fmt: skip
    stats = nv.player_stats(raw)["stats"][0]
    assert stats == '{"receptions":5,"fg_pct":null}'


def test_resolve_uses_ids_only_and_tries_routes_in_order() -> None:
    idmap = pl.DataFrame({"provider": ["espn"], "external_id": ["77"], "player_id": ["00-0000003"]})
    known = {"00-0000001", "00-0000003"}
    df = pl.DataFrame(
        {
            "gsis_id": ["00-0000001", None, "00-0000009", None],
            "espn_id": [None, "77", None, None],
            "name": ["Known", "Via ESPN", "Unknown GSIS", "Known"],  # same name, no ID
        }
    )
    resolved, unresolved = nv.resolve(df, [("gsis_id", "gsis"), ("espn_id", "espn")], idmap, known)
    assert resolved["player_id"].to_list() == ["00-0000001", "00-0000003"]
    assert unresolved.select("provider", "external_id", "name").rows() == [
        ("gsis", "00-0000009", "Unknown GSIS")
    ]


def test_resolve_reports_fallback_id_when_primary_is_missing() -> None:
    idmap = pl.DataFrame({"provider": ["espn"], "external_id": ["77"], "player_id": ["00-1"]})
    df = pl.DataFrame({"gsis_id": [None, None], "espn_id": ["404", None], "name": ["A", "B"]})
    resolved, unresolved = nv.resolve(df, [("gsis_id", "gsis"), ("espn_id", "espn")], idmap, set())
    assert resolved.is_empty()
    # "A" has only an unknown ESPN ID and must still be reported; "B" has no ID at all.
    assert unresolved.select("provider", "external_id", "name").rows() == [("espn", "404", "A")]


def test_resolve_keeps_an_existing_player_id_column() -> None:
    df = pl.DataFrame({"player_id": ["00-0000001", "00-0000002"], "x": [1, 2]})
    resolved, unresolved = nv.resolve(df, [("player_id", "gsis")], pl.DataFrame(), {"00-0000001"})
    assert resolved.select("player_id", "x").rows() == [("00-0000001", 1)]
    assert unresolved["external_id"].to_list() == ["00-0000002"]


def test_external_ids_report_conflicts_and_apply_overrides() -> None:
    players_raw = pl.DataFrame(
        {"gsis_id": ["00-1", "00-2"], "pfr_id": ["SameXx00", "SameXx00"], "espn_id": [None, "5"]}
    )
    ffids = pl.DataFrame(
        {"gsis_id": ["00-1"], "sleeper_id": [10.0], "pfr_id": [None], "espn_id": [None],
         "fantasypros_id": [None]}
    )  # fmt: skip
    overrides = pl.DataFrame(
        {"provider": ["sleeper"], "external_id": ["99"], "player_id": ["00-2"]}
    )
    good, conflicts = nv.external_ids(players_raw, ffids, overrides, {"00-1", "00-2"})
    assert set(good.rows()) == {
        ("espn", "5", "00-2"),
        ("sleeper", "10", "00-1"),
        ("sleeper", "99", "00-2"),
    }
    assert conflicts.rows() == [("pfr", "SameXx00", "00-1 | 00-2")]


def test_depth_charts_both_shapes() -> None:
    old = pl.DataFrame(
        {"week": [1, 1, None], "club_code": ["DET"] * 3, "position": ["WR", "WR", "WR"],
         "depth_team": ["2", "1", "1"], "gsis_id": ["00-1"] * 3,
         "formation": ["Offense", "Special Teams", "Offense"], "full_name": ["A"] * 3}
    )  # fmt: skip
    out = nv.depth_charts(old, 2024)
    assert out.select("week", "depth", "as_of").rows() == [
        (1, 1, None)
    ]  # best depth, null week dropped
    new = pl.DataFrame(
        {"dt": ["2026-10-20T06:01:57Z"] * 2, "team": ["GB"] * 2, "pos_abb": ["PK", "LCB"],
         "pos_rank": [1, 1], "gsis_id": ["00-2", "00-3"], "espn_id": ["9", "8"],
         "player_name": ["K", "CB"]}
    )  # fmt: skip
    out = nv.depth_charts(new, 2026)
    assert out.select("week", "position", "as_of").rows() == [
        (None, "K", datetime(2026, 10, 20, 6, 1, 57, tzinfo=UTC))
    ]


def test_injuries_blank_to_null_and_missing_date_modified() -> None:
    raw = pl.DataFrame(
        {"season": [2026], "game_type": ["REG"], "week": [6], "team": ["DET"], "gsis_id": ["00-1"],
         "report_status": ["None"], "report_primary_injury": ["Knee"],
         "report_secondary_injury": [""], "practice_status": ["\n    "],
         "practice_primary_injury": [None], "full_name": ["A"], "position": ["WR"]}
    )  # fmt: skip
    row = nv.injuries(raw).row(0, named=True)
    assert row["report_status"] is None and row["practice_status"] is None
    assert row["report_primary_injury"] == "Knee" and row["date_modified"] is None


def test_search_name() -> None:
    df = pl.DataFrame({"n": ["Amon-Ra St. Brown", "Kenneth Walker III", "José Ñúñez Jr."]})
    assert df.select(search_name("n"))["n"].to_list() == [
        "amonra st brown", "kenneth walker", "jose nunez"
    ]  # fmt: skip


def test_trending_window_and_team_defenses() -> None:
    now = datetime(2026, 10, 20, 14, 37, tzinfo=UTC)
    assert sl.window_start(now) == datetime(2026, 10, 20, 12, tzinfo=UTC)
    df, skipped = sl.trending_frame(
        [{"player_id": "123", "count": 5}, {"player_id": "DET", "count": 2}], [], 24, now
    )
    assert skipped == 1 and df["sleeper_id"].to_list() == ["123"]


def test_kick_distances_survive_ingestion_into_scoring() -> None:
    raw = pl.DataFrame(
        {"player_id": ["00-0000009"], "player_display_name": ["K"], "position": ["K"],
         "season": [2026], "week": [4], "season_type": ["REG"], "game_id": ["g"], "team": ["DAL"],
         "opponent_team": ["NYG"], "fg_made": [2], "fg_missed": [1], "fg_blocked": [1],
         "pat_made": [4], "pat_missed": [0], "pat_blocked": [0], "fg_made_list": ["39;50"],
         "fg_missed_list": ["65"], "fg_blocked_list": ["62"]}
    )  # fmt: skip
    stored = json.loads(nv.player_stats(raw)["stats"][0])  # what goes into the jsonb column
    rules = ScoringRules.from_sleeper(
        {"fgm": 3, "fgm_yds_over_30": 0.1, "xpm": 1, "fgmiss_50p": -1, "fgmiss_60p": -1}
    )
    # 6 + 2.9 (9 + 20 yards over 30) + 4 - 2 (65 miss: 50p, 60p) - 2 (62 blocked: 50p, 60p) = 8.9
    assert score(stored, "K", rules).points == 8.9
