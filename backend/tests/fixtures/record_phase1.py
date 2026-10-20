"""Record small real slices of every Phase 1 source for offline tests.

Run by hand (network): uv run python tests/fixtures/record_phase1.py
nflverse data is CC-BY 4.0 (nflverse.com); Sleeper data is public player info.
"""

import json
from pathlib import Path

import nflreadpy as nfl
import polars as pl

from fantasy_rag.config import REPO_ROOT
from fantasy_rag.ingest.sleeper import Sleeper

OUT = Path(__file__).parent / "phase1"
TEAMS = ["DET", "GB"]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    season = int(nfl.get_current_season())
    stats = nfl.load_player_stats([season])
    week = stats["week"].max()
    stats = stats.filter((pl.col("week") == week) & pl.col("team").is_in(TEAMS))
    gsis = stats["player_id"].to_list()

    sched = nfl.load_schedules([season]).filter(
        (pl.col("week") == week)
        & (pl.col("home_team").is_in(TEAMS) | pl.col("away_team").is_in(TEAMS))
    )
    d24 = nfl.load_depth_charts([2024])
    d26 = nfl.load_depth_charts([season])
    inj = nfl.load_injuries([season])
    frames = {
        "player_stats": stats,
        "schedules": sched,
        "snap_counts": nfl.load_snap_counts([season]).filter(
            (pl.col("week") == week) & pl.col("team").is_in(TEAMS)
        ),
        "ff_opportunity": nfl.load_ff_opportunity([season]).filter(
            (pl.col("week") == week) & pl.col("posteam").is_in(TEAMS)
        ),
        "depth_2024": d24.filter((pl.col("week") == 1) & pl.col("club_code").is_in(TEAMS)),
        "depth_2025plus": d26.filter(
            (pl.col("dt") == d26["dt"].max()) & pl.col("team").is_in(TEAMS)
        ),
        "injuries": inj.filter((pl.col("week") == inj["week"].max()) & pl.col("team").is_in(TEAMS)),
    }
    ids_needed = set(gsis)
    for k in ("depth_2024", "depth_2025plus", "injuries"):
        ids_needed |= set(frames[k]["gsis_id"].drop_nulls().to_list())
    pfr_needed = set(frames["snap_counts"]["pfr_player_id"].to_list())
    players = nfl.load_players()
    frames["players"] = players.filter(
        pl.col("gsis_id").is_in(list(ids_needed)) | pl.col("pfr_id").is_in(list(pfr_needed))
    )
    frames["ff_playerids"] = nfl.load_ff_playerids().filter(
        pl.col("gsis_id").is_in(frames["players"]["gsis_id"].to_list())
    )
    for name, df in frames.items():
        df.write_parquet(OUT / f"{name}.parquet")

    sleeper_ids = {str(x) for x in frames["ff_playerids"]["sleeper_id"].drop_nulls().to_list()}
    allp = Sleeper().players(REPO_ROOT / "data" / "cache")
    keep = {
        k: v
        for k, v in allp.items()
        if k in sleeper_ids or (v.get("team") in TEAMS and v.get("active"))
    }
    (OUT / "sleeper_players.json").write_text(json.dumps(keep), encoding="utf-8")
    print({k: v.height for k, v in frames.items()}, "sleeper", len(keep))


if __name__ == "__main__":
    main()
