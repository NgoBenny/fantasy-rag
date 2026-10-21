"""Phase 8 serving: every caller serves the same policy, its own range, and the baseline
fallback; the forward log is insert-only. Uses a hand-written forward run (no training)."""

import json
from typing import Any

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError

from fantasy_rag.agent import tools
from fantasy_rag.analytics import queries as q
from fantasy_rag.ml import baseline, serve

pytestmark = pytest.mark.db
TE, WR = "00-4", "00-1"  # Mike Williams (TE, week 2) and Justin Jefferson (WR, weeks 1-2)
LOGGED = {"points": 9.5, "p10": 3.0, "p90": 17.25}
BASE_OFF = {"WR": [-4.0, 6.0], "TE": [-3.0, 5.0]}


def _run(
    c: Connection, weights: dict[str, Any] | None = None, rows: bool = True,
    effective: str = "ridge",
) -> int:  # fmt: skip
    rules = {k: q.rules_for(c, None, k)[0].weights for k in ("ppr",)}
    rules["league:1"] = weights or q.rules_for(c, 1, None)[0].weights
    run_id: int = c.execute(text(
        """INSERT INTO forward_runs (season, week, cutoff_at, snapshot_at, predicted_at, status,
               sync_runs, models, scoring, ranges)
           VALUES (2026, 3, now(), now(), now(), 'on_time', '{}', :m, :s, :r) RETURNING id"""), {
        "m": json.dumps({"baseline": {"version": baseline.MODEL_VERSION},
                         "ridge": {"version": "ridge-components-v1"}}),
        "s": json.dumps({k: {"weights": w} for k, w in rules.items()}),
        "r": json.dumps({"baseline": {k: BASE_OFF for k in rules},
                         "ridge": {k: {"TE": [-6.5, 7.75]} for k in rules}}),
    }).scalar_one()  # fmt: skip
    if rows:
        for key in rules:
            c.execute(text(
                "INSERT INTO projections (run_id, policy, scoring_key, player_id, points, p10,"
                " p90, effective_policy) VALUES (:r, 'ridge', :k, :p, :pts, :lo, :hi, :ef)"),
                {"r": run_id, "k": key, "p": TE, "pts": LOGGED["points"], "lo": LOGGED["p10"],
                 "hi": LOGGED["p90"], "ef": effective})  # fmt: skip
    return run_id


def _every_caller(c: Connection) -> dict[str, dict[str, Any]]:
    """What each caller says about the TE and the WR, in league 1."""
    choice = {"league_id": 1}
    one = tools.project_player(
        c, tools.ProjectPlayer(player={"name": "Mike Williams", "position": "TE"}, **choice)
    )
    pair = tools.compare_start_sit(
        c,
        tools.CompareStartSit(
            players=[{"name": "Mike Williams", "position": "TE"}, {"name": "Justin Jefferson"}],
            **choice,
        ),
    )
    waivers = tools.rank_waivers(c, tools.RankWaivers(league_id=1, limit=25))
    lineup = tools.optimize_lineup(c, tools.OptimizeLineup(league_id=1, roster_id=1))
    seen: dict[str, dict[str, Any]] = {"project_player": {TE: one}}
    seen["compare_start_sit"] = {p["player"]["id"]: p for p in pair["ranked"]}
    seen["rank_waivers"] = {p["id"]: p for p in waivers["candidates"]}
    slots = [s["player"] for s in lineup["recommended"] if s["player"]] + lineup["bench"]
    seen["optimize_lineup"] = {p["id"]: p for p in slots if p}
    return seen


def _same(seen: dict[str, dict[str, Any]], pid: str) -> set[tuple[Any, ...]]:
    return {(v[pid]["projected_points"], tuple(v[pid]["range_80"] or ()), v[pid]["policy"])
            for v in seen.values() if pid in v}  # fmt: skip


@pytest.fixture
def conn(db: str) -> Any:
    eng = create_engine(db)
    with eng.begin() as c:  # put the TE on roster 1 so the lineup tool serves him too
        c.execute(text("INSERT INTO fantasy_rosters (league_id, roster_id, week, player_ids,"
                       " sync_run_id) VALUES (1, 1, 3, '{00-1,00-4}', 1)"))  # fmt: skip
    with eng.connect() as c:
        yield c
    eng.dispose()


def test_an_active_policy_reaches_every_caller_with_its_own_range(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serve, "ACTIVE", {"TE": "ridge"})
    _run(conn)
    seen = _every_caller(conn)
    assert _same(seen, TE) == {(9.5, (3.0, 17.25), "ridge")}
    assert sum(TE in v for v in seen.values()) == 3  # waivers excludes a rostered TE
    # The WR stays on the baseline, with the baseline's own range offsets.
    wr = seen["compare_start_sit"][WR]
    pts = wr["projected_points"]
    assert wr["policy"] == "baseline" and tuple(wr["range_80"]) == (
        round(pts - 4.0, 2),
        round(pts + 6.0, 2),
    )
    assert _same(seen, WR) == {(pts, (round(pts - 4.0, 2), round(pts + 6.0, 2)), "baseline")}
    # League rules we can't compute are still reported.
    assert "rec_0_4" in seen["project_player"][TE]["unscored_rules"]


def test_inactive_policies_leave_every_caller_on_the_baseline(conn: Connection) -> None:
    assert serve.ACTIVE == {}  # production: nothing promoted yet
    _run(conn)
    seen = _every_caller(conn)
    ((pts, rng, policy),) = _same(seen, TE)
    assert policy == "baseline" and pts != 9.5 and rng == (round(pts - 3.0, 2), round(pts + 5.0, 2))


def test_without_a_matching_forward_run_every_caller_falls_back_without_a_range(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serve, "ACTIVE", {"TE": "ridge"})
    seen = _every_caller(conn)  # no forward run at all
    ((_, rng, policy),) = _same(seen, TE)
    assert policy == "baseline" and rng == ()
    _run(conn, weights={"rec": 0.5})  # a run made under the league's old rules
    seen = _every_caller(conn)
    ((_, rng, policy),) = _same(seen, TE)
    assert policy == "baseline" and rng == ()


def test_the_forward_log_is_insert_only(conn: Connection) -> None:
    run_id = _run(conn)
    conn.commit()
    for stmt in ("UPDATE projections SET points = 0", "DELETE FROM projections",
                 "UPDATE forward_runs SET week = 9", "DELETE FROM forward_runs"):  # fmt: skip
        with pytest.raises(DBAPIError, match="insert-only"):
            conn.execute(text(stmt))
        conn.rollback()
    assert conn.execute(text("SELECT count(*) FROM projections WHERE run_id = :r"),
                        {"r": run_id}).scalar_one() == 2  # fmt: skip


def test_a_fallback_row_is_served_as_the_baseline_with_the_baseline_range(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ridge row that fell back (too little history) holds baseline points: it must be
    served and labelled as the baseline, with the baseline's range, not Ridge's."""
    monkeypatch.setattr(serve, "ACTIVE", {"TE": "ridge"})
    _run(conn, effective="baseline")
    seen = _every_caller(conn)
    ((pts, rng, policy),) = _same(seen, TE)
    assert policy == "baseline" and rng == (round(pts - 3.0, 2), round(pts + 5.0, 2))
