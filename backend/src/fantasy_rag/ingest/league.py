"""Import Sleeper leagues (read-only): settings, scoring rules, rosters and weekly matchups."""

import json
from typing import Any

import polars as pl
from sqlalchemy import Engine, text

from fantasy_rag.analytics.scoring import ScoringRules
from fantasy_rag.ingest.base import Run, record_unresolved, sync_run, upsert
from fantasy_rag.ingest.nflverse import resolve


def league_ids(api: Any, username: str | None, league_id: str | None, season: str) -> list[str]:
    if league_id:
        return [league_id]
    user = api.get(f"/user/{username}")
    if not user:
        raise ValueError("Sleeper username not found")
    return [
        lg["league_id"] for lg in api.get(f"/user/{user['user_id']}/leagues/nfl/{season}") or []
    ]


def league_row(league: dict[str, Any]) -> pl.DataFrame:
    rules = ScoringRules.from_sleeper(league.get("scoring_settings") or {})
    return pl.DataFrame(
        [
            {
                "provider": "sleeper",
                "external_league_id": str(league["league_id"]),
                "season": int(league["season"]),
                "name": league.get("name"),
                "scoring_settings": json.dumps(
                    league.get("scoring_settings") or {}, sort_keys=True
                ),
                "scoring_rules": rules.model_dump_json(),
                "roster_positions": json.dumps(league.get("roster_positions") or []),
                "settings": json.dumps(league.get("settings") or {}, sort_keys=True),
            }
        ]
    )


def roster_rows(
    league_pk: int,
    week: int,
    matchups: list[dict[str, Any]],
    owners: dict[int, str | None],
    idmap: pl.DataFrame,
    known: set[str],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """One row per roster for `week`. Returns (rows, unresolved Sleeper IDs)."""
    rows, misses = [], []
    for m in matchups:
        ids = [str(p) for p in m.get("players") or []]
        starters = [str(p) for p in m.get("starters") or [] if str(p) != "0"]
        players = [p for p in ids if p.isdigit()]  # team defenses ("DET") are not players
        frame = pl.DataFrame({"sleeper_id": players}, schema={"sleeper_id": pl.String})
        resolved, unresolved = resolve(frame, [("sleeper_id", "sleeper")], idmap, known)
        to_gsis = dict(resolved.select("sleeper_id", "player_id").iter_rows())
        misses.append(unresolved)
        rows.append(
            {
                "league_id": league_pk,
                "roster_id": int(m["roster_id"]),
                "week": week,
                "owner_name": owners.get(int(m["roster_id"])),
                "matchup_id": m.get("matchup_id"),
                "points": m.get("points"),
                "player_ids": "{" + ",".join(to_gsis[p] for p in players if p in to_gsis) + "}",
                "starter_ids": "{" + ",".join(to_gsis[p] for p in starters if p in to_gsis) + "}",
                "raw": json.dumps(
                    {k: m.get(k) for k in ("players", "starters", "players_points", "points")},
                    sort_keys=True,
                ),
            }
        )
    unresolved = pl.concat(misses) if misses else pl.DataFrame()
    return pl.DataFrame(rows), unresolved.unique() if unresolved.height else unresolved


REGULAR_SEASON_WEEKS = 18  # 2021 onward; fantasy playoffs end by week 17 or 18


def weeks_to_import(league_season: int, state: dict[str, Any]) -> int:
    """Last week to fetch: elapsed weeks this season, all weeks for a past season."""
    current = int(state["season"])
    if league_season < current:
        return REGULAR_SEASON_WEEKS
    if league_season == current:
        return {"regular": int(state["week"]), "post": REGULAR_SEASON_WEEKS}.get(
            str(state.get("season_type")), 0
        )
    return 0  # a future season has no matchups yet


def import_leagues(
    engine: Engine,
    api: Any,
    username: str | None,
    league_id: str | None,
    season: str | None = None,
) -> list[dict[str, Any]]:
    state = api.get("/state/nfl")
    summaries = []
    for lid in league_ids(api, username, league_id, season or str(state["season"])):
        with sync_run(engine, "sleeper.league") as run:
            summaries.append(_import_one(run, api, lid, state))
    return summaries


def _import_one(run: Run, api: Any, lid: str, state: dict[str, Any]) -> dict[str, Any]:
    conn = run.conn
    league = api.get(f"/league/{lid}")
    last_week = weeks_to_import(int(league["season"]), state)
    changed = upsert(
        conn, "fantasy_leagues", league_row(league), ["provider", "external_league_id"], run.id
    )
    league_pk = conn.execute(
        text(
            "SELECT id FROM fantasy_leagues WHERE provider = 'sleeper' AND external_league_id = :l"
        ),
        {"l": lid},
    ).scalar_one()

    users = {u["user_id"]: u.get("display_name") for u in api.get(f"/league/{lid}/users") or []}
    owners = {
        int(r["roster_id"]): users.get(r.get("owner_id"))
        for r in api.get(f"/league/{lid}/rosters") or []
    }

    idmap = pl.DataFrame(
        conn.execute(
            text("SELECT provider, external_id, player_id FROM player_external_ids")
        ).fetchall(),
        schema={"provider": pl.String, "external_id": pl.String, "player_id": pl.String},
        orient="row",
    )
    known = {r[0] for r in conn.execute(text("SELECT id FROM players"))}
    all_rows, all_misses = [], []
    for week in range(1, last_week + 1):
        rows, misses = roster_rows(
            league_pk, week, api.get(f"/league/{lid}/matchups/{week}") or [], owners, idmap, known
        )
        if rows.height:  # weeks without matchups (e.g. past the season) add nothing
            all_rows.append(rows)
        if misses.height:
            all_misses.append(misses)
    rows = pl.concat(all_rows) if all_rows else pl.DataFrame()
    changed += upsert(conn, "fantasy_rosters", rows, ["league_id", "roster_id", "week"], run.id)
    misses = pl.concat(all_misses).unique() if all_misses else pl.DataFrame({"external_id": []})
    record_unresolved(run, f"league.{league_pk}", misses)
    run.rows = changed
    return {
        "league_pk": league_pk,
        "rosters": int(rows["roster_id"].n_unique()) if rows.height else 0,
        "weeks": last_week,
        "changed": changed,
        "unresolved": misses.height,
        "unsupported_scoring": sorted(
            ScoringRules.from_sleeper(league.get("scoring_settings") or {}).unsupported
        ),
    }
