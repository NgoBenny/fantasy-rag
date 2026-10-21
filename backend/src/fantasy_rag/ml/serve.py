"""The one place projections are served from (tools, start/sit, waivers, lineup, chat).

Per position, `ACTIVE` names the policy serving it; anything absent is the baseline,
computed live as before. A non-baseline policy is read from the latest forward run for
the week (its Tuesday prediction) when that run used the same scoring rules; otherwise
the player falls back to the baseline. Every projection carries the range calibrated on
the policy that produced it (the baseline's own errors for baseline projections), or
none when there is no forward run with matching rules.
"""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from fantasy_rag.analytics import queries as q
from fantasy_rag.analytics.scoring import ScoringRules
from fantasy_rag.ml import baseline

# shortcut: serving policy is a reviewed code change, not a setting; empty = baseline
# everywhere. Activating TE Ridge is {"TE": "ridge"}, after its serving code is reviewed.
ACTIVE: dict[str, str] = {}


@dataclass(frozen=True)
class Served:
    points: float
    games_used: int
    policy: str
    model_version: str
    range_80: tuple[float, float] | None


def scoring_key(league_id: int | None, scoring: str | None) -> str:
    return f"league:{league_id}" if league_id is not None else (scoring or "ppr")


def _latest_run(conn: Connection, season: int, week: int) -> Any:
    return conn.execute(
        text(
            "SELECT id, models, scoring, ranges FROM forward_runs WHERE season = :s"
            " AND week = :w ORDER BY snapshot_at DESC, id DESC LIMIT 1"
        ),
        {"s": season, "w": week},
    ).one_or_none()


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def project(
    conn: Connection,
    refs: list[q.PlayerRef],
    rules: ScoringRules,
    key: str,
    season: int,
    week: int,
) -> dict[str, Served | None]:
    """player ID -> projection for (season, week), or None without enough history."""
    ids = [r.id for r in refs]
    history = baseline.scored_history(q.weekly_rows(conn, ids, [season - 1, season]), rules)
    run = _latest_run(conn, season, week)
    scoring = _json(run.scoring) if run else {}
    # A run made under different rules (league changed them since) says nothing here.
    same_rules = run is not None and (scoring.get(key) or {}).get("weights") == rules.weights
    ranges_ = _json(run.ranges) if same_rules else {}
    logged: dict[tuple[str, str], Any] = {}
    if same_rules and any(ACTIVE.get(r.position or "") for r in refs):
        for row in conn.execute(
            text(
                "SELECT policy, player_id, points, p10, p90 FROM projections WHERE run_id = :r"
                " AND scoring_key = :k AND player_id = ANY(:ids)"
            ),
            {"r": run.id, "k": key, "ids": ids},
        ):
            logged[(row.policy, row.player_id)] = row
    models = _json(run.models) if run else {}
    out: dict[str, Served | None] = {}
    for ref in refs:
        pos = ref.position or ""
        base = baseline.project(history.get(ref.id, []), season, week)
        policy = ACTIVE.get(pos)
        got = logged.get((policy, ref.id)) if policy else None
        if got is not None and got.points is not None:
            rng = None if got.p10 is None else (got.p10, got.p90)
            n = base[1] if base else 0
            out[ref.id] = Served(got.points, n, policy or "", models[policy]["version"], rng)
            continue
        if base is None:
            out[ref.id] = None
            continue
        off = ((ranges_.get("baseline") or {}).get(key) or {}).get(pos)
        rng = None if off is None else (round(base[0] + off[0], 2), round(base[0] + off[1], 2))
        out[ref.id] = Served(base[0], base[1], "baseline", baseline.MODEL_VERSION, rng)
    return out
