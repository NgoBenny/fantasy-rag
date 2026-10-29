"""Usage context: snap share, target and carry share, red-zone looks, and usage facts.

Definitions (also shown in the app and the get_usage tool description):
- Game share: the player's targets (carries) / his team's targets (carries) that game,
  team totals summed from nflverse player stats. Snap % is nflverse offense_pct.
- Window share ("last 3"): total player opportunities / total team opportunities over
  those games, not an average of game shares. Window snap % = snaps / team snaps, where a
  team-game's snaps are exact when some player played every snap, else an estimate
  (analytics.usage_check.team_snaps) and the window share is marked approximate.
- Red-zone looks = red-zone targets + red-zone carries (inside the opponent's 20, from
  play-by-play); goal-line carries (inside the 5) are a subset, never added. A game whose
  play-by-play wasn't loaded has unknown looks (None), never 0.
- Shares are percentages rounded to 1 decimal; changes are in percentage points.

Facts are fixed-template observations over completed games, newest last. They never say
why something happened, and with no rule firing there are none. Count rules (targets,
carries, red-zone looks) need a value of at least 3 to fire, so 0-1-2 isn't a trend.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text

from fantasy_rag.analytics.usage_check import team_snaps

WINDOW = 3  # games in "last 3"
SHIFT_PTS = 10.0  # percentage points between the last 3 games and the 3 before
RED_ZONE_LOOKS = 3  # over the last 3 games
GAME_OVER = timedelta(hours=4)  # a game is final this long after kickoff
MAX_FACTS = 3


def pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def completed_through(conn: Connection, season: int, now: datetime) -> int | None:
    """The latest week of `season` whose every regular-season game kicked off at least
    GAME_OVER ago and that has stats: Thursday's game alone never completes a week."""
    found: int | None = conn.execute(
        text(
            """
            SELECT max(week) FROM (
              SELECT week FROM schedules WHERE season = :s AND game_type = 'REG'
              GROUP BY week HAVING bool_and(kickoff_at IS NOT NULL AND kickoff_at <= :t)
            ) done
            WHERE EXISTS (SELECT 1 FROM player_week_stats p WHERE p.season = :s
                          AND p.week = done.week AND p.season_type = 'REG')
            """
        ),
        {"s": season, "t": now - GAME_OVER},
    ).scalar()
    return found


def usage_for(
    conn: Connection, player_ids: list[str], season: int, through_week: int
) -> dict[str, list[dict[str, Any]]]:
    """Every regular-season game each player played in `season` through `through_week`,
    oldest first. One query per source for the whole list (no per-player queries)."""
    if not player_ids:
        return {}
    params = {"ids": player_ids, "s": season, "w": through_week}
    games: dict[tuple[str, int], dict[str, Any]] = {}
    for r in conn.execute(
        text(
            "SELECT player_id, week, game_id, team, opponent,"
            " coalesce((stats->>'targets')::float, 0) AS targets,"
            " coalesce((stats->>'carries')::float, 0) AS carries"
            " FROM player_week_stats WHERE player_id = ANY(:ids) AND season = :s"
            " AND season_type = 'REG' AND week <= :w"
        ),
        params,
    ):
        games[(r.player_id, r.week)] = {"week": r.week, "game_id": r.game_id, "team": r.team,
                                        "opponent": r.opponent, "targets": int(r.targets),
                                        "carries": int(r.carries), "snaps": None}  # fmt: skip
    for r in conn.execute(
        text(
            "SELECT player_id, week, game_id, team, offense_snaps, offense_pct FROM snap_counts"
            " WHERE player_id = ANY(:ids) AND season = :s AND game_type = 'REG'"
            " AND week <= :w AND offense_snaps > 0"
        ),
        params,
    ):
        # Played (on the field for offense) but no stat line: 0 targets and carries.
        g = games.setdefault(
            (r.player_id, r.week),
            {"week": r.week, "game_id": r.game_id, "team": r.team, "opponent": None,
             "targets": 0, "carries": 0},
        )  # fmt: skip
        g["snaps"], g["offense_pct"] = r.offense_snaps, r.offense_pct
    team_games = {(g["team"], g["game_id"]) for g in games.values()}
    teams = sorted({t for t, _ in team_games if t})
    totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in conn.execute(
        text(
            "SELECT team, game_id, sum(coalesce((stats->>'targets')::float, 0)) AS tgt,"
            " sum(coalesce((stats->>'carries')::float, 0)) AS car FROM player_week_stats"
            " WHERE season = :s AND season_type = 'REG' AND team = ANY(:teams) AND week <= :w"
            " GROUP BY team, game_id"
        ),
        {"s": season, "teams": teams, "w": through_week},
    ):
        totals[(r.team, r.game_id)] = [r.tgt, r.car]
    snap_rows: dict[tuple[str, str], list[tuple[int | None, float | None]]] = defaultdict(list)
    for r in conn.execute(
        text(
            "SELECT team, game_id, offense_snaps, offense_pct FROM snap_counts"
            " WHERE season = :s AND game_type = 'REG' AND team = ANY(:teams) AND week <= :w"
            " AND offense_snaps > 0"
        ),
        {"s": season, "teams": teams, "w": through_week},
    ):
        snap_rows[(r.team, r.game_id)].append((r.offense_snaps, r.offense_pct))
    covered = set(
        conn.execute(
            text(
                "SELECT game_id FROM pbp_games WHERE season = :s AND season_type = 'REG'"
                " AND week <= :w"
            ),
            params,
        ).scalars()
    )
    rz: dict[tuple[str, int], dict[str, int]] = {}
    for r in conn.execute(
        text(
            "SELECT player_id, week, stats FROM player_week_pbp_usage WHERE player_id = ANY(:ids)"
            " AND season = :s AND season_type = 'REG' AND week <= :w"
        ),
        params,
    ):
        rz[(r.player_id, r.week)] = r.stats
    out: dict[str, list[dict[str, Any]]] = {p: [] for p in player_ids}
    for (pid, week), g in sorted(games.items(), key=lambda kv: kv[0][1]):
        tt, tc = totals.get((g["team"], g["game_id"]), [0.0, 0.0])
        ts, exact = team_snaps(snap_rows.get((g["team"], g["game_id"]), []))
        u = rz.get((pid, week))
        known = g["game_id"] in covered
        rz_t = (u or {}).get("rz_targets", 0) if known else None
        rz_c = (u or {}).get("rz_carries", 0) if known else None
        out[pid].append(
            {
                "season": season,
                "week": week,
                "team": g["team"],
                "opponent": g["opponent"],
                "snaps": g["snaps"],
                "team_snaps": ts,
                "team_snaps_exact": exact,
                # the game's own share is nflverse's; team snaps serve window totals
                "snap_pct": round(100 * g["offense_pct"], 1) if g.get("offense_pct") else None,
                "targets": g["targets"],
                "team_targets": int(tt),
                "target_share_pct": pct(g["targets"], tt),
                "carries": g["carries"],
                "team_carries": int(tc),
                "carry_share_pct": pct(g["carries"], tc),
                "rz_targets": rz_t,
                "rz_carries": rz_c,
                "rz_looks": None if rz_t is None or rz_c is None else rz_t + rz_c,
                "gl_carries": (u or {}).get("gl_carries", 0) if known else None,
                "rz_unknown": (u or {}).get("rz_unknown", 0) if known else None,
            }
        )
    return out


def window(games: list[dict[str, Any]], n: int = WINDOW) -> dict[str, Any] | None:
    """Totals-based shares over the last `n` games (None with no games). A share needs every
    game in the window: one game without snap data makes the window's snap share None
    (counted in snap_missing), never a share over fewer games."""
    last = games[-n:]
    if not last:
        return None
    missing = [g for g in last if g["snaps"] is None or not g["team_snaps"]]

    def pbp(key: str) -> int | None:  # one game without play-by-play: unknown, never partial
        return None if any(g[key] is None for g in last) else sum(g[key] for g in last)

    return {
        "games": len(last),
        "weeks": [g["week"] for g in last],
        "snap_pct": None
        if missing
        else pct(sum(g["snaps"] for g in last), sum(g["team_snaps"] for g in last)),
        "snap_missing": len(missing),
        "snap_pct_approx": any(not g["team_snaps_exact"] for g in last),
        "targets": sum(g["targets"] for g in last),
        "target_share_pct": pct(sum(g["targets"] for g in last),
                                sum(g["team_targets"] for g in last)),
        "carries": sum(g["carries"] for g in last),
        "carry_share_pct": pct(sum(g["carries"] for g in last),
                               sum(g["team_carries"] for g in last)),
        "rz_looks": pbp("rz_looks"),
        "rz_targets": pbp("rz_targets"),
        "rz_carries": pbp("rz_carries"),
        "gl_carries": pbp("gl_carries"),
        "rz_unknown": pbp("rz_unknown"),
    }  # fmt: skip


def team_weeks(conn: Connection, season: int, through_week: int) -> dict[str, set[int]]:
    """Weeks each team has a regular-season game in the schedule (for bye labels)."""
    out: dict[str, set[int]] = defaultdict(set)
    for r in conn.execute(
        text(
            "SELECT week, home_team, away_team FROM schedules WHERE season = :s"
            " AND game_type = 'REG' AND week <= :w"
        ),
        {"s": season, "w": through_week},
    ):
        out[r.home_team].add(r.week)
        out[r.away_team].add(r.week)
    return out


def window_meta(
    games: list[dict[str, Any]],
    shown: list[dict[str, Any]],
    schedule: dict[str, set[int]],
    through_week: int,
) -> dict[str, Any]:
    """What a shown window leaves out, stated rather than left to guess: the season's
    recorded games vs those shown (the rest are outside the requested window), and every
    week inside the window without a recorded game. A week is a bye only when the
    player's one team this season has no scheduled game that week; a week his team played
    is "no recorded appearance"; with no single known team it is "unknown"."""
    teams = {g["team"] for g in games if g.get("team")}
    team = next(iter(teams)) if len(teams) == 1 else None
    start = shown[0]["week"] if shown else 1
    played = {g["week"] for g in games}
    without = []
    for week in range(start, through_week + 1):
        if week in played:
            continue
        if team is None:
            status = "unknown"
        elif week not in schedule.get(team, set()):
            status = "bye"
        else:
            status = "no recorded appearance"
        without.append({"week": week, "status": status})
    return {
        "season_games_recorded": len(games),
        "games_shown": len(shown),
        "games_outside_requested_window": len(games) - len(shown),
        "window_weeks": [start, through_week],
        "weeks_without_a_game": without,
    }


def _weeks(ws: Iterable[int]) -> str:
    return ", ".join(str(w) for w in ws)


METRICS = {"targets": "Targets", "carries": "Carries", "snap_pct": "Snap share"}


def _fmt(metric: str, v: float) -> str:
    return f"{v:.1f}%" if metric == "snap_pct" else f"{v:g}"


def facts(games: list[dict[str, Any]], position: str | None) -> list[dict[str, Any]]:
    """At most MAX_FACTS observations about the latest completed games, by priority:
    shift > rising/falling > red zone > season high. Empty when nothing fires."""
    if len(games) < WINDOW:
        return []
    found: list[dict[str, Any]] = []
    last3 = games[-WINDOW:]
    weeks = [g["week"] for g in last3]

    # shift: last 3 games vs the 3 before, totals-based shares, >= SHIFT_PTS points
    if len(games) >= 2 * WINDOW:
        now, before = window(games), window(games[:-WINDOW])
        assert now is not None and before is not None
        share = {"RB": "carry_share_pct", "WR": "target_share_pct", "TE": "target_share_pct"}
        for key, label in [("snap_pct", "Snap share"),
                           (share.get(position or ""), None)]:  # fmt: skip
            if not key:
                continue
            a, b = before[key], now[key]
            if a is None or b is None or abs(b - a) < SHIFT_PTS:
                continue
            label = label or ("Carry share" if key == "carry_share_pct" else "Target share")
            estimated = now["snap_pct_approx"] or before["snap_pct_approx"]
            approx = "≈" if key == "snap_pct" and estimated else ""
            diff = round(b - a, 1)
            found.append({
                "kind": "shift",
                "text": f"{label} {approx}{a:.1f}% → {approx}{b:.1f}% ({diff:+.1f} pts):"
                        f" last 3 games (weeks {_weeks(now['weeks'])}) vs the 3 before"
                        f" (weeks {_weeks(before['weeks'])})",
                "values": {"before": a, "after": b, "change_pts": diff, "games": WINDOW},
                "weeks": before["weeks"] + now["weeks"],
            })  # fmt: skip

    # rising / falling: strictly up (down) from each game to the next over the last 3
    for key, label in METRICS.items():
        vals = [g[key] for g in last3]
        if any(v is None for v in vals):
            continue
        up = vals[0] < vals[1] < vals[2]
        down = vals[0] > vals[1] > vals[2]
        if not (up or down) or (key != "snap_pct" and max(vals) < 3):  # ignore 0-1-2 noise
            continue
        word = "rose" if up else "fell"
        shown = ", ".join(_fmt(key, v) for v in vals)
        found.append({
            "kind": "rising" if up else "falling",
            "text": f"{label} {word} in each of the last 3 games: {shown} (weeks {_weeks(weeks)})",
            "values": {"values": vals, "games": WINDOW},
            "weeks": weeks,
        })  # fmt: skip

    # red zone: complete counts only (every game covered, no unknown yard lines)
    w = window(games)
    assert w is not None
    if w["rz_looks"] is not None and w["rz_unknown"] == 0 and w["rz_looks"] >= RED_ZONE_LOOKS:
        t = sum(g["rz_targets"] for g in last3)
        c = sum(g["rz_carries"] for g in last3)
        found.append({
            "kind": "red_zone",
            "text": f"{w['rz_looks']} red-zone looks in the last 3 games ({t} targets, {c}"
                    f" carries; weeks {_weeks(weeks)})",
            "values": {"looks": w["rz_looks"], "targets": t, "carries": c, "games": WINDOW},
            "weeks": weeks,
        })  # fmt: skip

    # season high: the latest game strictly above every earlier game this season
    if len(games) >= 4:
        latest, earlier = games[-1], games[:-1]
        for key, label in [*METRICS.items(), ("rz_looks", "Red-zone looks")]:
            if key == "rz_looks" and any(g["rz_looks"] is None or g["rz_unknown"]
                                         for g in games):  # fmt: skip
                continue
            vals = [g[key] for g in earlier]
            if any(v is None for v in vals):  # an unknown earlier game could be the high
                continue
            if latest[key] is None or latest[key] <= max(vals) or latest[key] < 3:
                continue
            found.append({
                "kind": "season_high",
                "text": f"Season high: {_fmt(key, latest[key])} {label.lower()} in week"
                        f" {latest['week']} (previous high {_fmt(key, max(vals))})",
                "values": {"value": latest[key], "previous_high": max(vals)},
                "weeks": [latest["week"]],
            })  # fmt: skip
    return found[:MAX_FACTS]


def as_of(conn: Connection, season: int, through_week: int | None) -> dict[str, Any]:
    rows: dict[str, datetime] = dict(
        conn.execute(
            text(
                "SELECT source, max(finished_at) FROM sync_runs WHERE status = 'ok'"
                " AND source IN ('nflverse.player_stats', 'nflverse.snap_counts',"
                " 'nflverse.pbp_usage') GROUP BY source"
            )
        ).all()
    )
    parts = {
        "stats_updated_at": rows.get("nflverse.player_stats"),
        "snaps_updated_at": rows.get("nflverse.snap_counts"),
        "pbp_updated_at": rows.get("nflverse.pbp_usage"),
    }
    known = [t for t in parts.values() if t is not None]
    return {
        "season": season,
        "through_week": through_week,
        **parts,
        # Usage is only as fresh as its oldest input; None if any input never loaded.
        "updated_at": min(known) if len(known) == len(parts) else None,
    }
