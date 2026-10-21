"""Phase 8 safeguards: the cutoff, leakage under perturbation, postponed games, ranges."""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from fantasy_rag.ml import ranges
from fantasy_rag.ml.benchmark import FORMATS, run_policy
from fantasy_rag.ml.features import ET, LAG, Game, History, cutoffs, rows_for_labels

SEASON = 2024
THURSDAY = datetime(2024, 9, 5, 20, 15, tzinfo=ET)  # week 1's first kickoff


def sunday(week: int) -> datetime:
    return (THURSDAY + timedelta(days=7 * (week - 1) + 3)).replace(hour=13, minute=0)


def schedule(weeks: int = 8) -> list[dict]:  # type: ignore[type-arg]
    out = []
    for w in range(1, weeks + 1):
        thu = THURSDAY + timedelta(days=7 * (w - 1))
        out.append(
            {
                "season": SEASON,
                "week": w,
                "game_type": "REG",
                "kickoff_at": thu,
                "home_team": "AAA",
                "away_team": "BBB",
                "game_id": f"T{w}",
            }
        )
        out.append({"season": SEASON, "week": w, "game_type": "REG", "kickoff_at": sunday(w),
                    "home_team": "CCC", "away_team": "DDD", "game_id": f"S{w}"})  # fmt: skip
    return out


def games(weeks: int = 8, seed: int = 1) -> list[Game]:
    rng = random.Random(seed)
    out = []
    for p in range(12):
        team, opp = ("CCC", "DDD") if p % 2 else ("DDD", "CCC")
        for w in range(1, weeks + 1):
            rec = rng.randint(0, 9)
            stats = {
                "receptions": rec,
                "receiving_yards": rec * rng.randint(6, 14),
                "receiving_tds": rng.random() < 0.2,
                "targets": rec + rng.randint(0, 4),
            }
            out.append(Game(f"p{p}", "WR", SEASON, w, f"S{w}", team, opp, sunday(w),
                            stats, 0.5 + p / 30))  # fmt: skip
    return out


def test_the_cutoff_is_tuesday_noon_eastern_before_the_first_game() -> None:
    cut = cutoffs(schedule())
    assert cut[(SEASON, 1)] == datetime(2024, 9, 3, 12, tzinfo=ET).astimezone(UTC)
    assert cut[(SEASON, 2)].astimezone(ET).strftime("%a %H:%M") == "Tue 12:00"


def _rows(gs: list[Game]):  # type: ignore[no-untyped-def]
    return {
        (r.player_id, r.week): r
        for r in rows_for_labels(History(gs, FORMATS), schedule(), [SEASON])
    }


def test_changing_data_after_a_cutoff_changes_nothing_predicted_at_it() -> None:
    base = games()
    cut = cutoffs(schedule())[(SEASON, 5)]
    # Every game from week 5 on gets different stats; a new player appears later too.
    future = [replace(g, stats={k: v * 3 + 1 for k, v in g.stats.items()})
              if g.kickoff > cut - LAG else g for g in base]  # fmt: skip
    future.append(Game("late", "WR", SEASON, 6, "S6", "CCC", "DDD", sunday(6), {"receptions": 9}))
    a, b = _rows(base), _rows(future)
    for (pid, w), r in a.items():
        if w <= 5:
            assert b[(pid, w)].features == r.features and b[(pid, w)].baseline == r.baseline
    pa = run_policy("ridge", {"alpha": 1.0}, list(a.values()), SEASON)
    pb = run_policy("ridge", {"alpha": 1.0}, list(b.values()), SEASON)
    for key, got in pa.items():
        if key[2] <= 5:
            assert pb[key] == got, key  # training labels after the cutoff never entered


def test_a_postponed_game_is_neither_a_feature_nor_a_label_before_it_is_played() -> None:
    gs = games()
    cut5 = cutoffs(schedule())[(SEASON, 5)]
    # p0's week-4 game moved to Tuesday 1 pm, after week 5's cutoff.
    moved = [replace(g, kickoff=cut5 + timedelta(hours=1)) if (g.player_id, g.week) == ("p0", 4)
             else g for g in gs]  # fmt: skip
    rows = _rows(moved)
    r5 = rows[("p0", 5)]
    assert r5.games_used == 3 and r5.latest_input is not None
    assert r5.latest_input <= cut5 - LAG
    # It still can't train week 5's model, whatever its week number says.
    preds = run_policy("ridge", {"alpha": 1.0}, list(rows.values()), SEASON)
    assert ("p0", SEASON, 5) in preds


def test_range_order_statistics_are_one_based_without_interpolation() -> None:
    assert ranges.offsets([float(i) for i in range(49)]) is None  # under 50: no range
    r = [float(i) for i in range(1, 100)]  # n = 99: r(10) and r(90)
    assert ranges.offsets(list(reversed(r))) == (10.0, 90.0)
    r50 = [float(i) for i in range(1, 51)]  # n = 50: floor(5.1) = 5, ceil(45.9) = 46
    assert ranges.offsets(r50) == (5.0, 46.0)


@pytest.mark.parametrize("n", [50, 99, 500])
def test_nominal_coverage_on_exchangeable_data(n: int) -> None:
    rng = random.Random(n)
    hits = trials = 0
    for _ in range(400):
        res = [rng.gauss(0, 1) for _ in range(n)]
        lo, hi = ranges.offsets(res)  # type: ignore[misc]
        trials += 1
        hits += lo <= rng.gauss(0, 1) <= hi
    assert 0.74 <= hits / trials <= 0.9  # nominal 80%, at least ~80% on average


def test_a_tied_prediction_keeps_its_pair_with_half_credit() -> None:
    from fantasy_rag.ml.benchmark import actual, by_week, pairs

    rows = list(_rows(games()).values())
    week = [r for r in rows if r.week == 6]
    prs = pairs(week)
    flat = {(r.player_id, r.season, r.week): {"ppr": 1.0} for r in week}  # every pair tied
    got = by_week(week, flat, "ppr", prs)[6]
    decided = sum(1 for ps in prs.values() for a, b in ps if actual(a, "ppr") != actual(b, "ppr"))
    assert got["total"] == decided > 0 and got["hit"] == got["total"] * 0.5


def test_negative_yards_score_negative_points() -> None:
    from fantasy_rag.ml.train import points

    r = _rows(games())[("p1", 6)]
    zero = {c: 0.0 for c in ("receptions", "receiving_yards", "receiving_tds", "rushing_yards")}
    base = points(r, zero, FORMATS)["std"]
    neg = points(r, {**zero, "rushing_yards": -2.0, "receptions": -1.0}, FORMATS)["std"]
    assert neg == pytest.approx(base - 0.2)  # -2 yards count; -1 catches is clipped to 0


def test_a_ppr_winner_worse_in_half_or_standard_stays_on_the_baseline() -> None:
    from fantasy_rag.ml.benchmark import POSITIONS, decide

    def fmt(base_mae: float, ridge_mae: float) -> dict:  # type: ignore[type-arg]
        m = lambda mae, s: {"mae": mae, "spearman": s, "start_sit": s}  # noqa: E731
        return {"baseline": {"by_position": {p: m(base_mae, 0.5) for p in POSITIONS}},
                "ridge": {"by_position": {p: m(ridge_mae, 0.6) for p in POSITIONS},
                          "vs_baseline_ci": {p: {"mae": [-0.5, -0.1]} for p in POSITIONS}},
                "lgbm": {"by_position": {p: m(base_mae, 0.4) for p in POSITIONS},
                         "vs_baseline_ci": {p: {"mae": [-0.1, 0.1]} for p in POSITIONS}},
                }  # fmt: skip

    ok = decide({"formats": {"ppr": fmt(6.0, 5.0), "half": fmt(5.0, 4.5), "std": fmt(4.0, 3.9)}})
    assert ok["QB"]["winner"] == "ridge"
    blocked = decide(
        {"formats": {"ppr": fmt(6.0, 5.0), "half": fmt(5.0, 5.2), "std": fmt(4.0, 3.9)}}
    )
    assert blocked["QB"] == {
        "winner": None,
        "ppr_qualified": ["ridge"],
        "blocked_by_formats": ["half"],
    }


def test_snaps_schedule_and_ranges_after_a_cutoff_change_nothing_before_it() -> None:
    from fantasy_rag.ml.benchmark import range_offsets

    sched = schedule()
    cut = cutoffs(sched)[(SEASON, 7)]
    base = games()
    later = [replace(g, snap_pct=0.01, stats={k: v * 5 + 2 for k, v in g.stats.items()})
             if g.kickoff > cut - LAG else g for g in base]  # fmt: skip
    # Later weeks' schedule changes too (a flexed kickoff, swapped home team).
    sched_b = [dict(s, kickoff_at=s["kickoff_at"] + timedelta(hours=3), home_team=s["away_team"],
                    away_team=s["home_team"]) if s["week"] > 7 else s for s in sched]  # fmt: skip
    ra = {
        (r.player_id, r.week): r for r in rows_for_labels(History(base, FORMATS), sched, [SEASON])
    }
    rb = {
        (r.player_id, r.week): r
        for r in rows_for_labels(History(later, FORMATS), sched_b, [SEASON])
    }
    for k, r in ra.items():
        if k[1] <= 7:
            assert rb[k].features == r.features
    pa = run_policy("ridge", {"alpha": 1.0}, list(ra.values()), SEASON)
    pb = run_policy("ridge", {"alpha": 1.0}, list(rb.values()), SEASON)
    oa = range_offsets(list(ra.values()), pa, "ppr")
    ob = range_offsets(list(rb.values()), pb, "ppr")
    week7 = [k for k in oa if k[2] == 7]
    assert week7 and any(oa[k] is not None for k in week7)  # ranges exist to compare
    assert all(ob[k] == oa[k] for k in week7)


def test_the_forward_run_labels_its_snapshot_and_logs_te_ridge_beside_the_baseline() -> None:
    from fantasy_rag.ml.predict import forecast

    gs = [replace(g, position="TE") if g.player_id in ("p0", "p1", "p2", "p3") else g
          for g in games(weeks=5)]  # fmt: skip
    sched = schedule()
    deadline = cutoffs(sched)[(SEASON, 6)]
    tuesday = deadline - timedelta(hours=1, minutes=30)  # 10:30 ET
    f = forecast(gs, sched, FORMATS, tuesday, {})
    assert (f.season, f.week, f.status, f.cutoff) == (SEASON, 6, "on_time", deadline)
    assert set(f.preds["baseline"]) == {f"p{i}" for i in range(12)}
    assert set(f.preds["ridge"]) == {"p0", "p1", "p2", "p3"}  # TE only
    assert f.newest_game is not None and f.newest_game <= deadline - LAG
    assert "ridge" in f.artifacts
    # Same Thursday-before-kickoff week, but after the deadline: late, never on time.
    assert forecast(gs, sched, FORMATS, deadline + timedelta(hours=20), {}).status == "late"
    # Before the last game of the previous week could be in: early.
    assert forecast(gs, sched, FORMATS, sunday(5) + timedelta(hours=1), {}).status == "early"


def test_league_rules_the_model_does_not_predict_still_count() -> None:
    """A league scoring special-teams forced fumbles and targets: the trained projection
    keeps those points (from the baseline), instead of silently dropping them."""
    from fantasy_rag.analytics.scoring import ScoringRules
    from fantasy_rag.ml.train import points

    league = FORMATS | {  # History needs PPR for its opponent feature
        "lg": ScoringRules.from_sleeper({"rec": 1, "rec_yd": 0.1, "st_ff": 4, "rec_tgt": 0.5})
    }
    gs = [replace(g, stats={**g.stats, "st_ff": 1}) for g in games()]  # one per game
    r = {
        (x.player_id, x.week): x for x in rows_for_labels(History(gs, league), schedule(), [SEASON])
    }[("p1", 6)]
    same = {c: r.features[f"{c}_ewm"] for c in ("receptions", "receiving_yards", "receiving_tds")}
    assert points(r, same, league)["lg"] == r.baseline["lg"]  # includes 4 pts of st_ff
    more = same | {"receptions": same["receptions"] + 1}
    assert points(r, more, league)["lg"] == pytest.approx(r.baseline["lg"] + 1, abs=0.011)


def test_forward_ranges_use_only_recorded_errors_and_fallbacks_say_so() -> None:
    from fantasy_rag.ml import ranges as rng
    from fantasy_rag.ml.predict import forecast

    gs = [replace(g, position="TE") if g.player_id in ("p0", "p1") else g for g in games(weeks=5)]
    gs.append(Game("rookie", "TE", SEASON, 5, "S5", "CCC", "DDD", sunday(5), {"receptions": 2}))
    sched = schedule()
    tuesday = cutoffs(sched)[(SEASON, 6)] - timedelta(hours=1)
    old = datetime(2024, 1, 1, tzinfo=UTC)
    errors = {
        ("baseline", "ppr", "TE"): [(old, float(i)) for i in range(-30, 30)],
        ("ridge", "ppr", "TE"): [(old, float(i) / 2) for i in range(-30, 30)],
    }
    f = forecast(gs, sched, FORMATS, tuesday, errors)
    base_off, ridge_off = (
        rng.offsets([float(i) for i in range(-30, 30)]),
        rng.offsets([float(i) / 2 for i in range(-30, 30)]),
    )
    assert f.ranges["ridge"]["ppr"]["TE"] == list(ridge_off)
    full = f.preds["ridge"]["p0"]["ppr"]
    assert full["effective"] == "ridge" and full["p10"] == round(full["points"] + ridge_off[0], 2)
    # One game of history: the Ridge row is the baseline, labelled and ranged as such.
    rookie = f.preds["ridge"]["rookie"]["ppr"]
    assert rookie["effective"] == "baseline" and rookie["components"] is None
    assert rookie["points"] == f.preds["baseline"]["rookie"]["ppr"]["points"]
    assert rookie["p10"] == round(rookie["points"] + base_off[0], 2)
    # Corrections to past stats change no range: ranges come from recorded errors only.
    fixed = [
        replace(g, stats={k: v + 3 for k, v in g.stats.items()}) if g.week < 3 else g for g in gs
    ]
    assert forecast(fixed, sched, FORMATS, tuesday, errors).ranges == f.ranges
