"""Forward results: baseline vs TE Ridge on unseen 2026 weeks, and every Tuesday's status.

Errors come from calibration_errors rows recorded from each week's first on-time run
(predict.record_final), filtered to one scoring-rules fingerprint so PPR, half,
standard and league rules never mix. The comparison uses only player-weeks both
policies predicted (Ridge only where it really predicted, not its fallback rows).
"""

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from sqlalchemy import Connection, text

from fantasy_rag.ml.features import cutoffs
from fantasy_rag.ml.predict import scoring_rules, target_week

FIRST_FORWARD = (2026, 8)  # the first week with a valid Tuesday cutoff after Phase 8
WARN_BEFORE = timedelta(minutes=30)


def _schedule(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        "SELECT season, week, game_type, kickoff_at FROM schedules WHERE season >= :s"),
        {"s": FIRST_FORWARD[0]})  # fmt: skip
    return [dict(r._mapping) for r in rows]


def tuesdays(conn: Connection, now: datetime) -> list[dict[str, Any]]:
    """Every week since the first forward week whose deadline has passed, with exactly one
    status: on_time, late (only late runs), early_only (only early runs; never a success)
    or missing (no run)."""
    statuses: dict[tuple[int, int], set[str]] = defaultdict(set)
    for r in conn.execute(text("SELECT season, week, status FROM forward_runs")):
        statuses[(r.season, r.week)].add(r.status)
    out = []
    for (s, w), deadline in sorted(cutoffs(_schedule(conn)).items()):
        if (s, w) < FIRST_FORWARD or deadline > now:
            continue
        got = statuses.get((s, w), set())
        status = ("on_time" if "on_time" in got else "late" if "late" in got
                  else "early_only" if "early" in got else "missing")  # fmt: skip
        out.append({"season": s, "week": w, "deadline": deadline, "status": status})
    return out


def check(conn: Connection, now: datetime) -> tuple[bool, str]:
    """Pre-deadline check: False when the upcoming week has no on-time run and its
    deadline is less than WARN_BEFORE away (or has passed)."""
    sched = _schedule(conn)
    season, week = target_week(sched, now)
    deadline = cutoffs(sched)[(season, week)]
    on_time = conn.execute(text(
        "SELECT 1 FROM forward_runs WHERE season = :s AND week = :w AND status = 'on_time'"),
        {"s": season, "w": week}).first()  # fmt: skip
    if on_time:
        return True, f"{season} week {week}: on-time forward run recorded"
    if now < deadline - WARN_BEFORE:
        return True, f"{season} week {week}: not due yet (deadline {deadline:%a %H:%M %Z})"
    late = "has passed" if now >= deadline else f"is {deadline - now} away"
    return False, f"{season} week {week}: NO on-time forward run, and the deadline {late}"


def results(conn: Connection, scoring: str = "ppr") -> dict[str, Any]:
    """Per week and position, under one scoring-rules fingerprint: the baseline's MAE on all
    its scored player-weeks, and baseline vs Ridge on the player-weeks both predicted."""
    formats, _ = scoring_rules(conn)
    if scoring not in formats:
        raise ValueError(f"unknown scoring {scoring!r}; one of {sorted(formats)}")
    fp = formats[scoring].fingerprint()
    errs: dict[tuple[str, int, int, str], dict[str, float]] = defaultdict(dict)
    for r in conn.execute(text(
        """SELECT e.policy, e.player_id, e.season, e.week, e.position, e.error
           FROM calibration_errors e JOIN forward_runs f ON f.id = e.run_id
           WHERE e.source = 'forward' AND f.status = 'on_time' AND e.rules_fp = :fp"""),
        {"fp": fp}):  # fmt: skip
        errs[(r.player_id, r.season, r.week, r.position)][r.policy] = abs(r.error)
    groups: dict[tuple[int, int, str], list[dict[str, float]]] = defaultdict(list)
    for (_, s, w, pos), by_policy in errs.items():
        groups[(s, w, pos)].append(by_policy)
    rows = []
    for (s, w, pos), items in sorted(groups.items()):
        base = [i["baseline"] for i in items if "baseline" in i]
        both = [i for i in items if "baseline" in i and "ridge" in i]
        rows.append({
            "season": s, "week": w, "position": pos, "n": len(base),
            "baseline_mae": round(mean(base), 3) if base else None, "matched_n": len(both),
            "matched_baseline_mae": round(mean(i["baseline"] for i in both), 3) if both else None,
            "matched_ridge_mae": round(mean(i["ridge"] for i in both), 3) if both else None,
        })  # fmt: skip
    matched = [i for items in groups.values() for i in items if "baseline" in i and "ridge" in i]
    return {
        "scoring": scoring,
        "rows": rows,
        "matched_total": {
            "n": len(matched),
            "baseline_mae": round(mean(i["baseline"] for i in matched), 3) if matched else None,
            "ridge_mae": round(mean(i["ridge"] for i in matched), 3) if matched else None,
        },
    }
