"""nflverse frames -> table-shaped Polars frames. Pure functions; no network, no DB."""

import polars as pl

from fantasy_rag.ingest.base import require_columns, search_name

SKILL = ["QB", "RB", "WR", "TE", "K"]
# Identity/label columns, not stats. Fantasy points are never stored (computed per league).
STAT_EXCLUDE = {
    "player_id",
    "player_name",
    "player_display_name",
    "position",
    "position_group",
    "headshot_url",
    "season",
    "week",
    "season_type",
    "game_id",
    "team",
    "opponent_team",
    "fantasy_points",
    "fantasy_points_ppr",
    "full_name",
    "posteam",
}


def players(raw: pl.DataFrame) -> pl.DataFrame:
    require_columns(
        raw, ["gsis_id", "display_name", "position", "latest_team", "status"], "players"
    )
    return (
        raw.filter(pl.col("gsis_id").is_not_null())
        .select(
            pl.col("gsis_id").alias("id"),
            pl.coalesce(
                "display_name", pl.concat_str("first_name", "last_name", separator=" ")
            ).alias("full_name"),
            "position",
            pl.col("latest_team").alias("team"),
            "status",
        )
        .with_columns(search_name("full_name").alias("search_name"))
        .filter(pl.col("full_name").is_not_null())
    )


def _ids(df: pl.DataFrame, provider: str, col: str) -> pl.DataFrame:
    if col not in df.columns:
        return pl.DataFrame(
            schema={"provider": pl.String, "external_id": pl.String, "player_id": pl.String}
        )
    ext = df[col]
    if ext.dtype.is_float():
        ext = ext.cast(pl.Int64)
    return (
        df.select(pl.col("gsis_id").alias("player_id"))
        .with_columns(
            ext.cast(pl.String).str.strip_chars().alias("external_id"),
            pl.lit(provider).alias("provider"),
        )
        .filter(
            pl.col("player_id").is_not_null()
            & ~pl.col("external_id").is_in(["", "NA"])
            & pl.col("external_id").is_not_null()
        )
        .select("provider", "external_id", "player_id")
    )


def external_ids(
    players_raw: pl.DataFrame, ffids: pl.DataFrame, overrides: pl.DataFrame, known: set[str]
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(provider, external_id) -> GSIS. Returns (ids, conflicts).

    An external ID claimed by two different players is a conflict: reported, not stored.
    Human-confirmed overrides (data/id_overrides.csv) win over source data.
    """
    ids = pl.concat(
        [
            _ids(players_raw, "pfr", "pfr_id"),
            _ids(players_raw, "espn", "espn_id"),
            _ids(ffids, "pfr", "pfr_id"),
            _ids(ffids, "espn", "espn_id"),
            _ids(ffids, "sleeper", "sleeper_id"),
            _ids(ffids, "fantasypros", "fantasypros_id"),
        ]
    ).unique()
    ov = overrides.select("provider", "external_id", "player_id")
    ids = pl.concat([ids.join(ov, on=["provider", "external_id"], how="anti"), ov])
    ids = ids.filter(pl.col("player_id").is_in(list(known)))
    claims = ids.group_by("provider", "external_id").agg(pl.col("player_id").unique().sort())
    conflicts = claims.filter(pl.col("player_id").list.len() > 1).select(
        "provider", "external_id", pl.col("player_id").list.join(" | ").alias("candidates")
    )
    good = ids.join(conflicts, on=["provider", "external_id"], how="anti").unique()
    return good, conflicts


def schedules(raw: pl.DataFrame) -> pl.DataFrame:
    cols = [
        "game_id",
        "season",
        "week",
        "game_type",
        "home_team",
        "away_team",
        "gameday",
        "gametime",
        "home_score",
        "away_score",
        "result",
        "spread_line",
        "total_line",
        "roof",
        "surface",
        "stadium",
    ]
    require_columns(raw, cols, "schedules")
    # gameday + gametime are US/Eastern wall-clock values, including neutral-site games.
    kickoff = (
        pl.concat_str("gameday", "gametime", separator=" ")
        .str.to_datetime("%Y-%m-%d %H:%M", strict=False)
        .dt.replace_time_zone("America/New_York", ambiguous="earliest")
        .dt.convert_time_zone("UTC")
    )
    return raw.select(
        "game_id",
        "season",
        "week",
        "game_type",
        "home_team",
        "away_team",
        kickoff.alias("kickoff_at"),
        *[pl.col(c).cast(pl.Int64) for c in ("home_score", "away_score", "result")],
        "spread_line",
        "total_line",
        "roof",
        "surface",
        "stadium",
    )


# Per-kick distances ("51;43"); scoring needs them for per-yard kicker rules.
KICK_LISTS = ["fg_made_list", "fg_missed_list", "fg_blocked_list"]


def _stats_json(raw: pl.DataFrame) -> pl.Expr:
    numeric = [c for c, t in raw.schema.items() if t.is_numeric() and c not in STAT_EXCLUDE]
    # NaN is not valid JSON; Postgres jsonb rejects it.
    fields = [pl.col(c).fill_nan(None) if raw.schema[c].is_float() else pl.col(c) for c in numeric]
    fields += [pl.col(c) for c in KICK_LISTS if c in raw.columns]
    return pl.struct(fields).struct.json_encode().alias("stats")


def player_stats(raw: pl.DataFrame) -> pl.DataFrame:
    require_columns(
        raw,
        ["player_id", "season", "week", "season_type", "game_id", "team", "opponent_team"],
        "player_stats",
    )
    return raw.filter(pl.col("player_id").is_not_null()).select(
        "season",
        "week",
        "season_type",
        "player_id",
        "game_id",
        "team",
        pl.col("opponent_team").alias("opponent"),
        _stats_json(raw),
        pl.col("player_display_name").alias("name"),
        "position",
    )


def snap_counts(raw: pl.DataFrame) -> pl.DataFrame:
    require_columns(
        raw,
        [
            "pfr_player_id",
            "season",
            "week",
            "game_type",
            "game_id",
            "team",
            "offense_snaps",
            "offense_pct",
            "st_snaps",
            "st_pct",
        ],
        "snap_counts",
    )
    return raw.select(
        "season",
        "week",
        "game_type",
        "pfr_player_id",
        "game_id",
        "team",
        pl.col("offense_snaps").cast(pl.Int64),
        "offense_pct",
        pl.col("st_snaps").cast(pl.Int64),
        "st_pct",
        pl.col("player").alias("name"),
        "position",
    )


def opportunity(raw: pl.DataFrame) -> pl.DataFrame:
    require_columns(raw, ["player_id", "season", "week", "game_id", "posteam"], "ff_opportunity")
    return raw.filter(pl.col("player_id").is_not_null()).select(
        pl.col("season").cast(pl.Int64),
        pl.col("week").cast(pl.Int64),
        "player_id",
        "game_id",
        pl.col("posteam").alias("team"),
        _stats_json(raw),
        pl.col("full_name").alias("name"),
        "position",
    )


def depth_charts(raw: pl.DataFrame, season: int) -> pl.DataFrame:
    """Skill positions only (no IDP). <=2024: weekly; 2025+: `dt` snapshots."""
    if "week" in raw.columns:
        require_columns(
            raw, ["club_code", "position", "depth_team", "gsis_id", "formation"], "depth_charts"
        )
        out = raw.filter(
            pl.col("week").is_not_null()
            & pl.col("position").is_in(SKILL)
            & pl.col("formation").is_in(["Offense", "Special Teams"])
        ).select(
            pl.lit(season).alias("season"),
            pl.col("week").cast(pl.Int64),
            pl.lit(None, pl.Datetime("us", "UTC")).alias("as_of"),
            pl.col("club_code").alias("team"),
            "position",
            pl.col("depth_team").cast(pl.Int64).alias("depth"),
            "gsis_id",
            pl.lit(None, pl.String).alias("espn_id"),
            pl.col("full_name").alias("name"),
        )
    else:
        require_columns(
            raw, ["dt", "team", "pos_abb", "pos_rank", "gsis_id", "espn_id"], "depth_charts"
        )
        out = raw.filter(pl.col("pos_abb").is_in(["QB", "RB", "WR", "TE", "PK"])).select(
            pl.lit(season).alias("season"),
            pl.lit(None, pl.Int64).alias("week"),
            pl.col("dt").str.to_datetime("%Y-%m-%dT%H:%M:%SZ", time_zone="UTC").alias("as_of"),
            "team",
            pl.col("pos_abb").replace({"PK": "K"}).alias("position"),
            pl.col("pos_rank").cast(pl.Int64).alias("depth"),
            "gsis_id",
            "espn_id",
            pl.col("player_name").alias("name"),
        )
    # A player listed in two formations keeps his best (lowest) depth.
    return out.sort("depth").unique(
        ["season", "week", "as_of", "team", "position", "gsis_id", "espn_id"],
        keep="first",
        maintain_order=True,
    )


def injuries(raw: pl.DataFrame) -> pl.DataFrame:
    cols = [
        "season",
        "game_type",
        "week",
        "team",
        "gsis_id",
        "report_status",
        "report_primary_injury",
        "report_secondary_injury",
        "practice_status",
        "practice_primary_injury",
    ]
    require_columns(raw, cols, "injuries")
    blank = {"": None, "None": None}
    dm = (
        pl.col("date_modified").cast(pl.String).str.to_datetime(strict=False, time_zone="UTC")
        if "date_modified" in raw.columns
        else pl.lit(None, pl.Datetime("us", "UTC"))
    )
    return raw.filter(pl.col("gsis_id").is_not_null()).select(
        "season",
        "game_type",
        "week",
        "team",
        "gsis_id",
        *[pl.col(c).cast(pl.String).str.strip_chars().replace(blank) for c in cols[5:]],
        dm.alias("date_modified"),
        pl.col("full_name").alias("name"),
        "position",
    )


def resolve(
    df: pl.DataFrame, routes: list[tuple[str, str]], idmap: pl.DataFrame, known: set[str]
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Attach `player_id` by ID only, trying (column, provider) routes in order.

    provider "gsis" means the column already holds a GSIS ID (checked against `players`).
    Returns (resolved rows, unresolved report rows). Never matches on names.
    """
    df = df.with_row_index("_row").with_columns(pl.lit(None, pl.String).alias("_pid_out"))
    for col, provider in routes:
        if col not in df.columns:
            continue
        if provider == "gsis":
            found = pl.when(pl.col(col).is_in(list(known))).then(pl.col(col))
            df = df.with_columns(pl.coalesce("_pid_out", found).alias("_pid_out"))
        else:
            m = idmap.filter(pl.col("provider") == provider).select(
                pl.col("external_id").alias(col), pl.col("player_id").alias("_pid")
            )
            df = df.with_columns(pl.col(col).cast(pl.String)).join(m, on=col, how="left")
            df = df.with_columns(pl.coalesce("_pid_out", "_pid").alias("_pid_out")).drop("_pid")
    # Report each miss under the first route that had an ID, so none silently disappear.
    present = [(c, p) for c, p in routes if c in df.columns]
    has_id = [pl.col(c).is_not_null() for c, _ in present]
    miss_provider: pl.Expr = pl.lit(None, pl.String)
    miss_id: pl.Expr = pl.lit(None, pl.String)
    for (c, p), cond in reversed(list(zip(present, has_id, strict=True))):
        miss_provider = pl.when(cond).then(pl.lit(p)).otherwise(miss_provider)
        miss_id = pl.when(cond).then(pl.col(c).cast(pl.String)).otherwise(miss_id)
    unresolved = (
        df.filter(pl.col("_pid_out").is_null() & pl.any_horizontal(has_id))
        .select(
            miss_provider.alias("provider"),
            miss_id.alias("external_id"),
            *[pl.col(c) for c in ("name", "team", "position") if c in df.columns],
        )
        .unique(["provider", "external_id"], keep="first", maintain_order=True)
    )
    resolved = (
        df.filter(pl.col("_pid_out").is_not_null())
        .sort("_row")
        .drop(["_row"] + (["player_id"] if "player_id" in df.columns else []))
        .rename({"_pid_out": "player_id"})
    )
    return resolved, unresolved
