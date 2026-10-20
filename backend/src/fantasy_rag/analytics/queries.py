"""Parameterized, read-only queries behind the agent tools. No SQL is accepted from callers."""

import json
import re
import unicodedata
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, text

from fantasy_rag.analytics.scoring import ScoringRules

SKILL = ("QB", "RB", "WR", "TE", "K")
_OFFENSE = {
    "pass_yd": 0.04,
    "pass_td": 4,
    "pass_int": -2,
    "pass_2pt": 2,
    "rush_yd": 0.1,
    "rush_td": 6,
    "rush_2pt": 2,
    "rec_yd": 0.1,
    "rec_td": 6,
    "rec_2pt": 2,
    "fum_lost": -2,
    "fum_rec_td": 6,
    "st_td": 6,  # kick/punt return TDs
    "xpm": 1,
    "xpmiss": -1,
    "fgm_0_19": 3,
    "fgm_20_29": 3,
    "fgm_30_39": 3,
    "fgm_40_49": 4,
    "fgm_50p": 5,
    "fgmiss": -1,
}
PRESETS = {
    "ppr": {**_OFFENSE, "rec": 1.0},
    "half": {**_OFFENSE, "rec": 0.5},
    "std": dict(_OFFENSE),
}


class PlayerRef(BaseModel):
    id: str
    name: str
    position: str | None
    team: str | None


class AmbiguousPlayer(ValueError):
    def __init__(self, name: str, candidates: list[PlayerRef]) -> None:
        self.candidates = candidates
        listed = "; ".join(f"{c.name} ({c.position}, {c.team})" for c in candidates) or "none"
        super().__init__(f"'{name}' matches no single player. Candidates: {listed}")


def latest_week(conn: Connection) -> tuple[int, int]:
    """(season, last week with regular-season stats)."""
    season, week = conn.execute(
        text(
            "SELECT season, max(week) FROM player_week_stats WHERE season_type = 'REG'"
            " AND season = (SELECT max(season) FROM player_week_stats) GROUP BY season"
        )
    ).one()
    return int(season), int(week)


def resolve_player(
    conn: Connection, name: str, position: str | None = None, team: str | None = None
) -> PlayerRef:
    """Name -> one player via trigram similarity; ambiguity is an error, never a guess.

    Fantasy positions only (unless `position` is given). Among equally good name matches,
    a player with stats this season wins; if that still leaves several, the caller must
    narrow it with `position` or `team`.
    """
    season, _ = latest_week(conn)
    rows = conn.execute(
        text(
            """
            SELECT p.id, p.full_name, p.position, p.team,
                   greatest(similarity(p.search_name, :q),
                            word_similarity(:q, p.search_name)) AS sim,
                   EXISTS (SELECT 1 FROM player_week_stats s
                           WHERE s.player_id = p.id AND s.season = :season) AS active
            FROM players p
            WHERE (p.search_name % :q OR :q <% p.search_name)
              AND (CAST(:pos AS text) IS NULL AND p.position = ANY(:skill) OR p.position = :pos)
              AND (CAST(:team AS text) IS NULL OR p.team = :team)
            ORDER BY sim DESC, active DESC
            LIMIT 10
            """
        ),
        {
            "q": _normalize(name),
            "season": season,
            "pos": position,
            "team": team,
            "skill": list(SKILL),
        },  # fmt: skip
    ).fetchall()
    if not rows:
        raise AmbiguousPlayer(name, [])
    best = rows[0].sim
    top = [r for r in rows if r.sim >= best - 0.05]  # near-ties on the name
    if len(top) > 1 and any(r.active for r in top):
        top = [r for r in top if r.active]
    if len(top) != 1:
        raise AmbiguousPlayer(
            name,
            [PlayerRef(id=r.id, name=r.full_name, position=r.position, team=r.team) for r in top],
        )
    r = top[0]
    return PlayerRef(id=r.id, name=r.full_name, position=r.position, team=r.team)


def _normalize(name: str) -> str:
    """Same normalization as players.search_name (ingest.base.search_name)."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def rules_for(
    conn: Connection, league_id: int | None, scoring: str | None
) -> tuple[ScoringRules, str]:
    """A league's own rules, or a preset (ppr / half / std). Returns (rules, label)."""
    if league_id is not None:
        row = conn.execute(
            text("SELECT scoring_settings FROM fantasy_leagues WHERE id = :id"),
            {"id": league_id},
        ).one_or_none()
        if row is None:
            raise ValueError(f"no imported league with id {league_id}")
        # Rebuilt from Sleeper's raw settings each time, so rules saved by other code
        # versions can never name a key this version can't compute.
        return ScoringRules.from_sleeper(row.scoring_settings), f"league {league_id} rules"
    key = scoring or "ppr"
    return ScoringRules.from_sleeper(PRESETS[key]), key


def weekly_rows(
    conn: Connection,
    player_ids: list[str] | None,
    seasons: list[int],
    weeks: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Regular-season stat rows with player position; `player_ids=None` means everyone.

    Special-teams stats from play-by-play (st_ff, st_fum_rec) are merged into `stats`.
    """
    rows = conn.execute(
        text(
            """
            SELECT s.player_id, s.season, s.week, s.team, s.opponent, p.position,
                   s.stats || COALESCE(st.stats, '{}'::jsonb) AS stats
            FROM player_week_stats s JOIN players p ON p.id = s.player_id
            LEFT JOIN player_week_special_teams st USING (season, week, season_type, player_id)
            WHERE s.season_type = 'REG' AND s.season = ANY(:seasons)
              AND (CAST(:ids AS text[]) IS NULL OR s.player_id = ANY(:ids))
              AND (CAST(:w0 AS int) IS NULL OR s.week BETWEEN :w0 AND :w1)
            ORDER BY s.season, s.week
            """
        ),
        {
            "ids": player_ids,
            "seasons": seasons,
            "w0": weeks[0] if weeks else None,
            "w1": weeks[1] if weeks else None,
        },  # fmt: skip
    )
    return [
        {**r._mapping, "stats": r.stats if isinstance(r.stats, dict) else json.loads(r.stats)}
        for r in rows
    ]


def injury_snapshots(conn: Connection, player_id: str) -> list[dict[str, Any]]:
    """Latest snapshot per source, with when we observed it."""
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT ON (source) source, status, body_part, practice, season, week,
                   ingested_at
            FROM injury_snapshots WHERE player_id = :p
            ORDER BY source, ingested_at DESC, id DESC
            """
        ),
        {"p": player_id},
    )
    return [dict(r._mapping) for r in rows]


def last_sync(conn: Connection, source: str) -> datetime | None:
    value: datetime | None = conn.execute(
        text("SELECT max(finished_at) FROM sync_runs WHERE source = :s AND status = 'ok'"),
        {"s": source},
    ).scalar_one()
    return value


def trending(conn: Connection, kind: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT t.player_id, p.full_name, p.position, p.team, t.count, t.window_start
            FROM trending t JOIN players p ON p.id = t.player_id
            WHERE t.kind = :k AND t.window_start = (SELECT max(window_start) FROM trending)
            ORDER BY t.count DESC LIMIT :n
            """
        ),
        {"k": kind, "n": limit},
    )
    return [dict(r._mapping) for r in rows]


def rostered(conn: Connection, league_id: int) -> tuple[set[str], int | None, datetime | None]:
    """Players on any roster in the league's latest imported week, and when that was synced."""
    row = conn.execute(
        text(
            """
            SELECT array_agg(DISTINCT pid), max(week), max(ingested_at) FROM (
                SELECT unnest(player_ids) AS pid, week, ingested_at FROM fantasy_rosters
                WHERE league_id = :l
                  AND week = (SELECT max(week) FROM fantasy_rosters WHERE league_id = :l)
            ) x
            """
        ),
        {"l": league_id},
    ).one()
    return set(row[0] or []), row[1], row[2]


def players_by_id(conn: Connection, ids: list[str]) -> dict[str, PlayerRef]:
    rows = conn.execute(
        text("SELECT id, full_name, position, team FROM players WHERE id = ANY(:ids)"),
        {"ids": ids},
    )
    return {
        r.id: PlayerRef(id=r.id, name=r.full_name, position=r.position, team=r.team) for r in rows
    }
