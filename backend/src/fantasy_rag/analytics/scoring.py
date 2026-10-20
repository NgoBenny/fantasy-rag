"""League scoring rules -> fantasy points, from nflverse raw stats. No LLM, no I/O.

Sleeper scores a player as sum(stat[key] * weight[key]) over its scoring keys, so each
supported key maps to one nflverse-derived stat value. Keys we cannot compute are
reported per player, never estimated.
"""

import math
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

Stats = Mapping[str, Any]

# Team defense and IDP keys only score DEF / defensive players, so their absence does not
# make an offensive player's or kicker's total incomplete.
TEAM_DEF_OR_IDP = re.compile(
    r"^(def_|idp_|pts_allow|yds_allow|int$|int_ret|sack|ff$|fum_rec$|fum_ret|safe$|blk_kick"
    r"|tkl|qb_hit|kr_yd|pr_yd|fg_ret|bonus_def|bonus_sack|bonus_tkl)"
)
DEF_POSITIONS = {"DEF", "DST", "D/ST"}


def _n(stats: Stats, key: str) -> float:
    v = stats.get(key)
    return float(v) if isinstance(v, int | float) and not math.isnan(v) else 0.0


def _col(key: str) -> Callable[[Stats, str], float]:
    return lambda s, _pos: _n(s, key)


def _sum(*keys: str) -> Callable[[Stats, str], float]:
    return lambda s, _pos: sum(_n(s, k) for k in keys)


def _distances(stats: Stats, key: str) -> list[int]:
    raw = stats.get(key)
    return [int(d) for d in str(raw).split(";") if d.strip().isdigit()] if raw else []


def _missed(lo: int, hi: int | None) -> Callable[[Stats, str], float]:
    """Missed FGs in [lo, hi] yards. Sleeper counts a blocked FG as a miss at its distance."""

    def f(s: Stats, _pos: str) -> float:
        kicks = _distances(s, "fg_missed_list") + _distances(s, "fg_blocked_list")
        return float(sum(1 for d in kicks if d >= lo and (hi is None or d <= hi)))

    return f


def _yds_over_30(s: Stats, _pos: str) -> float:
    return float(sum(max(0, d - 30) for d in _distances(s, "fg_made_list")))


def _if_position(position: str, key: str) -> Callable[[Stats, str], float]:
    return lambda s, pos: _n(s, key) if pos == position else 0.0


def _game_bonus(lo: float, hi: float | None, *keys: str) -> Callable[[Stats, str], float]:
    """1 when the summed yards land in [lo, hi). Sleeper's labels read e.g. "100-199" / "200+"."""

    def f(s: Stats, _pos: str) -> float:
        total = sum(_n(s, k) for k in keys)
        return 1.0 if total >= lo and (hi is None or total < hi) else 0.0

    return f


# Sleeper key -> stat derived from nflverse weekly player stats.
DERIVE: dict[str, Callable[[Stats, str], float]] = {
    # passing
    "pass_yd": _col("passing_yards"),
    "pass_td": _col("passing_tds"),
    "pass_int": _col("passing_interceptions"),
    "pass_2pt": _col("passing_2pt_conversions"),
    "pass_att": _col("attempts"),
    "pass_cmp": _col("completions"),
    "pass_inc": lambda s, _p: _n(s, "attempts") - _n(s, "completions"),
    "pass_sack": _col("sacks_suffered"),
    "pass_fd": _col("passing_first_downs"),
    # rushing
    "rush_yd": _col("rushing_yards"),
    "rush_td": _col("rushing_tds"),
    "rush_2pt": _col("rushing_2pt_conversions"),
    "rush_att": _col("carries"),
    "rush_fd": _col("rushing_first_downs"),
    # receiving
    "rec": _col("receptions"),
    "rec_yd": _col("receiving_yards"),
    "rec_td": _col("receiving_tds"),
    "rec_2pt": _col("receiving_2pt_conversions"),
    "rec_tgt": _col("targets"),
    "rec_fd": _col("receiving_first_downs"),
    # ball security and other offensive scores
    # all fumbles lost, including special-teams muffs (matches Sleeper's points)
    "fum_lost": _col("fumbles_lost_total"),
    "fum": _col("fumbles_total"),
    "fum_rec_td": _col("fumble_recovery_tds"),
    "st_td": _col("special_teams_tds"),
    # common bonuses (hand-verified only; no imported league uses them yet)
    "bonus_rec_te": _if_position("TE", "receptions"),
    "bonus_rec_rb": _if_position("RB", "receptions"),
    "bonus_rec_wr": _if_position("WR", "receptions"),
    "bonus_pass_yd_300": _game_bonus(300, 400, "passing_yards"),
    "bonus_pass_yd_400": _game_bonus(400, None, "passing_yards"),
    "bonus_rush_yd_100": _game_bonus(100, 200, "rushing_yards"),
    "bonus_rush_yd_200": _game_bonus(200, None, "rushing_yards"),
    "bonus_rec_yd_100": _game_bonus(100, 200, "receiving_yards"),
    "bonus_rec_yd_200": _game_bonus(200, None, "receiving_yards"),
    "bonus_rush_rec_yd_100": _game_bonus(100, 200, "rushing_yards", "receiving_yards"),
    "bonus_rush_rec_yd_200": _game_bonus(200, None, "rushing_yards", "receiving_yards"),
    # kicking
    "fgm": _col("fg_made"),
    "fgmiss": _sum("fg_missed", "fg_blocked"),  # blocked kicks count as misses
    "fgm_0_19": _col("fg_made_0_19"),
    "fgm_20_29": _col("fg_made_20_29"),
    "fgm_30_39": _col("fg_made_30_39"),
    "fgm_40_49": _col("fg_made_40_49"),
    "fgm_50_59": _col("fg_made_50_59"),
    "fgm_60p": _col("fg_made_60_"),
    "fgm_50p": _sum("fg_made_50_59", "fg_made_60_"),
    "fgmiss_0_19": _missed(0, 19),
    "fgmiss_20_29": _missed(20, 29),
    "fgmiss_30_39": _missed(30, 39),
    "fgmiss_40_49": _missed(40, 49),
    "fgmiss_50_59": _missed(50, 59),
    "fgmiss_60p": _missed(60, None),
    "fgmiss_50p": _missed(50, None),
    "fgm_yds": lambda s, _p: float(sum(_distances(s, "fg_made_list"))),
    "fgm_yds_over_30": _yds_over_30,
    "xpm": _col("pat_made"),
    "xpmiss": _sum("pat_missed", "pat_blocked"),
}


class ScoringRules(BaseModel):
    weights: dict[str, float]  # supported, non-zero
    unsupported: dict[str, float]  # non-zero keys we cannot compute

    @classmethod
    def from_sleeper(cls, scoring_settings: Mapping[str, Any]) -> "ScoringRules":
        nonzero = {k: float(v) for k, v in scoring_settings.items() if v}
        return cls(
            weights={k: v for k, v in nonzero.items() if k in DERIVE},
            unsupported={k: v for k, v in nonzero.items() if k not in DERIVE},
        )

    def unscored_for(self, position: str) -> list[str]:
        """Unsupported keys that could change this position's total."""
        if position in DEF_POSITIONS:
            return ["team defense scoring (not supported yet)"]
        return sorted(k for k in self.unsupported if not TEAM_DEF_OR_IDP.match(k))


class Score(BaseModel):
    points: float
    breakdown: dict[str, float]
    unscored: list[str]  # non-empty means `points` may be incomplete for this player


def score(stats: Stats, position: str, rules: ScoringRules) -> Score:
    unscored = rules.unscored_for(position)
    if position in DEF_POSITIONS:
        return Score(points=0.0, breakdown={}, unscored=unscored)
    breakdown = {}
    for key, weight in rules.weights.items():
        value = DERIVE[key](stats, position) * weight
        if value:
            breakdown[key] = round(value, 4)
    return Score(points=round(sum(breakdown.values()), 2), breakdown=breakdown, unscored=unscored)
