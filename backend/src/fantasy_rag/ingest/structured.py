"""`fantasy-rag ingest structured`: nflverse + Sleeper -> Postgres, idempotently."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import nflreadpy
import polars as pl
from nflreadpy.config import update_config
from sqlalchemy import Connection, Engine, text

from fantasy_rag.config import REPO_ROOT
from fantasy_rag.ingest import nflverse as nv
from fantasy_rag.ingest import pbp as pb
from fantasy_rag.ingest import sleeper as sl
from fantasy_rag.ingest.base import Run, prune, record_unresolved, stage, sync_run, upsert
from fantasy_rag.ingest.retry import Retrying

nfl = Retrying(nflreadpy)  # every load_* download retried on transient errors

FIRST_SEASON = 2022
OVERRIDES = REPO_ROOT / "data" / "id_overrides.csv"
CACHE = REPO_ROOT / "data" / "cache"
TRENDING_LOOKBACK_HOURS = 24

# The sources registry. Ingestion stores no more than `storage_allowed`.
SOURCES = pl.DataFrame(
    [
        {
            "name": "nflverse",
            "license": "CC-BY 4.0 (FTN charting CC-BY-SA 4.0)",
            "attribution": "Data from nflverse (nflverse.com)",
            "storage_allowed": "full",
            "refresh_cadence": "daily",
        },
        {
            "name": "ffverse.ffopportunity",
            "license": "ffverse/ffopportunity repo is GPL-3.0; keep attribution, share alike",
            "attribution": "Expected points from ffverse/ffopportunity",
            "storage_allowed": "full",
            "refresh_cadence": "weekly",
        },
        {
            "name": "dynastyprocess",
            "license": "dynastyprocess/data repo is GPL-3.0; check before redistributing",
            "attribution": "Player IDs from DynastyProcess",
            "storage_allowed": "full",
            "refresh_cadence": "daily",
        },
        {
            "name": "sleeper",
            "license": "Sleeper API: non-commercial use only; read-only",
            "attribution": "Trending data from Sleeper",
            "storage_allowed": "full",
            "refresh_cadence": "players daily, trending every 6h",
        },
    ]
)


@dataclass
class Result:
    source: str
    changed: int = 0
    unresolved: int = 0
    note: str = ""
    error: str | None = None


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.error is None for r in self.results)


def _step(
    engine: Engine, report: Report, source: str, body: Callable[[Run, Result], object]
) -> None:
    result = Result(source)
    report.results.append(result)
    try:
        with sync_run(engine, source) as run:
            body(run, result)
            run.rows = result.changed
    except Exception as exc:  # one failing source must not block the others
        result.error = repr(exc)[:300]


def _load_and_upsert(
    run: Run,
    result: Result,
    table: str,
    keys: list[str],
    frame: pl.DataFrame,
    routes: list[tuple[str, str]],
    idmap: pl.DataFrame,
    known: set[str],
    unresolved_source: str,
) -> pl.DataFrame:
    """Resolve, upsert, report unresolved; returns the resolved rows."""
    resolved, unresolved = nv.resolve(frame, routes, idmap, known)
    table_cols = [
        c for c in _columns(run.conn, table) if c not in ("ingested_at", "sync_run_id", "id")
    ]
    result.changed += upsert(run.conn, table, resolved.select(table_cols), keys, run.id)
    result.unresolved += unresolved.height
    record_unresolved(run, unresolved_source, unresolved)
    return resolved


def _columns(conn: Connection, table: str) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = :t"
            " ORDER BY ordinal_position"
        ),
        {"t": table},
    )
    return [r[0] for r in rows]


def _snapshot(run: Run, source: str, df: pl.DataFrame) -> int:
    """Insert a snapshot when a player's injury info differs from his latest one.

    A first observation is kept if it has any of status, body part or practice status
    (practice-only reports such as DNP with no game status are real information).
    """
    df = df.select("player_id", "status", "body_part", "practice", "season", "week").unique(
        "player_id", keep="first"
    )
    stage(run.conn, "injury_snapshots", df)
    inserted = run.conn.execute(
        text(
            """
            WITH latest AS (
                SELECT DISTINCT ON (player_id) player_id, status, body_part, practice, season, week
                FROM injury_snapshots WHERE source = :src
                ORDER BY player_id, ingested_at DESC, id DESC
            )
            INSERT INTO injury_snapshots
                (player_id, status, body_part, practice, season, week, source, sync_run_id)
            SELECT s.player_id, s.status, s.body_part, s.practice, s.season, s.week, :src, :run
            FROM stage s LEFT JOIN latest l USING (player_id)
            WHERE (l.player_id IS NULL
                   AND COALESCE(s.status, s.body_part, s.practice) IS NOT NULL)
               OR (l.player_id IS NOT NULL AND
                   ROW(s.status, s.body_part, s.practice, s.season, s.week)
                   IS DISTINCT FROM ROW(l.status, l.body_part, l.practice, l.season, l.week))
            """
        ),
        {"src": source, "run": run.id},
    ).rowcount
    run.conn.execute(text("DROP TABLE stage"))
    return int(inserted)


def drop_conflicted(conn: Connection, conflicts: pl.DataFrame) -> int:
    """Delete stored mappings for IDs that are now claimed by two players.

    Runs in the same transaction as the ID upsert, before anything resolves through them,
    so a once-valid ID can't keep attaching data to the wrong player.
    """
    if conflicts.is_empty():
        return 0
    ids = [f"{p}:{e}" for p, e in conflicts.select("provider", "external_id").rows()]
    removed = conn.execute(
        text("DELETE FROM player_external_ids WHERE provider || ':' || external_id = ANY(:ids)"),
        {"ids": ids},
    ).rowcount
    return int(removed)


def _idmap(conn: Connection) -> tuple[pl.DataFrame, set[str]]:
    ids = conn.execute(text("SELECT provider, external_id, player_id FROM player_external_ids"))
    idmap = pl.DataFrame(
        ids.fetchall(),
        schema={"provider": pl.String, "external_id": pl.String, "player_id": pl.String},
        orient="row",
    )
    known = {r[0] for r in conn.execute(text("SELECT id FROM players"))}
    return idmap, known


def run_structured(
    engine: Engine, api: Any = None, now: datetime | None = None, season: int | None = None
) -> Report:
    """Fill every Phase 1 table. Safe to re-run: unchanged data changes no rows."""
    # 1h: shorter than the 5h gap between scheduled Wed-Sat runs, so each run sees fresh
    # injury files; long enough that a quick re-run reuses downloads. (Sleeper's
    # /players/nfl keeps its own once-per-day cache.)
    update_config(cache_mode="filesystem", cache_dir=CACHE / "nflreadpy", cache_duration=3600)
    api = api or sl.Sleeper()
    now = now or datetime.now(UTC)
    season = season or int(nfl.get_current_season())
    seasons = list(range(FIRST_SEASON, season + 1))
    report = Report()

    _step(
        engine,
        report,
        "registry.sources",
        lambda run, res: setattr(
            res, "changed", upsert(run.conn, "sources", SOURCES, ["name"], None)
        ),
    )

    loaded: dict[str, Any] = {}  # frames fetched inside a step, reused by later steps

    def players(run: Run, res: Result) -> None:
        loaded["players"] = nfl.load_players()
        res.changed = upsert(run.conn, "players", nv.players(loaded["players"]), ["id"], run.id)

    _step(engine, report, "nflverse.players", players)

    def ids(run: Run, res: Result) -> None:
        overrides = pl.read_csv(OVERRIDES, schema_overrides={"external_id": pl.String})
        known = {r[0] for r in run.conn.execute(text("SELECT id FROM players"))}
        good, conflicts = nv.external_ids(
            loaded["players"], nfl.load_ff_playerids(), overrides, known
        )
        res.changed = upsert(
            run.conn, "player_external_ids", good, ["provider", "external_id"], run.id
        )
        res.changed += drop_conflicted(run.conn, conflicts)
        res.unresolved = conflicts.height
        res.note = f"{conflicts.height} IDs claimed by two players (skipped, old mappings removed)"
        record_unresolved(run, "conflict.player_external_ids", conflicts)

    _step(engine, report, "nflverse.player_ids", ids)

    with engine.connect() as conn:
        idmap, known = _idmap(conn)

    def dataset(
        source: str,
        table: str,
        keys: list[str],
        load: Callable[[], pl.DataFrame],
        routes: list[tuple[str, str]],
    ) -> None:
        _step(
            engine,
            report,
            source,
            lambda run, res: _load_and_upsert(
                run, res, table, keys, load(), routes, idmap, known, source
            ),
        )

    _step(
        engine,
        report,
        "nflverse.schedules",
        lambda run, res: setattr(
            res,
            "changed",
            upsert(
                run.conn,
                "schedules",
                nv.schedules(nfl.load_schedules(seasons)),
                ["game_id"],
                run.id,
            ),
        ),
    )
    dataset(
        "nflverse.player_stats",
        "player_week_stats",
        ["season", "week", "season_type", "player_id"],
        lambda: nv.player_stats(nfl.load_player_stats(seasons)),
        [("player_id", "gsis")],
    )
    dataset(
        "nflverse.snap_counts",
        "snap_counts",
        ["season", "week", "game_type", "player_id"],
        lambda: nv.snap_counts(nfl.load_snap_counts(seasons)),
        [("pfr_player_id", "pfr")],
    )
    dataset(
        "nflverse.ff_opportunity",
        "ff_opportunity",
        ["season", "week", "player_id"],
        lambda: nv.opportunity(nfl.load_ff_opportunity(seasons)),
        [("player_id", "gsis")],
    )
    dataset(
        "nflverse.depth_charts",
        "depth_charts",
        ["season", "week", "as_of", "team", "position", "player_id"],
        lambda: pl.concat(
            [nv.depth_charts(nfl.load_depth_charts([s]), s) for s in seasons],
            how="diagonal_relaxed",
        ),
        [("gsis_id", "gsis"), ("espn_id", "espn")],
    )
    dataset(
        "nflverse.injuries",
        "injury_history",
        ["season", "game_type", "week", "team", "player_id"],
        lambda: nv.injuries(nfl.load_injuries(seasons)).filter(pl.col("team").is_not_null()),
        [("gsis_id", "gsis")],
    )

    def team_defense(run: Run, res: Result) -> None:
        defense, special, usage, games, full = [], [], [], [], []
        for s in seasons:  # one season of play-by-play at a time keeps memory down
            raw = nfl.load_pbp([s])
            if raw.is_empty():  # not started yet: nothing to refresh or prune
                continue
            full.append(s)
            defense.append(pb.team_defense(raw))
            special.append(pb.special_teams(raw))
            usage.append(pb.usage(raw))
            games.append(pb.games(raw))
        # Each loaded season is the complete truth for that season, so rows a corrected
        # play-by-play no longer produces are pruned in the same transaction.
        loaded["pbp_seasons"] = full
        loaded["special_teams"] = pl.concat(special) if special else None
        loaded["usage"] = pl.concat(usage) if usage else None
        loaded["games"] = pl.concat(games) if games else None
        ids = ["season", "week", "season_type", "team", "game_id", "opponent"]
        keys = ["season", "week", "season_type", "team"]
        frame = pb.packed(pl.concat(defense), ids) if defense else None
        if frame is not None:
            res.changed = upsert(run.conn, "team_week_defense", frame, keys, run.id)
            res.changed += prune(run.conn, "team_week_defense", frame, keys, "season", full)

    _step(engine, report, "nflverse.pbp_team_defense", team_defense)

    def special_teams(run: Run, res: Result) -> None:
        if "pbp_seasons" not in loaded:
            raise RuntimeError("play-by-play did not load; special teams left unchanged")
        keys = ["season", "week", "season_type", "player_id"]
        frame = loaded["special_teams"]
        if frame is None:
            return
        packed = pb.packed(frame, ["season", "week", "season_type", "game_id", "player_id"])
        resolved = _load_and_upsert(
            run, res, "player_week_special_teams", keys, packed, [("player_id", "gsis")],
            idmap, known, "nflverse.pbp_special_teams",
        )  # fmt: skip
        res.changed += prune(
            run.conn, "player_week_special_teams", resolved, keys, "season",
            loaded["pbp_seasons"],
        )  # fmt: skip

    _step(engine, report, "nflverse.pbp_special_teams", special_teams)

    def pbp_usage(run: Run, res: Result) -> None:
        """Targets, carries and red-zone looks per player-game, plus which games' play-by-play
        was loaded (coverage: no row in a covered game is a known zero)."""
        if "pbp_seasons" not in loaded:
            raise RuntimeError("play-by-play did not load; usage left unchanged")
        full = loaded["pbp_seasons"]
        if loaded["games"] is not None:
            game_keys = ["season", "week", "season_type", "game_id"]
            res.changed += upsert(run.conn, "pbp_games", loaded["games"], game_keys, run.id)
            res.changed += prune(run.conn, "pbp_games", loaded["games"], game_keys, "season", full)
        if loaded["usage"] is None:
            return
        keys = ["season", "week", "season_type", "player_id"]
        packed = pb.packed(
            loaded["usage"], ["season", "week", "season_type", "game_id", "player_id"]
        )
        resolved = _load_and_upsert(
            run, res, "player_week_pbp_usage", keys, packed, [("player_id", "gsis")],
            idmap, known, "nflverse.pbp_usage",
        )  # fmt: skip
        res.changed += prune(run.conn, "player_week_pbp_usage", resolved, keys, "season", full)

    _step(engine, report, "nflverse.pbp_usage", pbp_usage)

    def nflverse_snapshot(run: Run, res: Result) -> None:
        inj = nv.injuries(nfl.load_injuries([season]))
        latest = inj.filter(pl.col("week") == inj["week"].max()).rename(
            {
                "report_status": "status",
                "report_primary_injury": "body_part",
                "practice_status": "practice",
            }
        )
        resolved, unresolved = nv.resolve(latest, [("gsis_id", "gsis")], idmap, known)
        res.changed = _snapshot(run, "nflverse", resolved)
        res.unresolved = unresolved.height
        record_unresolved(run, "snapshot.nflverse", unresolved)

    _step(engine, report, "snapshot.nflverse_injuries", nflverse_snapshot)

    def sleeper_snapshot(run: Run, res: Result) -> None:
        loaded["sleeper"] = api.players(CACHE)
        df = sl.injury_frame(loaded["sleeper"]).with_columns(
            pl.lit(None, pl.Int64).alias("season"), pl.lit(None, pl.Int64).alias("week")
        )
        resolved, unresolved = nv.resolve(df, [("sleeper_id", "sleeper")], idmap, known)
        res.changed = _snapshot(run, "sleeper", resolved)
        res.unresolved = unresolved.height
        record_unresolved(run, "snapshot.sleeper", unresolved)

    _step(engine, report, "snapshot.sleeper_injuries", sleeper_snapshot)

    def fantasy_positions(run: Run, res: Result) -> None:
        """Sleeper's slot eligibility (a fullback is RB-eligible), keyed to our players."""
        rows = [
            {"sleeper_id": str(pid), "fantasy_positions": "{" + ",".join(fp) + "}"}
            for pid, p in loaded.get("sleeper", {}).items()
            if (fp := p.get("fantasy_positions"))
        ]
        if not rows:
            return
        resolved, _ = nv.resolve(pl.DataFrame(rows), [("sleeper_id", "sleeper")], idmap, known)
        frame = resolved.select(pl.col("player_id").alias("id"), "fantasy_positions")
        stage(run.conn, "players", frame.unique("id", keep="first"))
        res.changed = int(
            run.conn.execute(
                text(
                    "UPDATE players p SET fantasy_positions = s.fantasy_positions"
                    " FROM stage s WHERE p.id = s.id"
                    " AND p.fantasy_positions IS DISTINCT FROM s.fantasy_positions"
                )
            ).rowcount
        )
        run.conn.execute(text("DROP TABLE stage"))

    _step(engine, report, "sleeper.fantasy_positions", fantasy_positions)

    def trending(run: Run, res: Result) -> None:
        adds = api.get(
            "/players/nfl/trending/add", lookback_hours=TRENDING_LOOKBACK_HOURS, limit=50
        )
        drops = api.get(
            "/players/nfl/trending/drop", lookback_hours=TRENDING_LOOKBACK_HOURS, limit=50
        )
        df, skipped = sl.trending_frame(adds, drops, TRENDING_LOOKBACK_HOURS, now)
        resolved, unresolved = nv.resolve(df, [("sleeper_id", "sleeper")], idmap, known)
        cols = ["player_id", "kind", "count", "lookback_hours", "window_start"]
        res.changed = upsert(
            run.conn,
            "trending",
            resolved.select(cols),
            ["window_start", "kind", "lookback_hours", "player_id"],
            run.id,
        )
        res.unresolved = unresolved.height
        res.note = f"{skipped} team-defense entries skipped"
        record_unresolved(run, "trending.sleeper", unresolved)

    _step(engine, report, "sleeper.trending", trending)

    def coverage(run: Run, res: Result) -> None:
        """Skill players with stats this season but no Sleeper ID: leagues can't map them."""
        rows = run.conn.execute(
            text(
                """
                SELECT DISTINCT ON (s.player_id) s.player_id, p.full_name, s.team, p.position
                FROM player_week_stats s JOIN players p ON p.id = s.player_id
                WHERE s.season = :season AND p.position IN ('QB','RB','WR','TE','K')
                  AND NOT EXISTS (SELECT 1 FROM player_external_ids x
                                  WHERE x.player_id = s.player_id AND x.provider = 'sleeper')
                ORDER BY s.player_id, s.week DESC
                """
            ),
            {"season": season},
        ).fetchall()
        missing = pl.DataFrame(
            rows, schema=["external_id", "name", "team", "position"], orient="row"
        )
        missing = missing.with_columns(
            pl.lit("gsis").alias("provider"),
            pl.Series(
                "candidates",
                [_candidates(loaded.get("sleeper", {}), r) for r in missing.iter_rows(named=True)],
                dtype=pl.String,
            ),
        )
        res.unresolved = missing.height
        res.note = "add confirmed pairs to data/id_overrides.csv"
        record_unresolved(run, "coverage.sleeper", missing)

    _step(engine, report, "coverage.sleeper_ids", coverage)
    return report


def _candidates(players: dict[str, dict[str, Any]], row: dict[str, Any]) -> str | None:
    """Hint for a human: Sleeper players with the same last name, team and position.

    Never applied automatically; a person confirms each pair in data/id_overrides.csv.
    """
    last = str(row["name"] or "").split(" ")[-1].lower()
    hits = [
        f"{pid} {p.get('full_name')}"
        for pid, p in players.items()
        if (p.get("last_name") or "").lower() == last
        and p.get("team") == row["team"]
        and p.get("position") == row["position"]
    ]
    return "; ".join(hits) or None
