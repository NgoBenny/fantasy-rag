"""Live check (reads the dev DB after `fantasy-rag ingest structured`, plus nflverse).

Standard PPR computed from our stored raw stats must reproduce nflverse's own
`fantasy_points_ppr` for last week's top 10 scorers.
"""

import nflreadpy as nfl
import polars as pl
import pytest
from sqlalchemy import text

from fantasy_rag.db import get_engine

pytestmark = [pytest.mark.live, pytest.mark.db]

S = "(stats->>'{}')::float"
PPR = " + ".join(
    [
        f"coalesce({S.format(k)}, 0) * {w}"
        for k, w in [
            ("passing_yards", 0.04), ("passing_tds", 4), ("passing_interceptions", -2),
            ("passing_2pt_conversions", 2), ("rushing_yards", 0.1), ("rushing_tds", 6),
            ("rushing_2pt_conversions", 2), ("receptions", 1), ("receiving_yards", 0.1),
            ("receiving_tds", 6), ("receiving_2pt_conversions", 2), ("special_teams_tds", 6),
            ("sack_fumbles_lost", -2), ("rushing_fumbles_lost", -2), ("receiving_fumbles_lost", -2),
        ]
    ]
)  # fmt: skip


def test_last_week_top10_ppr_matches_nflverse() -> None:
    season = int(nfl.get_current_season())
    theirs = nfl.load_player_stats([season]).filter(pl.col("season_type") == "REG")
    week = int(theirs["week"].max())
    top = theirs.filter(pl.col("week") == week).sort("fantasy_points_ppr", descending=True).head(10)

    with get_engine().connect() as conn:
        ours = conn.execute(
            text(
                f"SELECT player_id, round(({PPR})::numeric, 2) AS pts FROM player_week_stats"
                " WHERE season = :s AND week = :w AND season_type = 'REG'"
                " ORDER BY pts DESC, player_id LIMIT 10"
            ),
            {"s": season, "w": week},
        ).fetchall()

    expected = [
        (r["player_id"], round(r["fantasy_points_ppr"], 2)) for r in top.iter_rows(named=True)
    ]
    got = [(pid, float(pts)) for pid, pts in ours]
    print(f"\nweek {week} top 10:", got)
    assert got == expected
