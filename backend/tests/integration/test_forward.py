"""Forward calibration: errors come from saved forward predictions once outcomes are final,
are recorded once, and never change; the seed is recorded once."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, text

from fantasy_rag.ml.predict import record_final, seed

pytestmark = pytest.mark.db
NOW = datetime.now(UTC)


def _week(eng: Engine, week: int, kickoff: datetime, status: str = "on_time") -> int:
    """A forward run for 2026 `week` with a ridge and a fallback row for 00-4, and the
    played game (00-4: 3 catches, 30 yards = 6.0 PPR)."""
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO schedules (game_id, season, week, game_type, home_team, away_team,"
            " kickoff_at, sync_run_id) VALUES (:g, 2026, :w, 'REG', 'WAS', 'DAL', :k, 1)"),
            {"g": f"g{week}", "w": week, "k": kickoff})  # fmt: skip
        c.execute(text(
            "INSERT INTO player_week_stats (season, week, season_type, player_id, game_id,"
            " opponent, stats, sync_run_id) VALUES (2026, :w, 'REG', '00-4', :g, 'DAL', :s, 1)"),
            {"w": week, "g": f"g{week}",
             "s": json.dumps({"receptions": 3, "receiving_yards": 30})})  # fmt: skip
        run = c.execute(text(
            """INSERT INTO forward_runs (season, week, cutoff_at, snapshot_at, predicted_at,
                   status, sync_runs, models, scoring, ranges)
               VALUES (2026, :w, :k, :k, :k, :st, '{}', '{}', :sc, '{}') RETURNING id"""),
            {"w": week, "k": kickoff - timedelta(days=2), "st": status,
             "sc": json.dumps({"ppr": {"raw": {"rec": 1, "rec_yd": 0.1}}})},
        ).scalar_one()  # fmt: skip
        c.execute(text("INSERT INTO forward_inputs (run_id, player_id, position, features,"
                       " games_used) VALUES (:r, '00-4', 'TE', '{}', 4)"), {"r": run})  # fmt: skip
        for policy, effective, pts in (("ridge", "ridge", 8.0), ("baseline", "baseline", 5.0)):
            c.execute(text(
                "INSERT INTO projections (run_id, policy, scoring_key, player_id, points,"
                " effective_policy) VALUES (:r, :p, 'ppr', '00-4', :pts, :e)"),
                {"r": run, "p": policy, "e": effective, "pts": pts})  # fmt: skip
    return run


def _errors(eng: Engine) -> dict[tuple[str, int], float]:
    with eng.connect() as c:
        rows = c.execute(text("SELECT policy, week, error FROM calibration_errors"))
        return {(r.policy, r.week): r.error for r in rows}


def test_final_weeks_add_their_errors_once_and_corrections_never_change_them(db: str) -> None:
    eng = create_engine(db)
    _week(eng, 3, NOW - timedelta(days=10))  # final: last game 10 days ago
    _week(eng, 4, NOW - timedelta(days=1))  # not final yet (stat corrections still open)
    _week(eng, 5, NOW - timedelta(days=9), status="late")  # never counted
    assert record_final(eng) == 2
    assert _errors(eng) == {("ridge", 3): -2.0, ("baseline", 3): 1.0}  # actual 6.0
    # A later stat correction: the recorded error stays as it was made.
    with eng.begin() as c:
        c.execute(text("UPDATE player_week_stats SET stats = '{\"receptions\": 9}' WHERE week = 3"))
    assert record_final(eng) == 0
    assert _errors(eng) == {("ridge", 3): -2.0, ("baseline", 3): 1.0}
    eng.dispose()


def test_a_fallback_row_adds_no_error_to_the_challenger(db: str) -> None:
    eng = create_engine(db)
    run = _week(eng, 3, NOW - timedelta(days=10))
    with eng.begin() as c:  # a second player whose Ridge row fell back to the baseline
        c.execute(text("INSERT INTO forward_inputs (run_id, player_id, position, features,"
                       " games_used) VALUES (:r, '00-6', 'TE', '{}', 1)"), {"r": run})  # fmt: skip
        c.execute(text(
            "INSERT INTO player_week_stats (season, week, season_type, player_id, game_id,"
            " opponent, stats, sync_run_id)"
            " VALUES (2026, 3, 'REG', '00-6', 'g3', 'DAL', '{}', 1)"))  # fmt: skip
        c.execute(text("INSERT INTO projections (run_id, policy, scoring_key, player_id, points,"
                       " effective_policy) VALUES (:r, 'ridge', 'ppr', '00-6', 1.0, 'baseline')"),
                  {"r": run})  # fmt: skip
    record_final(eng)
    with eng.connect() as c:
        who = c.execute(text("SELECT player_id FROM calibration_errors WHERE policy = 'ridge'"))
        assert [r.player_id for r in who] == ["00-4"]
    eng.dispose()


def test_the_seed_is_recorded_once(db: str) -> None:
    eng = create_engine(db)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO calibration_errors (policy, rules_fp, position, player_id, season,"
            " week, kickoff, error, source) VALUES ('baseline', 'abc', 'TE', '00-4', 2025, 1,"
            " now(), 1.5, 'seed')"))  # fmt: skip
    with pytest.raises(SystemExit, match="never rebuilt"):
        seed(eng)
    with eng.begin() as c, pytest.raises(Exception, match="insert-only"):
        c.execute(text("UPDATE calibration_errors SET error = 0"))
    eng.dispose()
