"""Rolling-origin backtest of the baseline: each week is projected only from earlier games.

Evaluation set: QB/RB/WR/TE regular-season player-weeks with a stat row (the player
played) and at least one earlier game. Availability isn't predicted, so this measures
"how many points if he plays", which is what start/sit compares.
"""

from itertools import combinations
from statistics import mean
from typing import Any

from fantasy_rag.ml.baseline import project

POSITIONS = ("QB", "RB", "WR", "TE")
# Start/sit pairs come from the players a manager would actually weigh: the top N per
# position by projection each week (roughly startable in 12-team leagues, plus depth).
RELEVANT = {"QB": 24, "RB": 48, "WR": 60, "TE": 24}


def evaluate(
    history: dict[str, list[tuple[int, int, float]]],
    positions: dict[str, str],
    season: int,
    half_life: float,
    use_prior_season: bool,
) -> dict[str, Any]:
    preds: dict[tuple[int, str], list[tuple[float, float]]] = {}
    errors: dict[str, list[float]] = {p: [] for p in POSITIONS}
    for pid, games in history.items():
        pos = positions.get(pid)
        if pos not in POSITIONS:
            continue
        for s, w, actual in games:
            if s != season:
                continue
            got = project(games, s, w, half_life, use_prior_season)
            if got is None:
                continue
            errors[pos].append(abs(got[0] - actual))
            preds.setdefault((w, pos), []).append((got[0], actual))

    concordant = total = 0
    for (_, pos), rows in preds.items():
        top = sorted(rows, key=lambda r: -r[0])[: RELEVANT[pos]]
        for (p1, a1), (p2, a2) in combinations(top, 2):
            if a1 == a2 or p1 == p2:
                continue
            total += 1
            concordant += (p1 > p2) == (a1 > a2)
    all_errors = [e for es in errors.values() for e in es]
    return {
        "season": season,
        "half_life": half_life,
        "use_prior_season": use_prior_season,
        "n": len(all_errors),
        "mae": round(mean(all_errors), 3) if all_errors else None,
        "mae_by_position": {p: round(mean(e), 3) for p, e in errors.items() if e},
        "start_sit_accuracy": round(concordant / total, 4) if total else None,
        "start_sit_pairs": total,
    }
