"""Sleeper API (read-only): client plus frames for injury snapshots and trending."""

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl

SLEEPER = "https://api.sleeper.app/v1"
USER_AGENT = "fantasy-rag/0.1 (+https://github.com/NgoBenny/fantasy-rag)"
SKILL = {"QB", "RB", "WR", "TE", "K"}


class Sleeper:
    def __init__(self) -> None:
        self.client = httpx.Client(base_url=SLEEPER, headers={"User-Agent": USER_AGENT}, timeout=60)

    def get(self, path: str, **params: Any) -> Any:
        time.sleep(0.25)  # far under Sleeper's 1,000 calls/minute
        resp = self.client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def players(self, cache_dir: Path) -> dict[str, dict[str, Any]]:
        """/players/nfl at most once per day: reuse today's cached copy if present."""
        cache = cache_dir / f"sleeper_players_{date.today().isoformat()}.json"
        if cache.exists():
            data: dict[str, dict[str, Any]] = json.loads(cache.read_text(encoding="utf-8"))
            return data
        data = self.get("/players/nfl")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
        return data


def injury_frame(players: dict[str, dict[str, Any]]) -> pl.DataFrame:
    """Current status of every active, rostered skill player (status may be null = healthy)."""
    rows = [
        {
            "sleeper_id": str(p["player_id"]),
            "status": p.get("injury_status") or None,
            "body_part": p.get("injury_body_part") or None,
            "practice": p.get("practice_participation") or None,
            "name": p.get("full_name"),
            "team": p.get("team"),
            "position": p.get("position"),
        }
        for p in players.values()
        if p.get("active") and p.get("team") and p.get("position") in SKILL
    ]
    return pl.DataFrame(rows, schema={k: pl.String for k in rows[0]} if rows else None)


def window_start(now: datetime) -> datetime:
    """Start of the 6-hour UTC window `now` falls in (trending is stored once per window)."""
    now = now.astimezone(UTC)
    return now.replace(hour=now.hour - now.hour % 6, minute=0, second=0, microsecond=0)


def trending_frame(
    adds: list[dict[str, Any]], drops: list[dict[str, Any]], lookback_hours: int, now: datetime
) -> tuple[pl.DataFrame, int]:
    """Returns (rows, team defenses skipped). Team DEF entries (e.g. "DET") are not players."""
    rows = [
        {"sleeper_id": str(t["player_id"]), "kind": kind, "count": int(t["count"])}
        for kind, items in (("add", adds), ("drop", drops))
        for t in items
    ]
    df = pl.DataFrame(rows, schema={"sleeper_id": pl.String, "kind": pl.String, "count": pl.Int64})
    players = df.filter(pl.col("sleeper_id").str.contains(r"^\d+$"))
    return (
        players.with_columns(
            pl.lit(lookback_hours).alias("lookback_hours"),
            pl.lit(window_start(now)).alias("window_start"),
        ),
        df.height - players.height,
    )
