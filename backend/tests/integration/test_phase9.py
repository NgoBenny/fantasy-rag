"""Phase 9 (local reliability) against a scratch database."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from fantasy_rag.analytics.queries import PRESETS
from fantasy_rag.analytics.scoring import ScoringRules
from fantasy_rag.ingest import health as h
from fantasy_rag.ingest.base import SourceUnavailable, sync_run
from fantasy_rag.jobs import locks
from fantasy_rag.ml import forward_results as fr
from fantasy_rag.ml import predict
from fantasy_rag.ml.features import ET

pytestmark = pytest.mark.db
NOW = datetime.now(UTC)
PPR = ScoringRules.from_sleeper(PRESETS["ppr"]).fingerprint()
HALF = ScoringRules.from_sleeper(PRESETS["half"]).fingerprint()


@pytest.fixture
def eng(db: str) -> Any:
    e = create_engine(db)
    yield e
    e.dispose()


def test_one_owner_per_lock_and_release_even_on_failure(eng: Engine) -> None:
    with (
        locks.job_lock(eng, locks.STRUCTURED) as first,
        locks.job_lock(eng, locks.STRUCTURED) as second,
    ):
        assert first and not second  # a second holder is turned away
        with locks.job_lock(eng, locks.NEWS) as other:
            assert other  # a different job runs alongside
    with pytest.raises(RuntimeError), locks.job_lock(eng, locks.STRUCTURED) as got:
        assert got
        raise RuntimeError("job crashed")
    with locks.job_lock(eng, locks.STRUCTURED) as again:
        assert again  # released on the failure path, not leaked with a pooled connection


def test_ingest_league_import_and_predict_share_one_lock() -> None:
    from argparse import Namespace

    from fantasy_rag.cli import _lock_for

    ids = {_lock_for(Namespace(command=c, what=w, action=w)) for c, w in
           [("ingest", "structured"), ("league", "import"), ("predict", None)]}  # fmt: skip
    assert ids == {locks.STRUCTURED}
    assert _lock_for(Namespace(command="ingest", what="news", action=None)) == locks.NEWS
    assert _lock_for(Namespace(command="ingest", what="news-report", action=None)) is None


def _attempt(eng: Engine, source: str, ok: bool, at: datetime) -> None:
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO sync_runs (source, started_at, finished_at, status, error)"
            " VALUES (:s, :t, :t, :st, :e)"),
            {"s": source, "t": at, "st": "ok" if ok else "failed",
             "e": None if ok else "SourceUnavailable('injected')"})  # fmt: skip


def test_health_needs_two_failures_to_say_failing_and_clears_on_recovery(eng: Engine) -> None:
    for source, _ in h.SOURCES.values():
        _attempt(eng, source, True, NOW - timedelta(minutes=30))
    with eng.connect() as c:
        assert {v["state"] for v in h.health(c, NOW).values()} == {"fresh"}
    _attempt(eng, "news", False, NOW - timedelta(minutes=20))
    with eng.connect() as c:
        assert h.health(c, NOW)["news"]["state"] == "fresh"  # one failure: age only
    _attempt(eng, "news", False, NOW - timedelta(minutes=10))
    with eng.connect() as c:
        got = h.health(c, NOW)
    assert got["news"]["state"] == "failing" and "injected" in got["news"]["error"]
    assert got["stats"]["state"] == "fresh"  # the others are unaffected
    _attempt(eng, "news", True, NOW)
    with eng.connect() as c:
        assert h.health(c, NOW)["news"]["state"] == "fresh"
        # Old successes alone: stale.
        assert h.health(c, NOW + timedelta(hours=4))["news"]["state"] == "stale"


def test_an_injected_outage_is_recorded_like_a_real_one(
    eng: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FANTASY_RAG_FAIL_SOURCE", "news")
    with pytest.raises(SourceUnavailable), sync_run(eng, "news"):
        pass
    with sync_run(eng, "sleeper.transactions") as run:  # other sources still run
        run.rows = 0
    with eng.connect() as c:
        rows = dict(c.execute(text("SELECT source, status FROM sync_runs WHERE source IN"
                                   " ('news', 'sleeper.transactions')")).all())  # fmt: skip
    assert rows == {"news": "failed", "sleeper.transactions": "ok"}


def _week(eng: Engine, week: int, thursday: datetime) -> None:
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO schedules (game_id, season, week, game_type, home_team, away_team,"
            " kickoff_at, sync_run_id) VALUES (:g, 2026, :w, 'REG', 'A', 'B', :k, 1)"),
            {"g": f"w{week}", "w": week, "k": thursday})  # fmt: skip


def _run(eng: Engine, week: int, status: str) -> int:
    with eng.begin() as c:
        rid: int = c.execute(text(
            """INSERT INTO forward_runs (season, week, cutoff_at, snapshot_at, predicted_at, status,
                   sync_runs, models, scoring, ranges)
               VALUES (2026, :w, now(), now(), now(), :st, '{}', '{}', '{}', '{}') RETURNING id"""),
            {"w": week, "st": status}).scalar_one()  # fmt: skip
    return rid


def test_every_past_tuesday_has_exactly_one_status(eng: Engine) -> None:
    base = datetime(2026, 10, 29, 20, 15, tzinfo=ET)  # week 8 opener
    for i, week in enumerate(range(8, 13)):
        _week(eng, week, base + timedelta(days=7 * i))
    _run(eng, 8, "on_time")
    _run(eng, 9, "late")
    _run(eng, 10, "early")  # early only: never a success, never dropped
    _run(eng, 11, "early")
    _run(eng, 11, "on_time")  # on time wins over an earlier early run
    with eng.connect() as c:
        got = {t["week"]: t["status"] for t in fr.tuesdays(c, base + timedelta(days=40))}
    assert got == {8: "on_time", 9: "late", 10: "early_only", 11: "on_time", 12: "missing"}


def test_the_pre_deadline_check(eng: Engine) -> None:
    thursday = datetime(2026, 10, 29, 20, 15, tzinfo=ET)
    _week(eng, 8, thursday)
    deadline = fr.cutoffs([{"season": 2026, "week": 8, "game_type": "REG",
                            "kickoff_at": thursday}])[(2026, 8)]  # fmt: skip
    with eng.connect() as c:
        assert fr.check(c, deadline - timedelta(hours=2))[0]  # not due yet
        ok, msg = fr.check(c, deadline - timedelta(minutes=20))
        assert not ok and "NO on-time" in msg
        assert not fr.check(c, deadline + timedelta(hours=1))[0]
    _run(eng, 8, "on_time")
    with eng.connect() as c:
        assert fr.check(c, deadline + timedelta(hours=1))[0]


def test_forward_results_use_one_rule_set_on_time_runs_and_matched_samples(eng: Engine) -> None:
    on_time, late = _run(eng, 8, "on_time"), _run(eng, 9, "late")
    rows = [  # (policy, fp, player, week, error, run)
        ("baseline", PPR, "00-4", 8, 2.0, on_time), ("ridge", PPR, "00-4", 8, 1.0, on_time),
        ("baseline", PPR, "00-6", 8, 4.0, on_time),  # Ridge fell back: baseline only
        ("baseline", HALF, "00-4", 8, 9.0, on_time), ("ridge", HALF, "00-4", 8, 9.0, on_time),
        ("baseline", PPR, "00-4", 9, 50.0, late), ("ridge", PPR, "00-4", 9, 50.0, late),
    ]  # fmt: skip
    with eng.begin() as c:
        for pol, fp, pid, week, err, run in rows:
            c.execute(text(
                "INSERT INTO calibration_errors (policy, rules_fp, position, player_id, season,"
                " week, kickoff, error, source, run_id)"
                " VALUES (:pol, :fp, 'TE', :p, 2026, :w, now(), :e, 'forward', :r)"),
                {"pol": pol, "fp": fp, "p": pid, "w": week, "e": err, "r": run})  # fmt: skip
    with eng.connect() as c:
        got = fr.results(c, "ppr")
        assert fr.results(c, "half")["matched_total"]["baseline_mae"] == 9.0  # never mixed
        with pytest.raises(ValueError):
            fr.results(c, "nope")
    assert [(r["week"], r["n"], r["baseline_mae"], r["matched_n"]) for r in got["rows"]] == [
        (8, 2, 3.0, 1)
    ]  # late week 9 excluded; 00-6 counts for the baseline only
    assert got["matched_total"] == {"n": 1, "baseline_mae": 2.0, "ridge_mae": 1.0}


def test_a_second_on_time_prediction_for_the_week_writes_nothing(
    eng: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(eng, 3, "on_time")
    monkeypatch.setattr(predict, "target_week", lambda sched, snap: (2026, 3))
    monkeypatch.setattr(predict, "status_of", lambda *a: "on_time")
    before = _count(eng)
    out = predict.run(eng)
    assert "already recorded" in out["skipped"] and _count(eng) == before


def _count(eng: Engine) -> int:
    with eng.connect() as c:
        n: int = c.execute(text("SELECT count(*) FROM forward_runs")).scalar_one()
        return n


def test_status_endpoint_reports_health(db: str) -> None:
    from fantasy_rag.api.main import app, get_conn

    eng = create_engine(db)
    _attempt(eng, "news", False, NOW - timedelta(minutes=2))
    _attempt(eng, "news", False, NOW - timedelta(minutes=1))

    def conn() -> Any:
        with eng.connect() as c:
            yield c

    from fastapi.testclient import TestClient

    app.dependency_overrides[get_conn] = conn
    try:
        body = TestClient(app).get("/api/status").json()
    finally:
        app.dependency_overrides.clear()
        eng.dispose()
    assert body["health"]["news"]["state"] == "failing"
    assert json.dumps(body)  # serializable
