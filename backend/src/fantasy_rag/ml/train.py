"""Ridge and LightGBM per position and stat component; points come from the scoring rules.

A policy predicts a player's stat components, fills the rest from his weighted averages,
and scores them under each format. Players with fewer than MIN_GAMES usable games get
the baseline instead (the fallback is part of the policy being evaluated).
"""

from typing import Any

import numpy as np
from lightgbm import LGBMRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from fantasy_rag.analytics.scoring import ScoringRules, score
from fantasy_rag.ml.features import COMPONENTS, MODELED, Row

MIN_GAMES = 3
RIDGE_GRID = [{"alpha": a} for a in (1.0, 10.0, 100.0, 1000.0)]
LGBM_GRID = [
    {"n_estimators": 200, "learning_rate": 0.05, "num_leaves": 15, "min_child_samples": 50},
    {"n_estimators": 400, "learning_rate": 0.03, "num_leaves": 31, "min_child_samples": 50},
    {"n_estimators": 300, "learning_rate": 0.05, "num_leaves": 7, "min_child_samples": 100},
]


def matrix(rows: list[Row], names: list[str]) -> np.ndarray:
    return np.array(
        [[np.nan if r.features[n] is None else r.features[n] for n in names] for r in rows],
        dtype=float,
    )


def labels(rows: list[Row], component: str) -> np.ndarray:
    return np.array([float((r.label.stats if r.label else {}).get(component) or 0.0)
                     for r in rows])  # fmt: skip


def fit_predict(
    kind: str, params: dict[str, Any], train: list[Row], test: list[Row], position: str
) -> dict[str, np.ndarray]:
    """Component -> predictions for `test`, fit only on `train` (every learned step)."""
    names = sorted(train[0].features)
    x_train, x_test = matrix(train, names), matrix(test, names)
    targets = MODELED[position]
    if kind == "ridge":
        model = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                              StandardScaler(), Ridge(**params))  # fmt: skip
        y = np.column_stack([labels(train, c) for c in targets])
        pred = model.fit(x_train, y).predict(x_test)
        return {c: pred[:, i] for i, c in enumerate(targets)}
    out: dict[str, np.ndarray] = {}
    for c in targets:  # LightGBM handles missing values itself
        m = LGBMRegressor(**params, random_state=0, verbose=-1, n_jobs=4)
        out[c] = np.asarray(m.fit(x_train, labels(train, c)).predict(x_test))
    return out


def points(
    row: Row, predicted: dict[str, float], formats: dict[str, ScoringRules]
) -> dict[str, float | None]:
    """Score predicted components (others from the player's weighted averages); the
    baseline when the player has too little history."""
    if row.games_used < MIN_GAMES:
        return dict(row.baseline)
    stats = {c: row.features[f"{c}_ewm"] or 0.0 for c in COMPONENTS}
    # Counts (catches, TDs, picks, fumbles) can't be negative; yards can.
    stats |= {c: v if c.endswith("_yards") else max(0.0, v) for c, v in predicted.items()}
    return {f: score(stats, row.position, r).points for f, r in formats.items()}
