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
from datetime import datetime
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


def target_week(sched: list[dict[str, Any]], snapshot: datetime) -> tuple[int, int]:
    """The week whose first game is the next one to kick off after the snapshot."""
    first: dict[tuple[int, int], datetime] = {}
    for g in sched:
        if g["game_type"] == "REG" and g["kickoff_at"] is not None:
            k = (g["season"], g["week"])
            first[k] = min(first.get(k, g["kickoff_at"]), g["kickoff_at"])
    return min(k for k, kick in first.items() if kick > snapshot)


def status_of(sched: list[dict[str, Any]], season: int, week: int, snapshot: datetime) -> str:
    cutoff = cutoffs(sched)[(season, week)]
    prev = [g["kickoff_at"] for g in sched if g["game_type"] == "REG" and g["kickoff_at"]
            and (g["season"], g["week"]) < (season, week)]  # fmt: skip
    if snapshot > cutoff:
        return "late"
    if prev and snapshot < max(prev) + LAG:
        return "early"
    return "on_time"


def _residual_offsets(
    rows: list[Row], preds: dict[tuple[str, int, int], dict[str, float | None]],
    formats: dict[str, ScoringRules], cutoff: datetime, positions: tuple[str, ...],
) -> dict[str, dict[str, list[float] | None]]:  # fmt: skip
    """key -> position -> [lower, upper] offsets from this policy's own errors on games
    usable at the cutoff (calibration season on)."""
    out: dict[str, dict[str, list[float] | None]] = {k: {} for k in formats}
    for pos in positions:
        mine = [r for r in rows if r.position == pos and r.label and r.season >= CALIBRATE
                and usable(r.label.kickoff, cutoff)]  # fmt: skip
        for key, rules in formats.items():
            res = []
            for r in mine:
                p = preds.get((r.player_id, r.season, r.week), {}).get(key)
                if p is not None:
                    assert r.label is not None
                    res.append(score(r.label.stats, pos, rules).points - p)
            off = ranges.offsets(res)
            out[key][pos] = list(off) if off else None
    return out


def forecast(
    games: list[Game],
    sched: list[dict[str, Any]],
    formats: dict[str, ScoringRules],
    snapshot: datetime,
) -> Forecast:
    """Everything the run writes, from inputs read at `snapshot` (pure: tests drive it)."""
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

    base_hist = {(r.player_id, r.season, r.week): dict(r.baseline) for r in labeled}
    out.preds["baseline"] = {r.player_id: {k: {"points": v} for k, v in r.baseline.items()}
                             for r in targets}  # fmt: skip
    out.ranges["baseline"] = _residual_offsets(labeled, base_hist, formats, cutoff, POSITIONS)

    spec = POLICIES["ridge"]
    out.preds["ridge"] = {}
    hist: dict[tuple[str, int, int], dict[str, float | None]] = {}
    for s in range(CALIBRATE, season + 1):  # out-of-sample errors for its own ranges
        hist |= run_policy("ridge", spec["params"], labeled, s, formats, spec["positions"])
    out.ranges["ridge"] = _residual_offsets(labeled, hist, formats, cutoff, spec["positions"])
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
            pts = points(r, comps, formats)
            fallback = r.games_used < MIN_GAMES
            out.preds["ridge"][r.player_id] = {
                k: {"points": v, "components": None if fallback else comps}
                for k, v in pts.items()}  # fmt: skip
    for policy, by_player in out.preds.items():
        for pid, by_key in by_player.items():
            pos = next(r.position for r in targets if r.player_id == pid)
            for key, v in by_key.items():
                off = out.ranges[policy][key].get(pos)
                p = v["points"]
                v["p10"], v["p90"] = (None, None) if p is None or off is None else (
                    round(p + off[0], 2), round(p + off[1], 2))  # fmt: skip
    return out


def scoring_rules(conn: Connection) -> tuple[dict[str, ScoringRules], dict[str, Any]]:
    """Presets plus every imported league's own rules (keys "ppr", "league:3", ...)."""
    raw: dict[str, Any] = {k: PRESETS[k] for k in ("ppr", "half", "std")}
    for r in conn.execute(text("SELECT id, scoring_settings FROM fantasy_leagues ORDER BY id")):
        raw[f"league:{r.id}"] = r.scoring_settings
    return {k: ScoringRules.from_sleeper(v) for k, v in raw.items()}, raw


def run(engine: Engine) -> dict[str, Any]:
    conn = engine.connect().execution_options(isolation_level="REPEATABLE READ")
    with conn, conn.begin():
        # The first statement fixes the snapshot; its clock time is the information cutoff.
        snapshot = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        games, sched = load(conn)
        formats, raw = scoring_rules(conn)
        sync = {r.source: {"id": r.id, "finished_at": r.finished_at} for r in conn.execute(text(
            "SELECT DISTINCT ON (source) source, id, finished_at FROM sync_runs"
            " WHERE status = 'ok' ORDER BY source, finished_at DESC"))}  # fmt: skip
    f = forecast(games, sched, formats, snapshot)
    stats_run = sync.get("nflverse.player_stats", {})
    with engine.begin() as w:
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
            "sc": json.dumps({k: {"weights": r.weights, "raw": raw[k]}
                              for k, r in formats.items()}),
            "r": json.dumps(f.ranges)}).scalar_one()  # fmt: skip
        w.execute(text(
            "INSERT INTO forward_inputs (run_id, player_id, position, features, games_used,"
            " latest_input) VALUES (:run, :p, :pos, :f, :g, :li)"),
            [{"run": run_id, "p": r.player_id, "pos": r.position, "f": json.dumps(r.features),
              "g": r.games_used, "li": r.latest_input} for r in f.rows])  # fmt: skip
        w.execute(text(
            "INSERT INTO projections (run_id, policy, scoring_key, player_id, points, p10, p90,"
            " components) VALUES (:run, :pol, :k, :p, :pts, :lo, :hi, :c)"),
            [{"run": run_id, "pol": pol, "k": k, "p": pid, "pts": v["points"], "lo": v["p10"],
              "hi": v["p90"], "c": json.dumps(v.get("components"))}
             for pol, by_player in f.preds.items() for pid, by_key in by_player.items()
             for k, v in by_key.items()])  # fmt: skip
    return {"run_id": run_id, "season": f.season, "week": f.week, "status": f.status,
            "snapshot_at": snapshot, "cutoff": f.cutoff, "players": len(f.rows),
            "newest_game": f.newest_game, "models": models}  # fmt: skip
