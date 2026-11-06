"""Phase 11: the recomputed baseline change is exact arithmetic over the same history."""

import pytest

from fantasy_rag.ml.baseline import HALF_LIFE, ewm, explain, project

S = 2026
GAMES = [(2025, 17, 10.0), (S, 1, 8.0), (S, 2, 14.0), (S, 4, 22.5), (S, 5, -1.2), (S, 6, 9.0)]


def test_before_and_after_are_the_served_projections_and_steps_add_up() -> None:
    got = explain(GAMES, S, 2, 7)
    assert got["available"]
    assert (got["before"]["points"], got["before"]["games_used"]) == project(GAMES, S, 2)
    assert (got["after"]["points"], got["after"]["games_used"]) == project(GAMES, S, 7)
    assert got["baseline_change"] == round(got["after"]["points"] - got["before"]["points"], 2)
    assert [s["week"] for s in got["steps"]] == [2, 4, 5, 6]
    assert got["no_game_weeks"] == [3]  # no game: no step, no change
    total = sum(s["change"] for s in got["steps"])
    assert abs(total - got["baseline_change"]) <= 0.01 * len(got["steps"])


def test_one_new_game_follows_the_closed_form_step() -> None:
    got = explain(GAMES, S, 6, 7)
    (step,) = got["steps"]
    past = [10.0, 8.0, 14.0, 22.5, -1.2]
    d = 0.5 ** (1 / HALF_LIFE)
    weights = sum(d**k for k in range(len(past)))
    m = ewm(past, HALF_LIFE)
    assert step["projection_before"] == round(m, 2)
    assert step["change"] == round((9.0 - m) / (d * weights + 1), 2)
    assert step["weight_pct"] == round(100 / (d * weights + 1), 1)
    assert got["after"]["points"] == round(ewm([*past, 9.0], HALF_LIFE), 2)


def test_negative_games_and_projections_are_ordinary_values() -> None:
    bad = [(S, 1, -2.0), (S, 2, -3.5)]
    got = explain(bad, S, 2, 3)
    assert got["before"]["points"] == -2.0 and got["after"]["points"] < -2.0
    assert got["steps"][0]["game_points"] == -3.5 and got["steps"][0]["change"] < 0


def test_a_game_equal_to_the_projection_moves_nothing() -> None:
    got = explain([(S, 1, 12.0), (S, 2, 12.0)], S, 2, 3)
    assert got["steps"][0]["change"] == 0.0 and got["baseline_change"] == 0.0


def test_the_half_life_passed_in_sets_the_decay() -> None:
    a, b = explain(GAMES, S, 6, 7, half_life=2.0), explain(GAMES, S, 6, 7)
    assert a["steps"][0]["weight_pct"] != b["steps"][0]["weight_pct"]
    assert a["after"]["points"] == project(GAMES, S, 7, half_life=2.0)[0]  # type: ignore[index]


@pytest.mark.parametrize(
    ("since", "week", "reason"),
    [
        (0, 1, "no earlier week this season to compare with"),
        (1, 9, "more than 6 weeks apart"),
        (5, 5, "week 5 is not before week 5"),
    ],
)
def test_unsupported_comparisons_are_results_not_errors(since: int, week: int, reason: str) -> None:
    assert explain(GAMES, S, since, week) == {
        "available": False, "reason": reason, "since_week": since, "week": week,
    }  # fmt: skip


def test_a_first_projection_has_nothing_to_compare_with() -> None:
    got = explain([(S, 3, 11.0)], S, 2, 4)  # no game before week 2, no prior season
    assert not got["available"] and got["reason"] == "no games before week 2"
