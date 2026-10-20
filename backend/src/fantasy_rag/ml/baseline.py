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
    past = [
        p
        for s, w, p in games
        if (s, w) < (season, week) and (use_prior_season or s == season) and s >= season - 1
    ]
    if not past:
        return None
    return round(ewm(past, half_life), 2), len(past)
