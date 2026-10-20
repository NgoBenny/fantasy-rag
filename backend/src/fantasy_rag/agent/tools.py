"""Typed tools the router may call. Inputs are validated by Pydantic; no tool takes SQL.

Every tool is a thin wrapper over analytics / ml functions and works without an LLM.
Results are plain JSON-able dicts that state season, weeks, scoring and data freshness.
"""

from collections.abc import Callable
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Connection

from fantasy_rag.analytics import queries as q
from fantasy_rag.analytics.scoring import score
from fantasy_rag.ml import baseline

Scoring = Literal["ppr", "half", "std"]
Position = Literal["QB", "RB", "WR", "TE", "K"]
SHOWN_STATS = [
    "completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions",
    "carries", "rushing_yards", "rushing_tds", "targets", "receptions", "receiving_yards",
    "receiving_tds", "fg_made", "fg_att", "pat_made",
]  # fmt: skip


class PlayerName(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    position: Position | None = None
    team: str | None = Field(default=None, min_length=2, max_length=3)


class ScoringChoice(BaseModel):
    league_id: int | None = Field(default=None, description="imported league; else `scoring`")
    scoring: Scoring = "ppr"


class WeekRange(BaseModel):
    season: int | None = Field(default=None, ge=2022, le=2030)
    start_week: int | None = Field(default=None, ge=1, le=18)
    end_week: int | None = Field(default=None, ge=1, le=18)
    last_n: int | None = Field(default=None, ge=1, le=18, description="most recent N weeks")

    @model_validator(mode="after")
    def _one_window(self) -> "WeekRange":
        if self.last_n and (self.start_week or self.end_week):
            raise ValueError("use last_n or start_week/end_week, not both")
        return self


class GetPlayerStats(ScoringChoice, WeekRange):
    player: PlayerName


class ComparePlayers(ScoringChoice, WeekRange):
    players: list[PlayerName] = Field(min_length=2, max_length=4)


class DefenseVsPosition(ScoringChoice):
    team: str = Field(min_length=2, max_length=3)
    position: Literal["QB", "RB", "WR", "TE"]
    last_n: int = Field(default=4, ge=1, le=18)


class GetInjuryStatus(BaseModel):
    player: PlayerName


class GetTrending(BaseModel):
    kind: Literal["add", "drop"] = "add"
    limit: int = Field(default=10, ge=1, le=25)


class ProjectPlayer(ScoringChoice):
    player: PlayerName


class CompareStartSit(ScoringChoice):
    players: list[PlayerName] = Field(min_length=2, max_length=4)


class RankWaivers(BaseModel):
    league_id: int
    position: Position | None = None
    limit: int = Field(default=10, ge=1, le=25)


# ---------- implementations ----------


def _window(conn: Connection, w: WeekRange) -> tuple[int, int, int]:
    current, last = q.latest_week(conn)
    season = w.season or current
    if season != current:
        last = 18  # a finished season
    if w.last_n:
        return season, max(1, last - w.last_n + 1), last
    return season, w.start_week or 1, w.end_week or last


def _player_block(conn: Connection, ref: q.PlayerRef, args: Any) -> dict[str, Any]:
    season, w0, w1 = _window(conn, args)
    rules, label = q.rules_for(conn, args.league_id, args.scoring)
    rows = q.weekly_rows(conn, [ref.id], [season], (w0, w1))
    weeks = []
    for r in rows:
        s = score(r["stats"], ref.position or "", rules)
        weeks.append(
            {
                "week": r["week"],
                "opponent": r["opponent"],
                "points": s.points,
                **{k: r["stats"].get(k) for k in SHOWN_STATS if r["stats"].get(k)},
            }
        )
    totals = {k: sum(w.get(k) or 0 for w in weeks) for k in SHOWN_STATS}
    totals = {k: v for k, v in totals.items() if v}
    unscored = rules.unscored_for(ref.position or "") if weeks else []
    return {
        "player": ref.model_dump(),
        "season": season,
        "weeks": [w0, w1],
        "scoring": label,
        "games": len(weeks),
        "total_points": round(sum(w["points"] for w in weeks), 2),
        "points_per_game": round(mean(w["points"] for w in weeks), 2) if weeks else None,
        "totals": totals,
        "by_week": weeks,
        "unscored_rules": unscored,
    }


def get_player_stats(conn: Connection, a: GetPlayerStats) -> dict[str, Any]:
    return _player_block(conn, q.resolve_player(conn, **a.player.model_dump()), a)


def compare_players(conn: Connection, a: ComparePlayers) -> dict[str, Any]:
    refs = [q.resolve_player(conn, **p.model_dump()) for p in a.players]
    return {"players": [_player_block(conn, r, a) for r in refs]}


def defense_vs_position(conn: Connection, a: DefenseVsPosition) -> dict[str, Any]:
    season, last = q.latest_week(conn)
    w0 = max(1, last - a.last_n + 1)
    rules, label = q.rules_for(conn, a.league_id, a.scoring)
    allowed: dict[str, dict[int, float]] = {}
    for r in q.weekly_rows(conn, None, [season], (w0, last)):
        if r["position"] == a.position and r["opponent"]:
            by_week = allowed.setdefault(r["opponent"], {})
            by_week[r["week"]] = (
                by_week.get(r["week"], 0.0) + score(r["stats"], a.position, rules).points
            )
    per_game = {t: round(mean(w.values()), 2) for t, w in allowed.items()}
    ranked = sorted(per_game, key=lambda t: -per_game[t])
    team = a.team.upper()
    if team not in per_game:
        raise ValueError(f"no games for defense {team} in weeks {w0}-{last} of {season}")
    return {
        "team": team,
        "position": a.position,
        "season": season,
        "weeks": [w0, last],
        "scoring": label,
        "points_allowed_per_game": per_game[team],
        "games": len(allowed[team]),
        "rank_most_allowed": ranked.index(team) + 1,
        "teams_ranked": len(ranked),
    }


def get_injury_status(conn: Connection, a: GetInjuryStatus) -> dict[str, Any]:
    ref = q.resolve_player(conn, **a.player.model_dump())
    snaps = q.injury_snapshots(conn, ref.id)
    return {
        "player": ref.model_dump(),
        "snapshots": snaps,  # each carries `ingested_at`: when we observed it
        "last_successful_sync": {
            "sleeper": q.last_sync(conn, "snapshot.sleeper_injuries"),
            "nflverse": q.last_sync(conn, "snapshot.nflverse_injuries"),
        },
        "note": None if snaps else "no injury designation observed since collection began",
    }


def get_trending(conn: Connection, a: GetTrending) -> dict[str, Any]:
    rows = q.trending(conn, a.kind, a.limit)
    return {
        "kind": a.kind,
        "window_start": rows[0]["window_start"] if rows else None,
        "players": rows,
        "attribution": "Trending data from Sleeper",
    }


def _projection(conn: Connection, ref: q.PlayerRef, a: ScoringChoice) -> dict[str, Any]:
    season, last = q.latest_week(conn)
    rules, label = q.rules_for(conn, a.league_id, a.scoring)
    rows = q.weekly_rows(conn, [ref.id], [season - 1, season])
    games = baseline.scored_history(rows, rules).get(ref.id, [])
    got = baseline.project(games, season, last + 1)
    return {
        "player": ref.model_dump(),
        "season": season,
        "week": last + 1,
        "scoring": label,
        "projected_points": got[0] if got else None,
        "games_used": got[1] if got else 0,
        "model_version": baseline.MODEL_VERSION,
        "last_game": {"season": games[-1][0], "week": games[-1][1], "points": games[-1][2]}
        if games
        else None,
        "caveats": [
            "assumes he plays; injuries and depth-chart changes are not in the baseline",
            "no prediction interval yet (Phase 8)",
        ],
    }


def project_player(conn: Connection, a: ProjectPlayer) -> dict[str, Any]:
    return _projection(conn, q.resolve_player(conn, **a.player.model_dump()), a)


def compare_start_sit(conn: Connection, a: CompareStartSit) -> dict[str, Any]:
    refs = [q.resolve_player(conn, **p.model_dump()) for p in a.players]
    projections = [_projection(conn, r, a) for r in refs]
    for p in projections:
        p["injury"] = q.injury_snapshots(conn, p["player"]["id"])
    ranked = sorted(projections, key=lambda p: -(p["projected_points"] or -1e9))
    return {"ranked": ranked, "higher_projection": ranked[0]["player"]["name"]}


def rank_waivers(conn: Connection, a: RankWaivers) -> dict[str, Any]:
    season, last = q.latest_week(conn)
    taken, roster_week, synced = q.rostered(conn, a.league_id)
    if roster_week is None:
        raise ValueError(f"league {a.league_id} has no imported rosters")
    rules, label = q.rules_for(conn, a.league_id, None)
    positions = {a.position} if a.position else set(q.SKILL)
    rows = [
        r
        for r in q.weekly_rows(conn, None, [season - 1, season])
        if r["player_id"] not in taken and r["position"] in positions
    ]
    # Only players active this season are candidates; last season only informs the average.
    active = {r["player_id"] for r in rows if r["season"] == season}
    history = baseline.scored_history(rows, rules)
    picks = []
    for pid in active:
        got = baseline.project(history[pid], season, last + 1)
        if got:
            picks.append((got[0], got[1], pid))
    picks.sort(reverse=True)
    refs = q.players_by_id(conn, [pid for _, _, pid in picks[: a.limit]])
    return {
        "league_id": a.league_id,
        "scoring": label,
        "week": last + 1,
        "rosters_as_of": {"week": roster_week, "synced_at": synced},
        "rostered_players_excluded": len(taken),
        "candidates": [
            {**refs[pid].model_dump(), "projected_points": pts, "games_used": n}
            for pts, n, pid in picks[: a.limit]
        ],
        "model_version": baseline.MODEL_VERSION,
    }


TOOLS: dict[str, tuple[type[BaseModel], Callable[[Connection, Any], dict[str, Any]], str]] = {
    "get_player_stats": (
        GetPlayerStats,
        get_player_stats,
        "A player's weekly stats and fantasy points.",
    ),
    "compare_players": (
        ComparePlayers,
        compare_players,
        "Side-by-side stats and points for 2-4 players.",
    ),
    "defense_vs_position": (
        DefenseVsPosition,
        defense_vs_position,
        "Fantasy points a defense allows to a position per game, with rank.",
    ),
    "get_injury_status": (
        GetInjuryStatus,
        get_injury_status,
        "Latest observed injury status per source, with observation time.",
    ),
    "get_trending": (
        GetTrending,
        get_trending,
        "Sleeper's most added or dropped players in the latest window.",
    ),
    "project_player": (
        ProjectPlayer,
        project_player,
        "Baseline fantasy-point projection for next week.",
    ),
    "compare_start_sit": (
        CompareStartSit,
        compare_start_sit,
        "Rank 2-4 players by projection, with injury status.",
    ),
    "rank_waivers": (
        RankWaivers,
        rank_waivers,
        "Best unrostered players in a league by projection.",
    ),
}


def run_tool(conn: Connection, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate `args` against the tool's schema, then run it. Unknown tools are refused."""
    if name not in TOOLS:
        raise ValueError(f"unknown tool {name!r}")
    model, fn, _ = TOOLS[name]
    return fn(conn, model.model_validate(args))
