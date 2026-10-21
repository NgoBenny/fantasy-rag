"""Ridge and LightGBM per position and stat component; points come from the scoring rules.

A policy predicts a player's stat components, fills the rest from his weighted averages,
and scores them under each format. Players with fewer than MIN_GAMES usable games get
the baseline instead (the fallback is part of the policy being evaluated).
"""

from dataclasses import dataclass
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


@dataclass
class Fitted:
    """A trained policy for one position: what the forward log saves as an artifact."""

    kind: str
    position: str
    names: list[str]  # feature order
    models: dict[str, Any]  # component (or "*" for Ridge's multi-output) -> estimator

    def predict(self, rows: list[Row]) -> dict[str, np.ndarray]:
        x = matrix(rows, self.names)
        if self.kind == "ridge":
            pred = self.models["*"].predict(x)
            return {c: pred[:, i] for i, c in enumerate(MODELED[self.position])}
        return {c: np.asarray(m.predict(x)) for c, m in self.models.items()}


def fit(kind: str, params: dict[str, Any], train: list[Row], position: str) -> Fitted:
    """Fit only on `train` (every learned step: imputation, scaling, model)."""
    names = sorted(train[0].features)
    x = matrix(train, names)
    targets = MODELED[position]
    if kind == "ridge":
        model = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                              StandardScaler(), Ridge(**params))  # fmt: skip
        y = np.column_stack([labels(train, c) for c in targets])
        return Fitted(kind, position, names, {"*": model.fit(x, y)})
    models = {}
    for c in targets:  # LightGBM handles missing values itself
        m = LGBMRegressor(**params, random_state=0, verbose=-1, n_jobs=4)
        models[c] = m.fit(x, labels(train, c))
    return Fitted(kind, position, names, models)


def fit_predict(
    kind: str, params: dict[str, Any], train: list[Row], test: list[Row], position: str
) -> dict[str, np.ndarray]:
    """Component -> predictions for `test`, fit only on `train`."""
    return fit(kind, params, train, position).predict(test)


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
