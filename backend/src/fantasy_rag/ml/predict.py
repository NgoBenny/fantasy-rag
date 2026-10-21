"""`fantasy-rag predict`: the Tuesday forward run (docs/phase8_plan.md, amendment).

Reads every input inside one REPEATABLE READ transaction whose first query records the
database clock: that time is the snapshot, the run's real information cutoff. Status:
`on_time` when the snapshot falls between the previous week's last kickoff + LAG and
the Tuesday 12:00 ET deadline, `late` after the deadline, `early` before the previous
week's games could be in. Baseline (every position) and TE Ridge are predicted from the
same saved inputs, with each policy's own range, and written insert-only.
"""

import hashlib
import json
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, text

from fantasy_rag.analytics.queries import PRESETS
from fantasy_rag.analytics.scoring import ScoringRules, score
from fantasy_rag.ml import baseline, ranges
from fantasy_rag.ml.benchmark import CALIBRATE, load, run_policy
from fantasy_rag.ml.features import (
    LAG,
    POSITIONS,
    Game,
    History,
    Row,
    build_row,
    cutoffs,
    rows_for_labels,
    team_week_games,
    usable,
)
from fantasy_rag.ml.train import MIN_GAMES, fit, points

# Policies logged every Tuesday, each from the same inputs. Ridge was tuned on 2023
# (alpha 100, model_runs "ridge-components-v1") and qualified for TE only.
POLICIES: dict[str, dict[str, Any]] = {
    "baseline": {"version": baseline.MODEL_VERSION, "positions": POSITIONS},
    "ridge": {"version": "ridge-components-v1", "positions": ("TE",), "params": {"alpha": 100.0}},
}


@dataclass
class Forecast:
    season: int
    week: int
    cutoff: datetime
    status: str
    newest_game: datetime | None
    rows: list[Row]
    preds: dict[str, dict[str, dict[str, Any]]]  # policy -> player -> {key: {points,...}}
    ranges: dict[str, dict[str, dict[str, list[float] | None]]]  # policy -> key -> position
    artifacts: dict[str, bytes] = field(default_factory=dict)  # policy -> pickled model


class NoUpcomingWeek(LookupError):
    """No regular-season game left to kick off: after week 18, until the next schedule."""


def target_week(sched: list[dict[str, Any]], snapshot: datetime) -> tuple[int, int]:
    """The week whose first game is the next one to kick off after the snapshot."""
    first: dict[tuple[int, int], datetime] = {}
    for g in sched:
        if g["game_type"] == "REG" and g["kickoff_at"] is not None:
            k = (g["season"], g["week"])
            first[k] = min(first.get(k, g["kickoff_at"]), g["kickoff_at"])
    upcoming = [k for k, kick in first.items() if kick > snapshot]
    if not upcoming:
        raise NoUpcomingWeek("no upcoming regular-season week")
    return min(upcoming)


def status_of(sched: list[dict[str, Any]], season: int, week: int, snapshot: datetime) -> str:
    cutoff = cutoffs(sched)[(season, week)]
    prev = [g["kickoff_at"] for g in sched if g["game_type"] == "REG" and g["kickoff_at"]
            and (g["season"], g["week"]) < (season, week)]  # fmt: skip
    if snapshot > cutoff:
        return "late"
    if prev and snapshot < max(prev) + LAG:
        return "early"
    return "on_time"


# (policy, rules fingerprint, position) -> [(game kickoff, actual - predicted)], frozen
Errors = dict[tuple[str, str, str], list[tuple[datetime, float]]]
FINAL_AFTER = timedelta(days=4)  # a week's outcomes are final 4 days after its last game


def offsets_at(
    errors: Errors, policy: str, rules: ScoringRules, pos: str, cutoff: datetime
) -> list[float] | None:
    """Range offsets from this policy's recorded errors under these exact rules, on games
    usable at the cutoff (a league that changed its rules starts a new pool)."""
    pool = errors.get((policy, rules.fingerprint(), pos), [])
    got = ranges.offsets([e for k, e in pool if usable(k, cutoff)])
    return list(got) if got else None


def forecast(
    games: list[Game],
    sched: list[dict[str, Any]],
    formats: dict[str, ScoringRules],
    snapshot: datetime,
    errors: Errors,
) -> Forecast:
    """Everything the run writes, from inputs read at `snapshot` (pure: tests drive it).
    Ranges use only the recorded calibration `errors`, never a rebuild from today's data."""
    season, week = target_week(sched, snapshot)
    cutoff = cutoffs(sched)[(season, week)]
    h = History(games, formats)
    labeled = rows_for_labels(h, sched, range(min(g.season for g in games), season + 1))
    games_of = team_week_games(sched)
    targets = []
    for pid, gs in h.players.items():
        n = h.prefix(pid, cutoff)
        last = gs[n - 1] if n else None
        if last and last.season == season and last.position in POSITIONS:
            targets.append(build_row(h, pid, last.position, season, week, cutoff,
                                     games_of.get((season, week, last.team))))  # fmt: skip
    newest = max((r.latest_input for r in targets if r.latest_input), default=None)
    out = Forecast(season, week, cutoff, status_of(sched, season, week, snapshot), newest,
                   targets, {}, {})  # fmt: skip

    out.preds["baseline"] = {
        r.player_id: {k: {"points": v, "effective": "baseline"} for k, v in r.baseline.items()}
        for r in targets
    }
    spec = POLICIES["ridge"]
    out.preds["ridge"] = {}
    for pos in spec["positions"]:
        train = [r for r in labeled if r.position == pos and r.label and r.games_used >= 1
                 and usable(r.label.kickoff, cutoff)]  # fmt: skip
        mine = [r for r in targets if r.position == pos]
        if not train or not mine:
            continue
        model = fit("ridge", spec["params"], train, pos)
        out.artifacts["ridge"] = pickle.dumps(model)
        predicted = model.predict(mine)
        for i, r in enumerate(mine):
            comps = {c: float(v[i]) for c, v in predicted.items()}
            # Too little history: the policy serves the baseline, and says so.
            effective = "baseline" if r.games_used < MIN_GAMES else "ridge"
            out.preds["ridge"][r.player_id] = {
                k: {"points": v, "effective": effective,
                    "components": comps if effective == "ridge" else None}
                for k, v in points(r, comps, formats).items()}  # fmt: skip
    position = {r.player_id: r.position for r in targets}
    for policy in POLICIES:
        out.ranges[policy] = {
            k: {p: offsets_at(errors, policy, r, p, cutoff) for p in POSITIONS}
            for k, r in formats.items()
        }
    for by_player in out.preds.values():
        for pid, by_key in by_player.items():
            for key, v in by_key.items():
                off = out.ranges[v["effective"]][key][position[pid]]
                p = v["points"]
                v["p10"], v["p90"] = (None, None) if p is None or off is None else (
                    round(p + off[0], 2), round(p + off[1], 2))  # fmt: skip
    return out


def seed_errors(
    games: list[Game], sched: list[dict[str, Any]], formats: dict[str, ScoringRules]
) -> list[dict[str, Any]]:
    """Out-of-sample errors from the calibration season to now, per policy as it serves:
    the baseline everywhere, Ridge only where it predicted (not its fallback rows)."""
    h = History(games, formats)
    seasons = range(min(g.season for g in games), max(g.season for g in games) + 1)
    labeled = rows_for_labels(h, sched, seasons)
    spec = POLICIES["ridge"]
    ridge: dict[tuple[str, int, int], dict[str, float | None]] = {}
    for s in range(CALIBRATE, seasons[-1] + 1):
        ridge |= run_policy("ridge", spec["params"], labeled, s, formats, spec["positions"])
    out = []
    for r in labeled:
        if r.season < CALIBRATE or r.games_used < 1 or r.label is None:
            continue
        preds = {"baseline": r.baseline}
        if r.position in spec["positions"] and r.games_used >= MIN_GAMES:
            preds["ridge"] = ridge.get((r.player_id, r.season, r.week), {})
        for policy, by_key in preds.items():
            for key, p in by_key.items():
                if p is not None:
                    actual = score(r.label.stats, r.position, formats[key]).points
                    out.append({"pol": policy, "fp": formats[key].fingerprint(),
                                "pos": r.position, "p": r.player_id,
                                "s": r.season, "w": r.week, "kick": r.label.kickoff,
                                "e": round(actual - p, 4)})  # fmt: skip
    return out


def seed(engine: Engine) -> int:
    """Record the calibration seed, once. A second seed would rebuild old errors from data
    corrected since, which the frozen plan forbids, so it is refused."""
    with engine.begin() as conn:
        if conn.execute(
            text("SELECT 1 FROM calibration_errors WHERE source = 'seed' LIMIT 1")
        ).first():
            raise SystemExit("the calibration seed is already recorded; it is never rebuilt")
        games, sched = load(conn)
        formats, _ = scoring_rules(conn)
        rows = seed_errors(games, sched, formats)
        # Leagues with identical rules share one fingerprint: count what was stored.
        stored = conn.execute(_INSERT_ERROR, [r | {"src": "seed", "run": None} for r in rows])
    return int(stored.rowcount)


_INSERT_ERROR = text(
    "INSERT INTO calibration_errors (policy, rules_fp, position, player_id, season, week,"
    " kickoff, error, source, run_id) VALUES (:pol, :fp, :pos, :p, :s, :w, :kick, :e, :src, :run)"
    " ON CONFLICT DO NOTHING"
)


# Each week's first on-time run, once the week's last game is FINAL_AFTER old.
_FINAL_RUNS = text(
    """SELECT DISTINCT ON (f.season, f.week) f.id, f.season, f.week, f.scoring
       FROM forward_runs f
       WHERE f.status = 'on_time'
         AND clock_timestamp() >= (SELECT max(kickoff_at) FROM schedules s
             WHERE s.season = f.season AND s.week = f.week AND s.game_type = 'REG') + :final
       ORDER BY f.season, f.week, f.snapshot_at"""
)
# That run's predictions the policy itself made, for players who played (with outcomes).
_PLAYED = text(
    """SELECT p.policy, p.scoring_key, p.player_id, p.points, i.position, sc.kickoff_at,
              s.stats || COALESCE(st.stats, '{}'::jsonb) AS stats
       FROM projections p
       JOIN forward_inputs i ON i.run_id = p.run_id AND i.player_id = p.player_id
       JOIN player_week_stats s ON s.player_id = p.player_id AND s.season = :s
            AND s.week = :w AND s.season_type = 'REG'
       JOIN schedules sc ON sc.game_id = s.game_id
       LEFT JOIN player_week_special_teams st ON st.player_id = s.player_id
            AND st.season = s.season AND st.week = s.week AND st.season_type = s.season_type
       WHERE p.run_id = :r AND p.effective_policy = p.policy AND p.points IS NOT NULL"""
)


def record_final(engine: Engine) -> int:
    """Score the saved predictions of each week's first on-time run once that week is final
    (FINAL_AFTER its last game, after the NFL's stat corrections). Insert-only: a recorded
    error never changes, and a player who didn't play adds none."""
    added = 0
    with engine.begin() as conn:
        runs = conn.execute(_FINAL_RUNS, {"final": FINAL_AFTER}).all()
        for run in runs:
            rules = {k: ScoringRules.from_sleeper(v["raw"]) for k, v in _obj(run.scoring).items()}
            played = conn.execute(_PLAYED, {"r": run.id, "s": run.season, "w": run.week})
            rows = [{"pol": x.policy, "fp": rules[x.scoring_key].fingerprint(),
                     "pos": x.position, "p": x.player_id,
                     "s": run.season, "w": run.week, "kick": x.kickoff_at, "src": "forward",
                     "run": run.id,
                     "e": round(score(_obj(x.stats), x.position, rules[x.scoring_key]).points
                                - x.points, 4)} for x in played]  # fmt: skip
            if rows:
                added += conn.execute(_INSERT_ERROR, rows).rowcount
    return added


def _obj(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def scoring_rules(conn: Connection) -> tuple[dict[str, ScoringRules], dict[str, Any]]:
    """Presets plus every imported league's own rules (keys "ppr", "league:3", ...)."""
    raw: dict[str, Any] = {k: PRESETS[k] for k in ("ppr", "half", "std")}
    for r in conn.execute(text("SELECT id, scoring_settings FROM fantasy_leagues ORDER BY id")):
        raw[f"league:{r.id}"] = r.scoring_settings
    return {k: ScoringRules.from_sleeper(v) for k, v in raw.items()}, raw


def _has_on_time(conn: Connection, season: int, week: int) -> bool:
    found = conn.execute(
        text("SELECT 1 FROM forward_runs WHERE season = :s AND week = :w AND status = 'on_time'"),
        {"s": season, "w": week},
    ).first()
    return found is not None


def run(engine: Engine) -> dict[str, Any]:
    """The Tuesday forward run. Caller must hold the STRUCTURED job lock (cli does)."""
    finalized = record_final(engine)
    conn = engine.connect().execution_options(isolation_level="REPEATABLE READ")
    with conn, conn.begin():
        # The first statement fixes the snapshot; its clock time is the information cutoff.
        snapshot = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        games, sched = load(conn)
        try:
            season, week = target_week(sched, snapshot)
        except NoUpcomingWeek as none_left:  # offseason: nothing to predict, not a failure
            return {"skipped": str(none_left)}
        if status_of(sched, season, week, snapshot) == "on_time" and _has_on_time(
            conn, season, week
        ):
            # Caller holds the STRUCTURED lock, so no other predict can be between check and insert.
            return {"skipped": f"an on-time run for {season} week {week} is already recorded"}
        formats, raw = scoring_rules(conn)
        sync = {r.source: {"id": r.id, "finished_at": r.finished_at} for r in conn.execute(text(
            "SELECT DISTINCT ON (source) source, id, finished_at FROM sync_runs"
            " WHERE status = 'ok' ORDER BY source, finished_at DESC"))}  # fmt: skip
        errors: Errors = {}
        found = conn.execute(
            text("SELECT policy, rules_fp, position, kickoff, error FROM calibration_errors")
        )
        for e in found:
            errors.setdefault((e.policy, e.rules_fp, e.position), []).append((e.kickoff, e.error))
    f = forecast(games, sched, formats, snapshot, errors)
    stats_run = sync.get("nflverse.player_stats", {})
    with engine.begin() as w:
        if f.status == "on_time" and _has_on_time(w, f.season, f.week):  # checked again at write
            return {"skipped": f"an on-time run for {f.season} week {f.week} is already recorded"}
        shas = {}
        for policy, body in f.artifacts.items():
            shas[policy] = hashlib.sha256(body).hexdigest()
            w.execute(text("INSERT INTO model_artifacts (sha256, name, body) VALUES (:s, :n, :b)"
                           " ON CONFLICT (sha256) DO NOTHING"),
                      {"s": shas[policy], "n": POLICIES[policy]["version"], "b": body})  # fmt: skip
        models = {p: {"version": spec["version"], "sha256": shas.get(p),
                      "positions": list(spec["positions"]), "params": spec.get("params")}
                  for p, spec in POLICIES.items()}  # fmt: skip
        run_id = w.execute(text(
            """INSERT INTO forward_runs (season, week, cutoff_at, snapshot_at, ingest_finished_at,
                   predicted_at, status, newest_game_kickoff, sync_runs, models, scoring, ranges)
               VALUES (:s, :w, :c, :snap, :ing, clock_timestamp(), :st, :new, :sync, :m, :sc, :r)
               RETURNING id"""), {
            "s": f.season, "w": f.week, "c": f.cutoff, "snap": snapshot,
            "ing": stats_run.get("finished_at"), "st": f.status, "new": f.newest_game,
            "sync": json.dumps(sync, default=str), "m": json.dumps(models),
            "sc": json.dumps({k: {"weights": r.weights, "raw": raw[k],
                                  "fingerprint": r.fingerprint()} for k, r in formats.items()}),
            "r": json.dumps(f.ranges)}).scalar_one()  # fmt: skip
        w.execute(text(
            "INSERT INTO forward_inputs (run_id, player_id, position, features, games_used,"
            " latest_input) VALUES (:run, :p, :pos, :f, :g, :li)"),
            [{"run": run_id, "p": r.player_id, "pos": r.position, "f": json.dumps(r.features),
              "g": r.games_used, "li": r.latest_input} for r in f.rows])  # fmt: skip
        w.execute(text(
            "INSERT INTO projections (run_id, policy, scoring_key, player_id, points, p10, p90,"
            " components, effective_policy) VALUES (:run, :pol, :k, :p, :pts, :lo, :hi, :c, :ef)"),
            [{"run": run_id, "pol": pol, "k": k, "p": pid, "pts": v["points"], "lo": v["p10"],
              "hi": v["p90"], "c": json.dumps(v.get("components")), "ef": v["effective"]}
             for pol, by_player in f.preds.items() for pid, by_key in by_player.items()
             for k, v in by_key.items()])  # fmt: skip
    return {"run_id": run_id, "season": f.season, "week": f.week, "status": f.status,
            "snapshot_at": snapshot, "cutoff": f.cutoff, "players": len(f.rows),
            "newest_game": f.newest_game, "models": models, "finalized_errors": finalized,
            "calibration_errors": sum(len(v) for v in errors.values())}  # fmt: skip
