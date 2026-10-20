"""Compare our per-player weekly points with Sleeper's own `players_points` for a league."""

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

from fantasy_rag.analytics.scoring import ScoringRules, score

TOLERANCE = 0.1


@dataclass
class Mismatch:
    week: int
    player_id: str
    name: str
    position: str
    sleeper: float
    ours: float
    breakdown: dict[str, float]
    unscored: list[str]  # league scoring keys we cannot compute that may apply to this player


@dataclass
class CheckResult:
    league_pk: int
    weeks: list[int]
    compared: int = 0
    within: int = 0
    def_compared: int = 0
    def_within: int = 0
    skipped_unresolved: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)


def check_league(conn: Connection, league_pk: int) -> CheckResult:
    season, settings = conn.execute(
        text("SELECT season, scoring_settings FROM fantasy_leagues WHERE id = :id"),
        {"id": league_pk},
    ).one()
    rules = ScoringRules.from_sleeper(settings)
    # Only weeks nflverse has finished stats for.
    last = conn.execute(
        text("SELECT max(week) FROM player_week_stats WHERE season = :s AND season_type = 'REG'"),
        {"s": season},
    ).scalar_one()
    result = CheckResult(league_pk, list(range(1, (last or 0) + 1)))
    sleeper_to_gsis = {
        str(ext): str(pid)
        for ext, pid in conn.execute(
            text(
                "SELECT external_id, player_id FROM player_external_ids WHERE provider = 'sleeper'"
            )
        )
    }

    # One Sleeper value per (week, player): a player appears once per league-week.
    sleeper_points: dict[tuple[int, str], float] = {}
    def_points: dict[tuple[int, str], float] = {}  # team DEF IDs are team abbreviations
    for week, raw in conn.execute(
        text("SELECT week, raw FROM fantasy_rosters WHERE league_id = :l AND week <= :w"),
        {"l": league_pk, "w": last or 0},
    ):
        for sid, pts in ((raw or {}).get("players_points") or {}).items():
            if not sid.isdigit():
                def_points[(week, sid)] = float(pts)
            elif sid not in sleeper_to_gsis:
                result.skipped_unresolved += 1
            else:
                sleeper_points[(week, sleeper_to_gsis[sid])] = float(pts)

    stats = _stats(conn, season, sorted({pid for _, pid in sleeper_points}))
    defense = {
        (w, team): st
        for w, team, st in conn.execute(
            text(
                "SELECT week, team, stats FROM team_week_defense"
                " WHERE season = :s AND season_type = 'REG'"
            ),
            {"s": season},
        )
    }
    rows = [
        (week, pid, *stats["players"].get(pid, (pid, "")), stats["weeks"].get((week, pid), {}), pts)
        for (week, pid), pts in sleeper_points.items()
    ] + [
        (week, team, team, "DEF", defense.get((week, team), {}), pts)
        for (week, team), pts in def_points.items()
    ]
    for week, pid, name, position, week_stats, theirs in sorted(rows):
        s = score(week_stats, position, rules)
        ok = abs(s.points - theirs) <= TOLERANCE + 1e-9
        result.compared += 1
        result.within += ok
        if position == "DEF":
            result.def_compared += 1
            result.def_within += ok
        if not ok:
            result.mismatches.append(
                Mismatch(week, pid, name, position, theirs, s.points, s.breakdown, s.unscored)
            )
    return result


def _stats(conn: Connection, season: int, player_ids: list[str]) -> dict[str, Any]:
    rows = conn.execute(
        text(
            "SELECT week, player_id, stats FROM player_week_stats"
            " WHERE season = :s AND season_type = 'REG' AND player_id = ANY(:ids)"
        ),
        {"s": season, "ids": player_ids},
    )
    weeks = {(w, pid): st if isinstance(st, dict) else json.loads(st) for w, pid, st in rows}
    # Special-teams stats from play-by-play join the player's week (st_ff, st_fum_rec).
    for w, pid, st in conn.execute(
        text(
            "SELECT week, player_id, stats FROM player_week_special_teams"
            " WHERE season = :s AND season_type = 'REG' AND player_id = ANY(:ids)"
        ),
        {"s": season, "ids": player_ids},
    ):
        weeks[(w, pid)] = {**weeks.get((w, pid), {}), **st}
    players = {
        pid: (name, pos)
        for pid, name, pos in conn.execute(
            text("SELECT id, full_name, position FROM players WHERE id = ANY(:ids)"),
            {"ids": player_ids},
        )
    }
    return {"weeks": weeks, "players": players}
