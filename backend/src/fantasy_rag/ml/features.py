"""Point-in-time features for trained projections (rules: docs/phase8_plan.md).

Every prediction for (season, week) is made at that week's cutoff: Tuesday 12:00
America/New_York before the week's first game. A game counts (as a feature input, or as
a training label) only if it kicked off at least LAG before the cutoff; postponed games
are handled by that rule, not by week numbers. Pure functions over plain rows, so the
leakage tests can perturb data after a cutoff and check nothing before it moves.
"""

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from fantasy_rag.analytics.scoring import ScoringRules, score
from fantasy_rag.ml.baseline import HALF_LIFE, ewm

ET = ZoneInfo("America/New_York")
LAG = timedelta(hours=12)  # a game's stats count only if it kicked off this long before
POSITIONS = ("QB", "RB", "WR", "TE")
# Trained per position; every other component is projected by the player's weighted average.
MODELED = {
    "QB": ["passing_yards", "passing_tds", "passing_interceptions", "rushing_yards",
           "rushing_tds", "fumbles_lost_total"],
    "RB": ["rushing_yards", "rushing_tds", "receptions", "receiving_yards", "receiving_tds",
           "fumbles_lost_total"],
    "WR": ["receptions", "receiving_yards", "receiving_tds", "rushing_yards",
           "fumbles_lost_total"],
    "TE": ["receptions", "receiving_yards", "receiving_tds", "fumbles_lost_total"],
}  # fmt: skip
# Everything the ppr/half/std presets score for these positions.
COMPONENTS = [
    "passing_yards", "passing_tds", "passing_interceptions", "passing_2pt_conversions",
    "rushing_yards", "rushing_tds", "rushing_2pt_conversions", "receptions",
    "receiving_yards", "receiving_tds", "receiving_2pt_conversions", "fumbles_lost_total",
    "fumble_recovery_tds", "special_teams_tds", "targets", "carries", "attempts",
]  # fmt: skip


@dataclass(frozen=True)
class Game:
    """One player's game: the label source and a feature input."""

    player_id: str
    position: str
    season: int
    week: int
    game_id: str
    team: str
    opponent: str
    kickoff: datetime
    stats: dict[str, Any]
    snap_pct: float | None = None


@dataclass
class Row:
    """Features for (player, season, week) as of that week's cutoff."""

    player_id: str
    position: str
    season: int
    week: int
    cutoff: datetime
    features: dict[str, float | None]
    baseline: dict[str, float | None]  # format -> baseline projection (the fallback)
    games_used: int
    latest_input: datetime | None  # newest game kickoff the features read
    label: Game | None = None  # the game played that week, if any
    extra: dict[str, Any] = field(default_factory=dict)


def cutoffs(schedule: Iterable[dict[str, Any]]) -> dict[tuple[int, int], datetime]:
    """(season, week) -> the Tuesday 12:00 ET before the week's first regular-season kickoff."""
    first: dict[tuple[int, int], datetime] = {}
    for g in schedule:
        if g["game_type"] == "REG" and g["kickoff_at"] is not None:
            key = (g["season"], g["week"])
            first[key] = min(first.get(key, g["kickoff_at"]), g["kickoff_at"])
    out = {}
    for key, kick in first.items():
        local = kick.astimezone(ET)
        day = local.date() - timedelta(days=(local.weekday() - 1) % 7)  # Tuesday on/before
        cut = datetime.combine(day, time(12), ET).astimezone(UTC)
        out[key] = cut if cut < kick else cut - timedelta(days=7)
    return out


def usable(kickoff: datetime, cutoff: datetime) -> bool:
    return kickoff <= cutoff - LAG


def _points(stats: dict[str, Any], position: str, rules: ScoringRules) -> float:
    return score(stats, position, rules).points


def _num(stats: dict[str, Any], key: str) -> float:
    v = stats.get(key)
    return float(v) if isinstance(v, int | float) else 0.0


class History:
    """All games, indexed so any cutoff's usable prefix is a binary search away."""

    def __init__(self, games: Iterable[Game], formats: dict[str, ScoringRules]) -> None:
        self.formats = formats
        by_player: dict[str, list[Game]] = defaultdict(list)
        team_tot: dict[tuple[str, str], dict[str, float]] = defaultdict(
            lambda: {"targets": 0.0, "carries": 0.0}
        )
        allowed: dict[tuple[str, str, str], float] = defaultdict(float)
        kick_of: dict[str, tuple[datetime, int]] = {}
        for g in games:
            by_player[g.player_id].append(g)
            for k in ("targets", "carries"):
                team_tot[(g.team, g.game_id)][k] += _num(g.stats, k)
            if g.position in POSITIONS:
                allowed[(g.opponent, g.game_id, g.position)] += _points(
                    g.stats, g.position, formats["ppr"]
                )
                kick_of[g.game_id] = (g.kickoff, g.season)
        self.players = {p: sorted(gs, key=lambda g: g.kickoff) for p, gs in by_player.items()}
        self.kicks = {p: [g.kickoff for g in gs] for p, gs in self.players.items()}
        self.team_tot = team_tot
        # (defense, position) -> [(kickoff, season, PPR points allowed)] in time order
        per_def: dict[tuple[str, str], list[tuple[datetime, int, float]]] = defaultdict(list)
        for (d, gid, pos), pts in allowed.items():
            kick, season = kick_of[gid]
            per_def[(d, pos)].append((kick, season, pts))
        self.allowed = {k: sorted(v) for k, v in per_def.items()}
        self.points = {
            p: [{f: _points(g.stats, g.position, r) for f, r in formats.items()} for g in gs]
            for p, gs in self.players.items()
        }

    def prefix(self, player_id: str, cutoff: datetime) -> int:
        """How many of the player's games are usable at the cutoff."""
        return bisect_right(self.kicks.get(player_id, []), cutoff - LAG)

    def opp_allowed(
        self, defense: str, position: str, season: int, cutoff: datetime
    ) -> float | None:
        games = [(k, s, p) for k, s, p in self.allowed.get((defense, position), [])
                 if usable(k, cutoff) and s in (season - 1, season)]  # fmt: skip
        this = [p for _, s, p in games if s == season]
        pool = this if len(this) >= 3 else [p for *_, p in games]
        return round(mean(pool), 3) if pool else None


def _stats_window(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return ewm(values, HALF_LIFE), mean(values[-3:])


def build_row(
    h: History,
    player_id: str,
    position: str,
    season: int,
    week: int,
    cutoff: datetime,
    upcoming: dict[str, Any] | None,
) -> Row:
    """Features from the player's usable games in this and last season (like the baseline).
    `upcoming`: the scheduled game for his latest team that week ({"opponent", "home"})."""
    n = h.prefix(player_id, cutoff)
    games = [g for g in h.players.get(player_id, [])[:n] if g.season >= season - 1]
    pts = h.points.get(player_id, [])[:n][-len(games) :] if games else []
    f: dict[str, float | None] = {}
    for c in COMPONENTS:
        vals = [_num(g.stats, c) for g in games]
        f[f"{c}_ewm"], f[f"{c}_last3"] = _stats_window(vals)
        this = [v for v, g in zip(vals, games, strict=True) if g.season == season]
        f[f"{c}_season"] = mean(this) if this else None
    snaps = [g.snap_pct for g in games if g.snap_pct is not None]
    f["snap_ewm"], f["snap_last3"] = _stats_window(snaps)
    for k in ("targets", "carries"):
        shares = [_num(g.stats, k) / t for g in games
                  if (t := h.team_tot[(g.team, g.game_id)][k]) > 0]  # fmt: skip
        f[f"{k}_share_ewm"], _ = _stats_window(shares)
    f["games_season"] = float(sum(g.season == season for g in games))
    f["games_recent"] = float(len(games))
    baseline = {fmt: round(ewm([p[fmt] for p in pts], HALF_LIFE), 2) if pts else None
                for fmt in h.formats}  # fmt: skip
    f["baseline_ppr"] = baseline.get("ppr")
    f["ppg_ppr"] = mean(p["ppr"] for p in pts) if pts else None  # picks start/sit pairs
    f["opp_allowed"] = (
        h.opp_allowed(upcoming["opponent"], position, season, cutoff) if upcoming else None
    )
    f["home"] = float(upcoming["home"]) if upcoming else None
    return Row(player_id, position, season, week, cutoff, f, baseline, len(games),
               games[-1].kickoff if games else None)  # fmt: skip


def team_week_games(
    schedule: Iterable[dict[str, Any]],
) -> dict[tuple[int, int, str], dict[str, Any]]:
    """(season, week, team) -> {"opponent", "home"} for regular-season games."""
    out = {}
    for g in schedule:
        if g["game_type"] != "REG":
            continue
        out[(g["season"], g["week"], g["home_team"])] = {"opponent": g["away_team"], "home": 1}
        out[(g["season"], g["week"], g["away_team"])] = {"opponent": g["home_team"], "home": 0}
    return out


def rows_for_labels(
    h: History,
    schedule: list[dict[str, Any]],
    seasons: Iterable[int],
) -> list[Row]:
    """One row per QB/RB/WR/TE game played in `seasons`, featurized at its week's cutoff.
    The upcoming opponent comes from the schedule for the player's latest usable team
    (what was knowable at the cutoff), not from the game he ended up playing."""
    cut = cutoffs(schedule)
    games_of = team_week_games(schedule)
    wanted = set(seasons)
    out = []
    for pid, games in h.players.items():
        for g in games:
            if g.season not in wanted or g.position not in POSITIONS:
                continue
            c = cut[(g.season, g.week)]
            n = h.prefix(pid, c)
            team = games[n - 1].team if n else g.team
            row = build_row(h, pid, g.position, g.season, g.week, c,
                            games_of.get((g.season, g.week, team)))  # fmt: skip
            row.label = g
            out.append(row)
    return out
