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
    skipped_def: int = 0
    skipped_unresolved: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)


def check_league(conn: Connection, league_pk: int) -> CheckResult:
    season, rules_json = conn.execute(
        text("SELECT season, scoring_rules FROM fantasy_leagues WHERE id = :id"), {"id": league_pk}
    ).one()
    rules = ScoringRules.model_validate(rules_json)
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
    for week, raw in conn.execute(
        text("SELECT week, raw FROM fantasy_rosters WHERE league_id = :l AND week <= :w"),
        {"l": league_pk, "w": last or 0},
    ):
        for sid, pts in ((raw or {}).get("players_points") or {}).items():
            if not sid.isdigit():
                result.skipped_def += 1
            elif sid not in sleeper_to_gsis:
                result.skipped_unresolved += 1
            else:
                sleeper_points[(week, sleeper_to_gsis[sid])] = float(pts)

    stats = _stats(conn, season, sorted({pid for _, pid in sleeper_points}))
    for (week, pid), theirs in sorted(sleeper_points.items()):
        name, position = stats["players"].get(pid, (pid, ""))
        s = score(stats["weeks"].get((week, pid), {}), position, rules)
        result.compared += 1
        if abs(s.points - theirs) <= TOLERANCE + 1e-9:
            result.within += 1
        else:
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
    players = {
        pid: (name, pos)
        for pid, name, pos in conn.execute(
            text("SELECT id, full_name, position FROM players WHERE id = ANY(:ids)"),
            {"ids": player_ids},
        )
    }
    return {"weeks": weeks, "players": players}
