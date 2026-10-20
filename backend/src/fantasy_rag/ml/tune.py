"""`fantasy-rag backtest`: tune the baseline on one season, report it on the next."""

import json
from typing import Any

from sqlalchemy import Engine, text

from fantasy_rag.analytics.queries import rules_for, weekly_rows
from fantasy_rag.ml import baseline
from fantasy_rag.ml.backtest import evaluate
from fantasy_rag.ml.baseline import scored_history

GRID = [(h, prior) for h in (1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0) for prior in (True, False)]
NAIVE = {"last game": (0.01, True), "season average": (1000.0, False)}


def run_backtest(engine: Engine, tune_season: int, test_season: int) -> dict[str, Any]:
    with engine.connect() as conn:
        rules, _ = rules_for(conn, None, "ppr")
        rows = weekly_rows(conn, None, [tune_season - 1, tune_season, test_season])
    history = scored_history(rows, rules)
    positions = {r["player_id"]: r["position"] for r in rows}

    tuned = [evaluate(history, positions, tune_season, h, p) for h, p in GRID]
    best = min(tuned, key=lambda r: r["mae"])
    h, prior = best["half_life"], best["use_prior_season"]
    test = evaluate(history, positions, test_season, h, prior)
    naive = {
        name: evaluate(history, positions, test_season, nh, np) for name, (nh, np) in NAIVE.items()
    }

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO model_runs (model_version, training_window, features, metrics)
                VALUES (:v, :w, :f, :m)
                ON CONFLICT (model_version) DO UPDATE SET
                    training_window = EXCLUDED.training_window, features = EXCLUDED.features,
                    metrics = EXCLUDED.metrics, created_at = now()
                """
            ),
            {
                "v": baseline.MODEL_VERSION,
                "w": json.dumps({"tune": tune_season, "test": test_season, "scoring": "ppr"}),
                "f": [f"ewm_points(half_life={h}, prior_season={prior})"],
                "m": json.dumps({"tune": best, "test": test, "naive_test": naive}),
            },
        )
    return {"grid": tuned, "best": best, "test": test, "naive": naive}
