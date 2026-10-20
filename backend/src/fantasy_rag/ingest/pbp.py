"""nflverse play-by-play -> weekly team-defense and special-teams player stats. Pure Polars.

Each stat follows Sleeper's (Sportradar) definition, checked per key against Sleeper's
per-week stats for 2026 weeks 1-6 (plus 2024-25 weeks with defensive 2-point returns);
the checks and the few known differences are listed in docs/PLAN.md (Data model).
"""

import polars as pl

from fantasy_rag.ingest.base import require_columns

KEYS = ["season", "week", "season_type", "game_id"]
ST_PLAYS = ["kickoff", "punt", "field_goal", "extra_point"]
# nflverse uses "LA" for the Rams; Sleeper's team DEF ID is "LAR".
SLEEPER_TEAM = {"LA": "LAR"}
COLUMNS = [
    *KEYS, "home_team", "away_team", "home_score", "away_score", "play_type", "posteam",
    "defteam", "td_team", "touchdown", "safety", "sack", "sack_player_id",
    "half_sack_1_player_id", "interception", "passing_yards", "rushing_yards", "yards_gained",
    "punt_blocked", "field_goal_result", "extra_point_result", "defensive_two_point_conv",
    "fourth_down_failed", "first_down_penalty", "first_down", "fixed_drive",
    "pass_defense_1_player_id", "pass_defense_2_player_id", "tackle_for_loss_1_player_id",
    "tackle_for_loss_2_player_id",
    *(f"forced_fumble_player_{i}_{c}" for i in (1, 2) for c in ("team", "player_id")),
    *(f"fumble_recovery_{i}_{c}" for i in (1, 2) for c in ("team", "player_id")),
    "fumbled_1_team", "fumbled_2_team",
]  # fmt: skip


def _n(col: str) -> pl.Expr:
    return pl.col(col).fill_null(0)


def _count(cond: pl.Expr) -> pl.Expr:
    return cond.fill_null(False).cast(pl.Int64).sum()


def _prepare(pbp: pl.DataFrame) -> pl.DataFrame:
    require_columns(pbp, COLUMNS, "pbp")
    teams = [c for c in COLUMNS if c.endswith("team")]
    return pbp.select(COLUMNS).with_columns(
        *(pl.col(c).replace(SLEEPER_TEAM) for c in teams),
        st=pl.col("play_type").is_in(ST_PLAYS).fill_null(False),
    )


def team_defense(pbp: pl.DataFrame) -> pl.DataFrame:
    """One row per (game, team) with the stats Sleeper scores that team's DEF on."""
    p = _prepare(pbp)
    games = p.group_by(KEYS).agg(
        pl.col("home_team", "away_team", "home_score", "away_score").first()
    )
    sides = pl.concat(
        [
            games.select(
                *KEYS,
                pl.col(f"{us}_team").alias("team"),
                pl.col(f"{them}_team").alias("opponent"),
                pl.col(f"{them}_score").alias("opp_score"),
            )
            for us, them in (("home", "away"), ("away", "home"))
        ]
    )
    # Each play contributes to every team in the game; `t` is the team being credited.
    plays = sides.select(*KEYS, "team").join(p, on=KEYS)
    t = pl.col("team")
    st = pl.col("st")
    on_def = (pl.col("defteam") == t) & ~st
    td = (pl.col("td_team") == t) & (_n("touchdown") == 1)
    stats = plays.group_by(*KEYS, "team").agg(
        # Every sack, credited to a defender or not (two 2026 strip-sacks differ; see docs).
        sack=_count((pl.col("defteam") == t) & (_n("sack") == 1)),
        int=_count((pl.col("defteam") == t) & (_n("interception") == 1)),
        ff=_forced_on(t, on_def),
        fum_rec=_recovered_on(t, ~st),
        def_st_ff=_forced_on(t, st),
        def_st_fum_rec=_recovered_on(t, st),
        # Return TDs on defense, plus the kicking team's TDs on special teams (a recovered
        # muff); the receiving team's kick/punt return TDs are def_st_td.
        def_td=_count(td & (on_def | (st & _kicking(t)))),
        def_st_td=_count(td & st & ~_kicking(t)),
        safe=_count((pl.col("defteam") == t) & (_n("safety") == 1)),
        blk_kick=_count(
            st
            & ~_kicking(t)
            & (
                (_n("punt_blocked") == 1)
                | (pl.col("field_goal_result") == "blocked")
                | (pl.col("extra_point_result") == "blocked")
            )
        ),
        def_2pt=_count((pl.col("defteam") == t) & (_n("defensive_two_point_conv") == 1)),
        # A failed 4th down that isn't wiped out by a penalty first down and isn't
        # scored as a defensive TD or safety instead.
        def_4_and_stop=_count(
            on_def
            & (_n("fourth_down_failed") == 1)
            & (_n("first_down_penalty") == 0)
            & (_n("touchdown") == 0)
            & (_n("safety") == 0)
        ),
        def_pass_def=_count(on_def & pl.col("pass_defense_1_player_id").is_not_null())
        + _count(on_def & pl.col("pass_defense_2_player_id").is_not_null()),
        tkl_loss=_count(on_def & pl.col("tackle_for_loss_1_player_id").is_not_null())
        + _count(on_def & pl.col("tackle_for_loss_2_player_id").is_not_null()),
        # Offensive yards: passing + rushing + sack yardage, including fake kicks.
        yds_allow=pl.when(pl.col("defteam") == t)
        .then(
            _n("passing_yards")
            + _n("rushing_yards")
            + pl.when(_n("sack") == 1).then(_n("yards_gained")).otherwise(0)
        )
        .otherwise(0)
        .sum(),
    )
    # Points allowed leave out what the opponent's defense scored (TDs and safeties);
    # its special-teams return TDs and defensive 2-point returns still count.
    opp = stats.select(
        *KEYS,
        pl.col("team").alias("opponent"),
        (6 * pl.col("def_td") + 2 * pl.col("safe")).alias("opp_def_pts"),
    )
    return (
        sides.join(stats, on=[*KEYS, "team"], how="left")
        .join(opp, on=[*KEYS, "opponent"], how="left")
        .join(_three_and_outs(p), on=[*KEYS, "team"], how="left")
        .with_columns(
            pts_allow=pl.col("opp_score") - pl.col("opp_def_pts"),
            def_3_and_out=pl.col("def_3_and_out").fill_null(0),
        )
        .drop("opp_score", "opp_def_pts")
    )


def _kicking(team: pl.Expr) -> pl.Expr:
    """`team` is the kicking side of a special-teams play (posteam, except on kickoffs)."""
    kicker = pl.when(pl.col("play_type") == "kickoff").then("defteam").otherwise("posteam")
    return kicker == team


def _forced_on(team: pl.Expr, where: pl.Expr) -> pl.Expr:
    return _count(where & (pl.col("forced_fumble_player_1_team") == team)) + _count(
        where & (pl.col("forced_fumble_player_2_team") == team)
    )


def _recovered_on(team: pl.Expr, where: pl.Expr) -> pl.Expr:
    """Recoveries by `team` of a fumble by the other team (own recoveries don't count)."""
    return sum(
        (
            _count(
                where
                & (pl.col(f"fumble_recovery_{i}_team") == team)
                & (pl.col(f"fumbled_{i}_team") != team)
            )
            for i in (1, 2)
        ),
        pl.lit(0, pl.Int64),
    )


def _three_and_outs(p: pl.DataFrame) -> pl.DataFrame:
    """Opponent drives that end in a punt without any first down (penalty ones included)."""
    drives = (
        p.filter(pl.col("fixed_drive").is_not_null() & (pl.col("play_type") != "kickoff"))
        .group_by(*KEYS, "fixed_drive", "posteam")
        .agg(
            team=pl.col("defteam").drop_nulls().first(),
            punt=(pl.col("play_type") == "punt").any(),
            first_downs=_n("first_down").sum(),
        )
    )
    return (
        drives.filter(pl.col("punt") & (pl.col("first_downs") == 0))
        .group_by(*KEYS, "team")
        .agg(def_3_and_out=pl.len())
    )


def special_teams(pbp: pl.DataFrame) -> pl.DataFrame:
    """Per player-game st_ff / st_fum_rec on kickoffs, punts, field goals and PATs."""
    p = _prepare(pbp).filter("st")
    events = pl.concat(
        [
            frame
            for i in (1, 2)
            for frame in (
                p.select(
                    *KEYS,
                    pl.col(f"forced_fumble_player_{i}_player_id").alias("player_id"),
                    pl.lit("st_ff").alias("stat"),
                ),
                p.filter(pl.col(f"fumble_recovery_{i}_team") != pl.col(f"fumbled_{i}_team")).select(
                    *KEYS,
                    pl.col(f"fumble_recovery_{i}_player_id").alias("player_id"),
                    pl.lit("st_fum_rec").alias("stat"),
                ),
            )
        ]
    ).filter(pl.col("player_id").is_not_null())
    return (
        events.group_by(*KEYS, "player_id")
        .agg(
            st_ff=_count(pl.col("stat") == "st_ff"),
            st_fum_rec=_count(pl.col("stat") == "st_fum_rec"),
        )
        .sort(KEYS + ["player_id"])
    )


def packed(df: pl.DataFrame, ids: list[str]) -> pl.DataFrame:
    """Table shape: `ids` columns plus every other column folded into a `stats` JSON."""
    stats = [c for c in df.columns if c not in ids]
    return df.select(*ids, pl.struct(stats).struct.json_encode().alias("stats"))
