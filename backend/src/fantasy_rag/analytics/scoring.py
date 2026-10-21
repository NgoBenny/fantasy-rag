"""League scoring rules -> fantasy points, from nflverse raw stats. No LLM, no I/O.

Sleeper scores a player as sum(stat[key] * weight[key]) over its scoring keys, so each
supported key maps to one nflverse-derived stat value. Keys we cannot compute are
reported per player, never estimated. Team defenses score from `ingest.pbp.team_defense`
stats, which already use Sleeper's key names.
"""

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

Stats = Mapping[str, Any]

# Team defense and IDP keys only score DEF / defensive players, so their absence does not
# make an offensive player's or kicker's total incomplete. Individual return yards
# (kr_yd, pr_yd) can score any player, so they stay visible when unsupported.
TEAM_DEF_OR_IDP = re.compile(
    r"^(def_|idp_|pts_allow|yds_allow|int$|int_ret|sack|ff$|fum_rec$|fum_ret|safe$|blk_kick"
    r"|tkl|qb_hit|fg_ret|bonus_def|bonus_sack|bonus_tkl)"
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
    # special teams (from play-by-play, merged into the player's weekly stats)
    "st_ff": _col("st_ff"),
    "st_fum_rec": _col("st_fum_rec"),
}
# Per-game threshold bonuses: not linear in the stats, so a point estimate can't score them.
THRESHOLD_KEYS = frozenset(k for k in DERIVE if k.startswith("bonus_") and k[-1].isdigit())


def _band(key: str, lo: float, hi: float | None) -> Callable[[Stats, str], float]:
    """1 when the stat lands in [lo, hi]; Sleeper's labels are inclusive ("pts_allow_7_13")."""

    def f(s: Stats, _pos: str) -> float:
        v = _n(s, key)
        return 1.0 if v >= lo and (hi is None or v <= hi) else 0.0

    return f


# Sleeper key -> stat for a team DEF (team_week_defense.stats). Bands checked against
# Sleeper's own pts_allow_* / yds_allow_* flags.
DEF_DERIVE: dict[str, Callable[[Stats, str], float]] = {
    **{
        k: _col(k)
        for k in (
            "sack", "int", "ff", "fum_rec", "safe", "blk_kick", "def_td", "def_st_td",
            "def_st_ff", "def_st_fum_rec", "def_2pt", "def_3_and_out", "def_4_and_stop",
            "def_pass_def", "tkl_loss", "pts_allow", "yds_allow",
        )
    },
    "pts_allow_0": _band("pts_allow", 0, 0),
    "pts_allow_1_6": _band("pts_allow", 1, 6),
    "pts_allow_7_13": _band("pts_allow", 7, 13),
    "pts_allow_14_20": _band("pts_allow", 14, 20),
    "pts_allow_21_27": _band("pts_allow", 21, 27),
    "pts_allow_28_34": _band("pts_allow", 28, 34),
    "pts_allow_35p": _band("pts_allow", 35, None),
    # shortcut: 0_100 vs 100_199 at exactly 100 yards is unseen in Sleeper's data; 100 -> 100_199
    "yds_allow_0_100": _band("yds_allow", float("-inf"), 99),
    "yds_allow_100_199": _band("yds_allow", 100, 199),
    "yds_allow_200_299": _band("yds_allow", 200, 299),
    "yds_allow_300_349": _band("yds_allow", 300, 349),
    "yds_allow_350_399": _band("yds_allow", 350, 399),
    "yds_allow_400_449": _band("yds_allow", 400, 449),
    "yds_allow_450_499": _band("yds_allow", 450, 499),
    "yds_allow_500_549": _band("yds_allow", 500, 549),
    "yds_allow_550p": _band("yds_allow", 550, None),
}  # fmt: skip


class ScoringRules(BaseModel):
    weights: dict[str, float]  # supported, non-zero
    unsupported: dict[str, float]  # non-zero keys we cannot compute

    @classmethod
    def from_sleeper(cls, scoring_settings: Mapping[str, Any]) -> "ScoringRules":
        nonzero = {k: float(v) for k, v in scoring_settings.items() if v}
        return cls(
            weights={k: v for k, v in nonzero.items() if k in DERIVE or k in DEF_DERIVE},
            unsupported={
                k: v for k, v in nonzero.items() if k not in DERIVE and k not in DEF_DERIVE
            },
        )

    def fingerprint(self) -> str:
        """Stable ID of the rules we score (same weights -> same ID, any league)."""
        body = json.dumps(sorted((k, round(v, 6)) for k, v in self.weights.items()))
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    def unscored_for(self, position: str) -> list[str]:
        """Unsupported keys that could change this position's total."""
        is_def = position in DEF_POSITIONS
        return sorted(k for k in self.unsupported if bool(TEAM_DEF_OR_IDP.match(k)) == is_def)


class Score(BaseModel):
    points: float
    breakdown: dict[str, float]
    unscored: list[str]  # non-empty means `points` may be incomplete for this player


def score(stats: Stats, position: str, rules: ScoringRules) -> Score:
    derive = DEF_DERIVE if position in DEF_POSITIONS else DERIVE
    breakdown: dict[str, float] = {}
    if derive is DEF_DERIVE and "pts_allow" not in stats:  # no game (bye): not a shutout
        return Score(points=0.0, breakdown=breakdown, unscored=rules.unscored_for(position))
    for key, weight in rules.weights.items():
        if key not in derive:  # a team-DEF key on a player, or the reverse
            continue
        value = derive[key](stats, position) * weight
        if value:
            breakdown[key] = round(value, 4)
    return Score(
        points=round(sum(breakdown.values()), 2),
        breakdown=breakdown,
        unscored=rules.unscored_for(position),
    )
