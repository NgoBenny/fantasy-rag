import pytest

from fantasy_rag.ml.backtest import evaluate
from fantasy_rag.ml.baseline import ewm, project


def test_ewm_weights_newest_game_most() -> None:
    # half-life 1: weights 0.25, 0.5, 1 -> (0*0.25 + 10*0.5 + 20*1) / 1.75
    assert ewm([0, 10, 20], 1.0) == pytest.approx(25 / 1.75)
    assert ewm([7, 7, 7], 3.0) == pytest.approx(7)


def test_projection_never_reads_the_target_week_or_later() -> None:
    games = [(2025, 17, 10.0), (2026, 1, 20.0), (2026, 2, 30.0), (2026, 3, 999.0)]
    pts, used = project(games, 2026, 3, half_life=1000.0) or (0, 0)
    assert used == 3 and pts == pytest.approx(20.0)  # week 3's 999 is not visible
    assert project(games, 2026, 3, half_life=1000.0, use_prior_season=False) == (25.0, 2)
    assert project(games, 2025, 17) is None  # nothing before his first game


def test_backtest_start_sit_counts_ordered_pairs() -> None:
    # Two WRs; the one with the higher history also scores more in week 2 -> 1 concordant pair.
    history = {
        "a": [(2026, 1, 20.0), (2026, 2, 18.0)],
        "b": [(2026, 1, 5.0), (2026, 2, 9.0)],
    }
    r = evaluate(history, {"a": "WR", "b": "WR"}, 2026, 4.0, True)
    assert r["start_sit_pairs"] == 1 and r["start_sit_accuracy"] == 1.0
    assert r["mae"] == pytest.approx((2 + 4) / 2)  # |20-18| and |5-9|
