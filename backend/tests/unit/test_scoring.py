"""Hand-computed scoring fixtures. Each expected total is worked out in the comment."""

import pytest

from fantasy_rag.analytics.scoring import ScoringRules, score

BASE = {
    "pass_yd": 0.04, "pass_td": 4, "pass_int": -1, "pass_2pt": 2, "rush_yd": 0.1, "rush_td": 6,
    "rush_2pt": 2, "rec_yd": 0.1, "rec_td": 6, "rec_2pt": 2, "fum_lost": -2,
}  # fmt: skip
WR_LINE = {
    "receptions": 7, "receiving_yards": 112, "receiving_tds": 1, "receiving_2pt_conversions": 1,
    "carries": 1, "rushing_yards": 9, "fumbles_lost_total": 1,
}  # fmt: skip


@pytest.mark.parametrize(
    ("rec", "expected"),
    [
        # 11.2 rec yd + 6 rec TD + 2 two-pt + 0.9 rush yd - 2 fumble = 18.1 before receptions
        (0.0, 18.1),  # standard
        (0.5, 21.6),  # half-PPR: + 7 * 0.5
        (1.0, 25.1),  # PPR: + 7
    ],
)
def test_standard_half_and_full_ppr(rec: float, expected: float) -> None:
    rules = ScoringRules.from_sleeper({**BASE, "rec": rec})
    assert score(WR_LINE, "WR", rules).points == expected


def test_quarterback_line() -> None:
    rules = ScoringRules.from_sleeper({**BASE, "pass_td": 6})
    qb = {"passing_yards": 287, "passing_tds": 2, "passing_interceptions": 1, "rushing_yards": 31,
          "rushing_tds": 1, "sacks_suffered": 3}  # fmt: skip
    # 11.48 + 12 - 1 + 3.1 + 6 = 31.58
    assert score(qb, "QB", rules).points == 31.58


def test_kicker_blocked_kicks_are_misses_and_distance_buckets_overlap() -> None:
    rules = ScoringRules.from_sleeper(
        {"fgm": 3, "fgm_yds_over_30": 0.1, "xpm": 1, "xpmiss": -1, "fgmiss_50_59": -1,
         "fgmiss_50p": -1, "fgmiss_60p": -1, "fgmiss_20_29": -1}
    )  # fmt: skip
    k = {"fg_made": 2, "fg_made_list": "39;50", "fg_missed": 1, "fg_missed_list": "65",
         "fg_blocked": 1, "fg_blocked_list": "23", "pat_made": 4, "pat_missed": 0,
         "pat_blocked": 1}  # fmt: skip
    # fgm 6 + over-30 (9 + 20) * 0.1 = 2.9 + xpm 4 - xpmiss 1 (blocked PAT)
    # - 65 miss in 50p and 60p (2) - blocked 23 in 20_29 (1) = 8.9
    s = score(k, "K", rules)
    assert s.points == 8.9
    assert s.breakdown["fgmiss_50p"] == -1 and s.breakdown["fgmiss_20_29"] == -1


def test_common_bonuses() -> None:
    rules = ScoringRules.from_sleeper(
        {"rec": 1, "bonus_rec_te": 0.5, "bonus_rec_yd_100": 3, "bonus_rec_yd_200": 5,
         "bonus_rush_rec_yd_100": 2}
    )  # fmt: skip
    te = {"receptions": 6, "receiving_yards": 104, "rushing_yards": 0}
    # 6 rec + 3 TE premium + 100-199 receiving bonus 3 + 100-199 scrimmage bonus 2 = 14
    assert score(te, "TE", rules).points == 14.0
    # The TE premium does not apply to a WR with the same line: 6 + 3 + 2 = 11
    assert score(te, "WR", rules).points == 11.0
    # 200+ yards takes the 200 bonus, not the 100-199 one: 6 + 3 + 5 = 14 (+0 scrimmage 200 key)
    assert score({**te, "receiving_yards": 201}, "TE", rules).points == 14.0


def test_zero_keys_are_ignored_and_unsupported_keys_reported() -> None:
    # Sleeper ships idp_/bonus_ keys at 0 in leagues that don't use them.
    rules = ScoringRules.from_sleeper(
        {"rec": 1, "idp_tkl": 0, "bonus_rec_te": 0, "st_ff": 1, "sack": 1, "pts_allow_0": 10,
         "rec_0_4": 0.5, "idp_sack": 1}
    )  # fmt: skip
    assert set(rules.unsupported) == {"rec_0_4", "idp_sack"}
    # A per-catch key might change a WR's total; IDP keys can only touch a defense.
    assert score({"receptions": 1, "st_ff": 1}, "WR", rules).unscored == ["rec_0_4"]
    assert score({"receptions": 1, "st_ff": 1}, "WR", rules).points == 2.0  # rec + st_ff
    assert score({"sack": 2}, "DEF", rules).unscored == ["idp_sack"]


def test_team_defense() -> None:
    rules = ScoringRules.from_sleeper(
        {"sack": 1, "int": 2, "ff": 1, "fum_rec": 2, "safe": 2, "blk_kick": 2, "def_td": 6,
         "def_st_td": 6, "def_2pt": 2, "def_pass_def": 0.25, "def_3_and_out": 0.5,
         "def_4_and_stop": 1, "tkl_loss": 0.5, "pts_allow_0": 10, "pts_allow_1_6": 7,
         "pts_allow_7_13": 4, "pts_allow_14_20": 1, "pts_allow_28_34": -1, "pts_allow_35p": -4,
         "yds_allow_350_399": -1, "yds_allow_400_449": -2, "rec": 1}
    )  # fmt: skip
    week = {"sack": 3, "int": 1, "ff": 1, "fum_rec": 1, "def_td": 1, "def_pass_def": 6,
            "def_3_and_out": 2, "def_4_and_stop": 1, "tkl_loss": 5, "pts_allow": 13,
            "yds_allow": 351}  # fmt: skip
    # 3 + 2 + 1 + 2 + 6 + 1.5 + 1 + 1 + 2.5 + 4 (7-13 band) - 1 (350-399 band) = 23
    assert score(week, "DEF", rules).points == 23.0
    # Band edges are inclusive: 6 is in 1-6, 7 in 7-13, 0 only in the shutout band.
    for allowed, points in [(0, 10), (6, 7), (7, 4), (20, 1), (21, 0), (34, -1), (35, -4)]:
        assert score({"pts_allow": allowed}, "DEF", rules).points == points
    assert score({"pts_allow": 21, "yds_allow": 400}, "DEF", rules).points == -2
    assert score({}, "DEF", rules).points == 0  # bye week, not a shutout
    # A player's line never picks up team-defense keys (and the reverse).
    assert score({"receptions": 2, "sack": 1}, "WR", rules).points == 2.0


def test_individual_return_yards_are_reported_not_dropped() -> None:
    rules = ScoringRules.from_sleeper({"rec": 1, "kr_yd": 0.04, "pr_yd": 0.04, "def_kr_yd": 0.04})
    s = score({"receptions": 0, "kickoff_return_yards": 150}, "WR", rules)
    assert s.points == 0 and s.unscored == ["kr_yd", "pr_yd"]  # def_kr_yd is team DEF only


def test_presets_score_return_touchdowns() -> None:
    from fantasy_rag.analytics.queries import PRESETS

    # nflverse counts a kick/punt return TD as 6 in fantasy_points_ppr.
    for name in ("ppr", "half", "std"):
        rules = ScoringRules.from_sleeper(PRESETS[name])
        assert score({"special_teams_tds": 1}, "WR", rules).points == 6.0, name
