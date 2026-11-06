"""Baseline projection: exponentially weighted average of a player's past fantasy points.

Points are scored under the requested league's rules first, so the projection is
league-aware; for linear scoring rules this equals projecting each stat and scoring it.
HALF_LIFE and USE_PRIOR_SEASON come from `fantasy-rag backtest` (tuned on 2024,
reported on 2025), not from intuition.
"""

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from fantasy_rag.analytics.scoring import ScoringRules, score

MODEL_VERSION = "baseline-ewm-v1"
HALF_LIFE = 4.0  # games; tuned by backtest (see model_runs)
USE_PRIOR_SEASON = True


def ewm(points: list[float], half_life: float) -> float:
    """Weighted mean, newest game weight 1, each older game decays by 0.5 per half-life."""
    weights = [0.5 ** ((len(points) - 1 - i) / half_life) for i in range(len(points))]
    return float(sum(w * p for w, p in zip(weights, points, strict=True)) / sum(weights))


def scored_history(
    rows: Iterable[dict[str, Any]], rules: ScoringRules
) -> dict[str, list[tuple[int, int, float]]]:
    """player_id -> [(season, week, points)] in time order."""
    history: dict[str, list[tuple[int, int, float]]] = defaultdict(list)
    for r in rows:
        pts = score(r["stats"], r["position"] or "", rules).points
        history[r["player_id"]].append((r["season"], r["week"], pts))
    for games in history.values():
        games.sort()
    return history


def project(
    games: list[tuple[int, int, float]],
    season: int,
    week: int,
    half_life: float = HALF_LIFE,
    use_prior_season: bool = USE_PRIOR_SEASON,
) -> tuple[float, int] | None:
    """Projection for (season, week) from games strictly before it: (points, games used)."""
    past = _past(games, season, week, use_prior_season)
    if not past:
        return None
    return round(ewm(past, half_life), 2), len(past)


def _past(
    games: list[tuple[int, int, float]], season: int, week: int, use_prior_season: bool
) -> list[float]:
    return [
        p
        for s, w, p in games
        if (s, w) < (season, week) and (use_prior_season or s == season) and s >= season - 1
    ]


MAX_EXPLAIN_WEEKS = 6


def explain(
    games: list[tuple[int, int, float]],
    season: int,
    since_week: int,
    week: int,
    half_life: float = HALF_LIFE,
    use_prior_season: bool = USE_PRIOR_SEASON,
) -> dict[str, Any]:
    """Recomputed baseline change between the projections for `since_week` and `week` of
    one season: every game played in between, in order, and how far each moved the
    weighted mean. Adding a game x to a mean m with weight total S gives
    (d*S*m + x) / (d*S + 1), d = 0.5 ** (1 / half_life), so each step is exact and the
    unrounded steps add up to the whole change. A request the data can't support comes
    back `available: False` with a reason, never an exception."""

    def unavailable(reason: str) -> dict[str, Any]:
        return {"available": False, "reason": reason, "since_week": since_week, "week": week}

    games = sorted(games)  # the steps are exact only in time order
    if since_week < 1:
        return unavailable("no earlier week this season to compare with")
    if since_week >= week:
        return unavailable(f"week {since_week} is not before week {week}")
    if week - since_week > MAX_EXPLAIN_WEEKS:
        return unavailable(f"more than {MAX_EXPLAIN_WEEKS} weeks apart")
    before = project(games, season, since_week, half_life, use_prior_season)
    after = project(games, season, week, half_life, use_prior_season)
    if before is None or after is None:
        return unavailable(f"no games before week {since_week}")
    d = 0.5 ** (1 / half_life)
    past = _past(games, season, since_week, use_prior_season)
    total = sum(d**k for k in range(len(past)))  # weights: newest 1, each older times d
    mean = ewm(past, half_life)
    steps = []
    new = [(s, w, p) for s, w, p in games if (season, since_week) <= (s, w) < (season, week)]
    for s, w, x in new:
        decayed = d * total
        moved = (decayed * mean + x) / (decayed + 1)
        steps.append({
            "season": s, "week": w, "game_points": round(x, 2),
            "projection_before": round(mean, 2), "weight_pct": round(100 / (decayed + 1), 1),
            "change": round(moved - mean, 2) + 0.0,  # never -0.0
            "size": abs(round(moved - mean, 2)),  # "down 0.07" quotes this, not a sign flip
        })  # fmt: skip
        mean, total = moved, decayed + 1
    played = {w for _, w, _ in new}
    return {
        "available": True,
        "reason": None,
        "since_week": since_week,
        "week": week,
        "before": {"week": since_week, "points": before[0], "games_used": before[1]},
        "after": {"week": week, "points": after[0], "games_used": after[1]},
        "baseline_change": round(after[0] - before[0], 2) + 0.0,
        "baseline_change_size": abs(round(after[0] - before[0], 2)),
        "steps": steps,
        "no_game_weeks": [w for w in range(since_week, week) if w not in played],
    }
