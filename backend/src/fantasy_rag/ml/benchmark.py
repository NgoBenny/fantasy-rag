"""`fantasy-rag benchmark`: the frozen Phase 8 comparison (docs/phase8_plan.md).

Rolling origin, retrained every week: for each target week, every policy is fit only on
games usable at that week's cutoff and predicts that week. Tuning on 2023 picks each
model's hyperparameters; 2024 seeds the range calibration; 2025 is the benchmark that
the frozen promotion rule reads. Every policy is scored on the same rows and pairs.
"""

import json
import random
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, time
from itertools import combinations
from statistics import mean
from typing import Any

from scipy.stats import spearmanr
from sqlalchemy import Connection, Engine, text

from fantasy_rag.analytics.queries import PRESETS
from fantasy_rag.analytics.scoring import ScoringRules, score
from fantasy_rag.ml import ranges
from fantasy_rag.ml.features import ET, LAG, POSITIONS, Game, History, Row, rows_for_labels, usable
from fantasy_rag.ml.train import LGBM_GRID, RIDGE_GRID, fit_predict, points

FORMATS = {f: ScoringRules.from_sleeper(PRESETS[f]) for f in ("ppr", "half", "std")}
RELEVANT = {"QB": 24, "RB": 48, "WR": 60, "TE": 24}  # start/sit pool per (week, position)
POLICIES = ("baseline", "ridge", "lgbm")
TUNE, CALIBRATE, BENCHMARK = 2023, 2024, 2025
Preds = dict[tuple[str, int, int], dict[str, float | None]]  # (player, season, week) -> fmt


def load(conn: Connection) -> tuple[list[Game], list[dict[str, Any]]]:
    """Every regular-season game since 2022 with snap share, and the schedule."""
    sched = [dict(r._mapping) for r in conn.execute(text(
        "SELECT game_id, season, week, game_type, kickoff_at, home_team, away_team"
        " FROM schedules WHERE season >= 2022"))]  # fmt: skip
    rows = conn.execute(text(
        """
        SELECT s.player_id, p.position, s.season, s.week, s.game_id, s.team, s.opponent,
               sc.kickoff_at, s.stats, n.offense_pct
        FROM player_week_stats s
        JOIN players p ON p.id = s.player_id
        JOIN schedules sc ON sc.game_id = s.game_id
        LEFT JOIN snap_counts n ON n.player_id = s.player_id AND n.season = s.season
             AND n.week = s.week AND n.game_type = 'REG'
        WHERE s.season_type = 'REG' AND s.season >= 2022
        """))  # fmt: skip
    games = [Game(r.player_id, r.position or "", r.season, r.week, r.game_id, r.team,
                  r.opponent, r.kickoff_at, r.stats, r.offense_pct) for r in rows]  # fmt: skip
    return games, sched


def run_policy(
    kind: str,
    params: dict[str, Any],
    rows: list[Row],
    season: int,
    formats: dict[str, ScoringRules] = FORMATS,
    positions: tuple[str, ...] = POSITIONS,
) -> Preds:
    """Predictions for every evaluated row of `season`, one cutoff at a time."""
    out: Preds = {}
    for (s, w), test in sorted(_by_week(rows, season).items()):
        cutoff = test[0].cutoff
        train = [r for r in rows if r.label and usable(r.label.kickoff, cutoff)
                 and r.games_used >= 1]  # fmt: skip
        # Leakage guards: labels and feature inputs both predate the cutoff.
        assert all(r.label and r.label.kickoff <= cutoff - LAG for r in train)
        assert all(r.latest_input is None or r.latest_input <= cutoff - LAG for r in test)
        for pos in positions:
            t_pos = [r for r in test if r.position == pos]
            if not t_pos:
                continue
            tr = [r for r in train if r.position == pos]
            # No usable training rows yet: no model, so the policy falls back to the baseline.
            model = kind != "baseline" and bool(tr)
            fitted = fit_predict(kind, params, tr, t_pos, pos) if model else {}
            for i, r in enumerate(t_pos):
                pred = {c: float(v[i]) for c, v in fitted.items()}
                got = points(r, pred, formats) if model else dict(r.baseline)
                out[(r.player_id, s, w)] = got
    return out


def _by_week(rows: list[Row], season: int) -> dict[tuple[int, int], list[Row]]:
    """The comparison set: player-weeks played, with at least one usable earlier game."""
    weeks: dict[tuple[int, int], list[Row]] = defaultdict(list)
    for r in rows:
        if r.season == season and r.games_used >= 1:
            weeks[(r.season, r.week)].append(r)
    return weeks


_ACTUAL: dict[tuple[str, int, int, str], float] = {}


def actual(r: Row, fmt: str) -> float:
    key = (*_key(r), fmt)
    if key not in _ACTUAL:
        assert r.label is not None
        _ACTUAL[key] = score(r.label.stats, r.position, FORMATS[fmt]).points
    return _ACTUAL[key]


def _key(r: Row) -> tuple[str, int, int]:
    return (r.player_id, r.season, r.week)


def pairs(rows: list[Row]) -> dict[tuple[int, str], list[tuple[Row, Row]]]:
    """Frozen start/sit pairs: top N per (week, position) by points per game over usable
    earlier games (model-free, known at the cutoff)."""
    groups: dict[tuple[int, str], list[Row]] = defaultdict(list)
    for r in rows:
        if r.features["ppg_ppr"] is not None:
            groups[(r.week, r.position)].append(r)
    out = {}
    for (w, pos), rs in groups.items():
        top = sorted(rs, key=lambda r: (-(r.features["ppg_ppr"] or 0.0), r.player_id))
        out[(w, pos)] = list(combinations(top[: RELEVANT[pos]], 2))
    return out


def by_week(
    rows: list[Row], preds: Preds, fmt: str, prs: dict[Any, list[Any]] | None = None
) -> dict[int, dict[str, Any]]:
    """Per-week sums the metrics (and the week bootstrap) are built from; `prs` overrides
    the start/sit pairs (the FantasyPros subset keeps the frozen pairs it covers)."""
    out: dict[int, dict[str, Any]] = {}
    prs = pairs(rows) if prs is None else prs
    groups: dict[tuple[int, str], list[Row]] = defaultdict(list)
    for r in rows:
        groups[(r.week, r.position)].append(r)
    for (w, pos), rs in groups.items():
        wk = out.setdefault(w, {"err": 0.0, "n": 0, "rhos": [], "hit": 0, "total": 0})
        got = [(p, actual(r, fmt)) for r in rs if (p := preds[_key(r)][fmt]) is not None]
        wk["err"] += sum(abs(p - a) for p, a in got)
        wk["n"] += len(got)
        if len(got) >= 5:
            rho = spearmanr([p for p, _ in got], [a for _, a in got]).statistic
            if rho == rho:  # NaN when a side is constant
                wk["rhos"].append(float(rho))
        for a, b in prs.get((w, pos), []):
            aa, ab = actual(a, fmt), actual(b, fmt)
            if aa == ab:  # no right answer: dropped for every model alike
                continue
            pa, pb = preds[_key(a)][fmt], preds[_key(b)][fmt]
            wk["total"] += 1
            # A tied (or missing) prediction picks neither: half credit, never a dropped pair.
            if pa is None or pb is None or pa == pb:
                wk["hit"] += 0.5
            else:
                wk["hit"] += (pa > pb) == (aa > ab)
    return out


def summarize(weeks: list[dict[str, Any]]) -> dict[str, Any]:
    n, total = sum(w["n"] for w in weeks), sum(w["total"] for w in weeks)
    rhos = [x for w in weeks for x in w["rhos"]]
    return {"n": n, "mae": _r(sum(w["err"] for w in weeks) / n) if n else None,
            "spearman": _r(mean(rhos)) if rhos else None, "spearman_groups": len(rhos),
            "start_sit": _r(sum(w["hit"] for w in weeks) / total) if total else None,
            "start_sit_pairs": total}  # fmt: skip


def metrics(rows: list[Row], preds: Preds, fmt: str) -> dict[str, Any]:
    return summarize(list(by_week(rows, preds, fmt).values()))


def mae(rows: list[Row], preds: Preds, fmt: str) -> float | None:
    m: float | None = metrics(rows, preds, fmt)["mae"]
    return m


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 4)


def bootstrap(
    rows: list[Row], preds: Preds, base: Preds, fmt: str, reps: int = 1000, seed: int = 0
) -> dict[str, list[float | None]]:
    """95% intervals of (policy - baseline) for MAE, Spearman and start/sit accuracy,
    resampling whole weeks (both policies see the same resampled weeks)."""
    a, b = by_week(rows, preds, fmt), by_week(rows, base, fmt)
    weeks = sorted(a)
    rng = random.Random(seed)
    diffs: dict[str, list[float]] = {"mae": [], "spearman": [], "start_sit": []}
    for _ in range(reps):
        pick = [rng.choice(weeks) for _ in weeks]
        sa, sb = summarize([a[w] for w in pick]), summarize([b[w] for w in pick])
        for k in diffs:
            if sa[k] is not None and sb[k] is not None:
                diffs[k].append(sa[k] - sb[k])
    return {k: _ci(v) for k, v in diffs.items()}


def _ci(v: list[float]) -> list[float | None]:
    if not v:
        return [None, None]
    s = sorted(v)
    return [round(s[int(0.025 * (len(s) - 1))], 4), round(s[int(0.975 * (len(s) - 1))], 4)]


def range_offsets(
    rows: list[Row], preds: Preds, fmt: str
) -> dict[tuple[str, int, int], tuple[float, float] | None]:
    """Each predicted row's range offsets, from this policy's own residuals (calibration
    season on) on games usable at that row's cutoff."""
    out: dict[tuple[str, int, int], tuple[float, float] | None] = {}
    for pos in POSITIONS:
        mine = sorted(
            (r for r in rows if r.position == pos and r.season >= CALIBRATE and r.label
             and preds.get(_key(r), {}).get(fmt) is not None),
            key=lambda r: r.label.kickoff,  # type: ignore[union-attr]
        )  # fmt: skip
        kicks = [r.label.kickoff for r in mine]  # type: ignore[union-attr]
        res = [actual(r, fmt) - preds[_key(r)][fmt] for r in mine]  # type: ignore[operator]
        cache: dict[Any, tuple[float, float] | None] = {}
        for r in mine:
            if r.cutoff not in cache:
                cache[r.cutoff] = ranges.offsets(res[: bisect_right(kicks, r.cutoff - LAG)])
            out[_key(r)] = cache[r.cutoff]
    return out


def calibrate(rows: list[Row], preds: Preds, fmt: str, eval_season: int) -> dict[str, Any]:
    """Coverage and width of the eval season's ranges, by position."""
    offs = range_offsets(rows, preds, fmt)
    by_pos: dict[str, dict[str, Any]] = {}
    for pos in POSITIONS:
        hits = n = no_range = 0
        width = 0.0
        for r in rows:
            if r.position != pos or r.season != eval_season or _key(r) not in offs:
                continue
            off = offs[_key(r)]
            if off is None:
                no_range += 1
                continue
            p = preds[_key(r)][fmt]
            assert p is not None
            n += 1
            hits += p + off[0] <= actual(r, fmt) <= p + off[1]
            width += off[1] - off[0]
        by_pos[pos] = {"n": n, "coverage": _r(hits / n) if n else None,
                       "width": _r(width / n) if n else None, "no_range": no_range}  # fmt: skip
    return by_pos


def promoted(pos_metrics: dict[str, Any], base: dict[str, Any], ci: dict[str, Any]) -> bool:
    """The frozen rule: MAE >= 2% lower with its 95% interval below zero, and Spearman and
    start/sit accuracy both higher (point estimates)."""
    m, b = pos_metrics, base
    if None in (m["mae"], b["mae"], m["spearman"], b["spearman"], m["start_sit"], b["start_sit"]):
        return False
    hi = ci["mae"][1]
    return bool(
        m["mae"] <= 0.98 * b["mae"] and hi is not None and hi < 0
        and m["spearman"] > b["spearman"] and m["start_sit"] > b["start_sit"]
    )  # fmt: skip


def decide(report: dict[str, Any]) -> dict[str, Any]:
    """Per position: the frozen PPR rule picks a winner (lower MAE if both qualify); a
    winner whose half or standard MAE is worse than the baseline's stays on the baseline."""
    out: dict[str, Any] = {}
    ppr = report["formats"]["ppr"]
    for pos in POSITIONS:
        wins = [k for k in ("ridge", "lgbm") if promoted(
            ppr[k]["by_position"][pos], ppr["baseline"]["by_position"][pos],
            ppr[k]["vs_baseline_ci"][pos])]  # fmt: skip
        best = min(wins, key=lambda k: ppr[k]["by_position"][pos]["mae"], default=None)
        worse = [f for f in ("half", "std") if best and (
            report["formats"][f][best]["by_position"][pos]["mae"]
            > report["formats"][f]["baseline"]["by_position"][pos]["mae"])]  # fmt: skip
        out[pos] = {"winner": None if worse else best, "ppr_qualified": wins,
                    "blocked_by_formats": worse}  # fmt: skip
    return out


def tune(rows: list[Row], log: Callable[[str], None]) -> dict[str, dict[str, Any]]:
    """Each model's hyperparameters by 2023 PPR MAE (the plan's tuning window)."""
    eval_rows = [r for rs in _by_week(rows, TUNE).values() for r in rs]
    chosen = {}
    for kind, grid in (("ridge", RIDGE_GRID), ("lgbm", LGBM_GRID)):
        scored = []
        for params in grid:
            m = mae(eval_rows, run_policy(kind, params, rows, TUNE), "ppr")
            assert m is not None
            log(f"tune {kind} {params}: 2023 PPR MAE {m:.4f}")
            scored.append((float(m), params))
        chosen[kind] = min(scored, key=lambda s: s[0])[1]
    return chosen


def predict_all(
    engine: Engine, log: Callable[[str], None] = print
) -> tuple[list[Row], dict[str, Preds], dict[str, Any]]:
    """The expensive part (tuning and weekly retraining); saved so reports can be redone."""
    with engine.connect() as conn:
        games, sched = load(conn)
    rows = rows_for_labels(History(games, FORMATS), sched, range(2022, 2026))
    log(f"{len(rows)} player-week rows featurized")
    params = tune(rows, log)
    preds: dict[str, Preds] = {}
    for kind in POLICIES:
        preds[kind] = {}
        for season in (CALIBRATE, BENCHMARK):
            preds[kind] |= run_policy(kind, params.get(kind, {}), rows, season)
        log(f"{kind}: predicted {len(preds[kind])} rows")
    return rows, preds, params


def report(
    engine: Engine,
    rows: list[Row],
    preds: dict[str, Preds],
    params: dict[str, Any],
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    bench = [r for rs in _by_week(rows, BENCHMARK).values() for r in rs]
    out: dict[str, Any] = {"params": params, "formats": {}}
    for fmt in FORMATS:
        per: dict[str, Any] = {}
        for kind in POLICIES:
            per[kind] = {"all": metrics(bench, preds[kind], fmt),
                         "by_position": {p: metrics([r for r in bench if r.position == p],
                                                    preds[kind], fmt) for p in POSITIONS},
                         "ranges": calibrate(rows, preds[kind], fmt, BENCHMARK)}  # fmt: skip
            if kind != "baseline":
                per[kind]["vs_baseline_ci"] = {
                    p: bootstrap([r for r in bench if r.position == p], preds[kind],
                                 preds["baseline"], fmt) for p in POSITIONS}  # fmt: skip
        out["formats"][fmt] = per
        log(f"{fmt}: " + ", ".join(f"{k} MAE {per[k]['all']['mae']}" for k in POLICIES))
    out["fantasypros"] = fantasypros(engine, bench, preds, rows)
    out["promotion"] = decide(out)
    return out


def fp_ranks(engine: Engine, cut: dict[tuple[int, int], Any]) -> dict[tuple[str, int, int], float]:
    """(player, season, week) -> FantasyPros weekly ECR (PPR pages), scraped Fridays: the
    week whose cutoff precedes the scrape date and whose next cutoff follows it."""
    import nflreadpy as nfl
    import polars as pl

    df = nfl.load_ff_rankings(type="all").filter(
        pl.col("page_type").is_in(["weekly-qb", "weekly-rb", "weekly-wr", "weekly-te"])
        & pl.col("scrape_date").str.slice(0, 4).cast(pl.Int32).is_between(2022, 2025)
    )
    with engine.connect() as conn:
        found = conn.execute(text(
            "SELECT external_id, player_id FROM player_external_ids WHERE provider = 'fantasypros'"
        ))  # fmt: skip
        to_gsis: dict[str, str] = {r.external_id: r.player_id for r in found}
    weeks = sorted(cut.items(), key=lambda kv: kv[1])
    out = {}
    for fp_id, day, ecr in df.select("id", "scrape_date", "ecr").iter_rows():
        pid = to_gsis.get(str(fp_id))
        noon = datetime.combine(date.fromisoformat(day), time(12), ET)
        week = next((k for (k, c), (_, nxt) in zip(weeks, weeks[1:], strict=False)
                     if c < noon < nxt), None)  # fmt: skip
        if pid and week and ecr is not None:
            out[(pid, *week)] = -float(ecr)  # lower ECR = better: negate to rank like points
    return out


def fantasypros(
    engine: Engine, bench: list[Row], preds: dict[str, Preds], rows: list[Row]
) -> dict[str, Any]:
    """Ranking metrics on the FantasyPros-ranked subset only, for every comparator alike
    (FantasyPros scraped on Fridays: three more days of news than our Tuesday cutoff)."""
    ranks = fp_ranks(engine, {(r.season, r.week): r.cutoff for r in rows})
    subset = [r for r in bench if _key(r) in ranks]
    keep = {_key(r) for r in subset}
    prs = {k: [(a, b) for a, b in ps if _key(a) in keep and _key(b) in keep]
           for k, ps in pairs(bench).items()}  # fmt: skip
    fp: Preds = {k: {"ppr": v} for k, v in ranks.items()}
    out: dict[str, Any] = {"rows": len(subset), "of": len(bench)}
    for name, p in [("fantasypros", fp), *preds.items()]:
        m = summarize(list(by_week(subset, p, "ppr", prs).values()))
        out[name] = {
            k: m[k] for k in ("spearman", "spearman_groups", "start_sit", "start_sit_pairs")
        }
    return out


def write_runs(engine: Engine, report: dict[str, Any]) -> None:
    with engine.begin() as conn:
        for kind in ("ridge", "lgbm"):
            conn.execute(text(
                """
                INSERT INTO model_runs (model_version, training_window, features, metrics)
                VALUES (:v, :w, :f, :m)
                ON CONFLICT (model_version) DO UPDATE SET
                    training_window = EXCLUDED.training_window, features = EXCLUDED.features,
                    metrics = EXCLUDED.metrics, created_at = now()
                """), {
                "v": f"{kind}-components-v1",
                "w": json.dumps({"tune": TUNE, "calibrate": CALIBRATE, "benchmark": BENCHMARK,
                                 "cutoff": "Tuesday 12:00 ET", "params": report["params"][kind]}),
                "f": ["per-component ewm/last3/season means", "snap share", "target/carry share",
                      "opponent PPR allowed", "home", "baseline_ppr"],
                "m": json.dumps({f: {k: report["formats"][f][k] for k in ("baseline", kind)}
                                 for f in report["formats"]}
                                | {k: report[k] for k in ("promotion", "fantasypros")}),
            })  # fmt: skip
