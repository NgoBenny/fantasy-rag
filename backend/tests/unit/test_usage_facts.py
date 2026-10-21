"""Usage facts and window shares (pure; no database)."""

import re
from typing import Any

from fantasy_rag.analytics.usage import facts, window
from fantasy_rag.analytics.usage_check import team_snaps


def game(week: int, **kw: Any) -> dict[str, Any]:
    g = {"season": 2026, "week": week, "opponent": "X", "snaps": 40, "team_snaps": 60,
         "team_snaps_exact": True, "snap_pct": 66.7, "targets": 5, "team_targets": 35,
         "target_share_pct": 14.3, "carries": 2, "team_carries": 25, "carry_share_pct": 8.0,
         "rz_targets": 0, "rz_carries": 0, "rz_looks": 0, "gl_carries": 0,
         "rz_unknown": 0}  # fmt: skip
    return g | kw


def kinds(gs: list[dict[str, Any]], position: str = "WR") -> list[str]:
    return [f["kind"] for f in facts(gs, position)]


def test_window_share_is_totals_not_an_average_of_game_shares() -> None:
    gs = [game(1, targets=2, team_targets=10), game(2, targets=9, team_targets=45),
          game(3, targets=1, team_targets=5)]  # fmt: skip
    gs[1] |= {"targets": 18}  # game shares 20%, 40%, 20%: averaged 26.7, totals 21/60 = 35.0
    assert window(gs)["target_share_pct"] == 35.0  # type: ignore[index]


def test_rising_needs_an_increase_from_each_game_to_the_next() -> None:
    base = [game(1), game(2)]
    assert "rising" in kinds([*base, game(3, targets=4), game(4, targets=6), game(5, targets=9)])
    flat = [*base, game(3, targets=4), game(4, targets=4), game(5, targets=9)]
    assert "rising" not in kinds(flat)
    assert "rising" not in kinds([game(1, targets=0), game(2, targets=1), game(3, targets=2)])
    text = facts([game(3, targets=4), game(5, targets=6), game(6, targets=9)], "WR")[0]["text"]
    assert text == "Targets rose in each of the last 3 games: 4, 6, 9 (weeks 3, 5, 6)"
    assert "straight" not in text


def test_shift_needs_six_games_and_ten_points() -> None:
    low = [game(w, snaps=30, team_snaps=60, snap_pct=50.0) for w in (1, 2, 3)]
    high = [game(w, snaps=42, team_snaps=60, snap_pct=70.0) for w in (4, 5, 6)]
    assert kinds(low + high)[0] == "shift"
    f = facts(low + high, "WR")[0]
    assert f["values"] == {"before": 50.0, "after": 70.0, "change_pts": 20.0, "games": 3}
    assert "+20.0 pts" in f["text"]
    assert "shift" not in kinds(low[1:] + high)  # 5 games: not enough
    near = [game(w, snaps=35, team_snaps=60, snap_pct=58.3) for w in (4, 5, 6)]
    assert "shift" not in kinds(low + near)  # 8.3 points: below the bar
    approx = low + [g | {"team_snaps_exact": False} for g in high]
    assert "≈" in facts(approx, "WR")[0]["text"]


def test_season_high_is_strict_and_needs_four_games() -> None:
    gs = [game(1, targets=7), game(2, targets=3), game(3, targets=5), game(4, targets=9)]
    assert "season_high" in kinds(gs)
    tied = [*gs[:3], game(4, targets=7)]
    assert "season_high" not in kinds(tied)
    assert "season_high" not in kinds(gs[1:])  # 3 games


def test_red_zone_needs_complete_counts() -> None:
    gs = [game(1), game(2, rz_targets=2, rz_looks=2), game(3, rz_carries=1, rz_looks=1)]
    gs = [game(0), *gs]
    assert "red_zone" in kinds(gs)
    unknown = [*gs[:-1], gs[-1] | {"rz_unknown": 1}]
    assert "red_zone" not in kinds(unknown)
    missing = [*gs[:-1], gs[-1] | {"rz_looks": None, "rz_targets": None, "rz_carries": None}]
    assert "red_zone" not in kinds(missing)
    assert window(missing)["rz_looks"] is None  # type: ignore[index]  # never 0


def test_no_facts_under_three_games_or_when_nothing_fires() -> None:
    assert facts([game(1), game(2)], "WR") == []
    assert facts([game(w) for w in range(1, 7)], "WR") == []


def test_every_number_in_a_fact_is_in_its_values_or_weeks() -> None:
    low = [game(w, snaps=30, snap_pct=50.0, targets=3) for w in (1, 2, 3)]
    high = [game(4, snaps=42, snap_pct=70.0, targets=4, rz_targets=2, rz_looks=2),
            game(5, snaps=42, snap_pct=70.0, targets=6, rz_carries=1, rz_looks=1),
            game(6, snaps=42, snap_pct=70.0, targets=9)]  # fmt: skip
    found = facts(low + high, "WR")
    assert len(found) == 3
    for f in found:
        allowed = {float(w) for w in f["weeks"]}
        for v in f["values"].values():
            allowed |= {float(x) for x in (v if isinstance(v, list) else [v])}
        for n in re.findall(r"\d+(?:\.\d+)?", f["text"]):
            assert float(n) in allowed, (n, f)


def test_team_snaps_exact_or_estimated() -> None:
    assert team_snaps([(64, 1.0), (40, 0.63)]) == (64, True)
    # no full-game player: median of round(snaps / pct) over pct >= 0.5
    assert team_snaps([(60, 0.94), (50, 0.78), (10, 0.16)]) == (64, False)
    assert team_snaps([]) == (None, False)


def test_missing_snap_data_suppresses_snap_claims_instead_of_shrinking_the_window() -> None:
    # Only one of the three earlier games has snap data (Codex's reproduction).
    before = [
        game(1, snaps=12, snap_pct=20.0),
        game(2, snaps=None, snap_pct=None),
        game(3, snaps=None, snap_pct=None),
    ]
    after = [game(w, snaps=30, snap_pct=50.0) for w in (4, 5, 6)]  # fmt: skip
    w = window(before)
    assert w is not None and (w["snap_pct"], w["snap_missing"]) == (None, 2)  # never 1 game's 20%
    assert not [f for f in facts(before + after, "WR") if "Snap share" in f["text"]]
    # Season high: an earlier game with unknown snaps could have been the high.
    highs = [game(1, snap_pct=None, snaps=None), game(2, snap_pct=60.0),
             game(3, snap_pct=61.0), game(4, snap_pct=70.0)]  # fmt: skip
    assert not [f for f in facts(highs, "WR") if f["kind"] == "season_high"]
