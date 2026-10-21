"""`fantasy-rag usage-check`: reconcile play-by-play usage with nflverse player stats.

Red-zone looks come from our own play-by-play counts, so before any of them is shown the
same counting rules must reproduce nflverse's all-field targets and carries. Reported per
stat, position and season, counting only player-weeks where either source is non-zero (so
zero-target QB weeks can't inflate the rate). A player-week whose game has play-by-play
but no usage row is a known zero; one whose game has none is reported as uncovered.

Pass bars (docs/PLAN.md, Phase 10; frozen before the first run): >= 99% exact for WR/TE
targets and RB carries, >= 97% for every other cell with rows. Also reports how many
team-games have an exact snap denominator (a player with offense_pct = 1.0) and how often
the estimate agrees with it.
"""

from collections import defaultdict
from statistics import median
from typing import Any

from sqlalchemy import Connection, text

POSITIONS = ("QB", "RB", "WR", "TE")
STATS = {"targets": "targets_pbp", "carries": "carries_pbp"}
STRICT = {("targets", "WR"), ("targets", "TE"), ("carries", "RB")}
BAR_STRICT, BAR = 99.0, 97.0


def bar(stat: str, position: str) -> float:
    return BAR_STRICT if (stat, position) in STRICT else BAR


def reconcile(conn: Connection, seasons: list[int]) -> dict[str, Any]:
    rows = conn.execute(
        text(
            """
            SELECT s.season, s.week, s.player_id, p.position, s.game_id,
                   (s.stats->>'targets')::float AS targets,
                   (s.stats->>'carries')::float AS carries,
                   (u.stats->>'targets_pbp')::float AS targets_pbp,
                   (u.stats->>'carries_pbp')::float AS carries_pbp,
                   g.game_id IS NOT NULL AS covered
            FROM player_week_stats s
            JOIN players p ON p.id = s.player_id
            LEFT JOIN player_week_pbp_usage u ON u.player_id = s.player_id
                 AND u.season = s.season AND u.week = s.week AND u.season_type = 'REG'
            LEFT JOIN pbp_games g ON g.game_id = s.game_id AND g.season_type = 'REG'
            WHERE s.season_type = 'REG' AND s.season = ANY(:s) AND p.position = ANY(:p)
            """
        ),
        {"s": seasons, "p": list(POSITIONS)},
    ).all()
    cells: dict[tuple[str, str, int], dict[str, Any]] = {}
    worst: list[dict[str, Any]] = []
    uncovered = 0
    for r in rows:
        if not r.covered:
            uncovered += 1
            continue
        for stat, col in STATS.items():
            ref, got = int(getattr(r, stat) or 0), int(getattr(r, col) or 0)  # covered: no row = 0
            if ref == 0 and got == 0:
                continue
            c = cells.setdefault(
                (stat, r.position, r.season), {"rows": 0, "exact": 0, "pbp_more": 0, "pbp_less": 0}
            )
            c["rows"] += 1
            c["exact"] += ref == got
            c["pbp_more"] += got > ref
            c["pbp_less"] += got < ref
            if ref != got:
                worst.append({"stat": stat, "player_id": r.player_id, "season": r.season,
                              "week": r.week, "game_id": r.game_id, "nflverse": ref,
                              "pbp": got})  # fmt: skip
    only_pbp = conn.execute(
        text(
            """
            SELECT count(*) FROM player_week_pbp_usage u
            JOIN players p ON p.id = u.player_id
            WHERE u.season_type = 'REG' AND u.season = ANY(:s) AND p.position = ANY(:p)
              AND NOT EXISTS (SELECT 1 FROM player_week_stats s WHERE s.player_id = u.player_id
                    AND s.season = u.season AND s.week = u.week AND s.season_type = 'REG')
            """
        ),
        {"s": seasons, "p": list(POSITIONS)},
    ).scalar_one()
    unresolved = conn.execute(
        text("SELECT count(*) FROM unresolved_players WHERE source = 'nflverse.pbp_usage'")
    ).scalar_one()
    table = []
    failed = []
    for (stat, pos, season), c in sorted(cells.items()):
        rate = round(100 * c["exact"] / c["rows"], 2)
        ok = rate >= bar(stat, pos)
        table.append({"stat": stat, "position": pos, "season": season, **c,
                      "exact_pct": rate, "bar": bar(stat, pos), "ok": ok})  # fmt: skip
        if not ok:
            failed.append(f"{stat} {pos} {season}: {rate}% < {bar(stat, pos)}%")
    worst.sort(key=lambda w: -abs(w["nflverse"] - w["pbp"]))
    return {
        "seasons": seasons,
        "cells": table,
        "failed": failed,
        "uncovered_player_weeks": uncovered,
        "pbp_rows_without_stats": only_pbp,
        "unresolved_pbp_ids": unresolved,
        "largest_differences": worst[:25],
        "snap_denominators": snap_denominators(conn, seasons),
    }


def team_snaps(rows: list[tuple[int | None, float | None]]) -> tuple[int | None, bool]:
    """One team-game's offensive snaps from its players' (offense_snaps, offense_pct) rows:
    exact when a player was on the field for every snap (pct 1.0), else the median of
    round(snaps / pct) over players with pct >= 0.5 (an estimate: pct is rounded)."""
    full = [s for s, p in rows if s is not None and p == 1.0]
    if full:
        return max(full), True
    est = [round(s / p) for s, p in rows if s is not None and p is not None and p >= 0.5]
    return (round(median(est)) if est else None), False


def snap_denominators(conn: Connection, seasons: list[int]) -> dict[str, Any]:
    games: dict[tuple[str, str], list[tuple[int | None, float | None]]] = defaultdict(list)
    for r in conn.execute(
        text(
            "SELECT game_id, team, offense_snaps, offense_pct FROM snap_counts"
            " WHERE game_type = 'REG' AND season = ANY(:s) AND offense_snaps > 0"
        ),
        {"s": seasons},
    ):
        games[(r.game_id, r.team)].append((r.offense_snaps, r.offense_pct))
    exact = agree = checked = 0
    for rows in games.values():
        value, is_exact = team_snaps(rows)
        if not is_exact:
            continue
        exact += 1
        est, _ = team_snaps([(s, p) for s, p in rows if p != 1.0])
        if est is not None:
            checked += 1
            agree += est == value
    return {
        "team_games": len(games),
        "exact": exact,
        "estimate_checked": checked,
        "estimate_agrees_pct": round(100 * agree / checked, 2) if checked else None,
    }
