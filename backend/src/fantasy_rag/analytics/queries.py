"""Parameterized, read-only queries behind the agent tools. No SQL is accepted from callers."""

import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Connection, text

from fantasy_rag.analytics.availability import Availability, Fetch, Tx, reconcile
from fantasy_rag.analytics.scoring import ScoringRules
from fantasy_rag.ingest.news import TEAMS
from fantasy_rag.ingest.pbp import SLEEPER_TEAM

SKILL = ("QB", "RB", "WR", "TE", "K")
# Sleeper's team-defense player IDs (team codes): never ranked, so an unmapped one is harmless.
SLEEPER_DEFENSES = frozenset(SLEEPER_TEAM.get(t, t) for t in TEAMS)
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
    fantasy_positions: list[str] | None = None  # Sleeper slot eligibility, when known


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


def rostered(
    conn: Connection, league_id: int, now: datetime | None = None
) -> tuple[Availability, int | None, datetime | None]:
    """Who is rostered now: the latest roster snapshot corrected by Sleeper transactions
    (see analytics.availability for the assumptions). Returns (availability, snapshot
    week, snapshot sync time)."""
    snap = conn.execute(
        text(
            "SELECT roster_id, player_ids, week, ingested_at FROM fantasy_rosters"
            " WHERE league_id = :l"
            " AND week = (SELECT max(week) FROM fantasy_rosters WHERE league_id = :l)"
        ),
        {"l": league_id},
    ).fetchall()
    if not snap:
        return Availability({}), None, None
    week, synced = snap[0].week, max(r.ingested_at for r in snap)
    owner = {pid: r.roster_id for r in snap for pid in r.player_ids or []}
    fetched = {
        (r.kind, r.week): r
        for r in conn.execute(
            text("SELECT kind, week, fetched_at, ok FROM league_fetches WHERE league_id = :l"),
            {"l": league_id},
        )
    }
    boundary = fetched.get(("rosters", week))
    txs = [
        Tx(
            r.transaction_id,
            r.type,
            r.status,
            r.week,
            r.status_updated,
            r.adds,
            r.drops,
            frozenset(r.unresolved or ()),
        )  # fmt: skip
        for r in conn.execute(
            text(
                "SELECT transaction_id, type, status, week, status_updated, adds, drops,"
                " unresolved"
                " FROM league_transactions WHERE league_id = :l"
            ),
            {"l": league_id},
        )
    ]
    fetches = [Fetch(w, r.fetched_at, r.ok) for (k, w), r in fetched.items() if k == "transactions"]
    # Sleeper's current week = the newest week the sync fetched; if syncing stops, the
    # fetch-age limit marks the result stale.
    current = max([week, *(f.week for f in fetches)])
    got = reconcile(
        owner,
        week,
        boundary.fetched_at if boundary and boundary.ok else None,
        current,
        txs,
        fetches,
        now or datetime.now(UTC),
        SLEEPER_DEFENSES,
    )
    return got, week, synced


def players_by_id(conn: Connection, ids: list[str]) -> dict[str, PlayerRef]:
    rows = conn.execute(
        text(
            "SELECT id, full_name, position, team, fantasy_positions FROM players"
            " WHERE id = ANY(:ids)"
        ),
        {"ids": ids},
    )
    return {
        r.id: PlayerRef(
            id=r.id,
            name=r.full_name,
            position=r.position,
            team=r.team,
            fantasy_positions=r.fantasy_positions,
        )
        for r in rows
    }


def league_roster(conn: Connection, league_id: int, roster_id: int) -> dict[str, Any]:
    """A roster's latest imported week: GSIS players, Sleeper starters, slot list."""
    row = conn.execute(
        text(
            """
            SELECT r.week, r.player_ids, r.raw->'starters' AS starters, r.owner_name,
                   r.ingested_at, l.roster_positions
            FROM fantasy_rosters r JOIN fantasy_leagues l ON l.id = r.league_id
            WHERE r.league_id = :l AND r.roster_id = :r
            ORDER BY r.week DESC LIMIT 1
            """
        ),
        {"l": league_id, "r": roster_id},
    ).one_or_none()
    if row is None:
        raise ValueError(f"no imported roster {roster_id} in league {league_id}")
    return dict(row._mapping)


def roster_checked_at(conn: Connection, league_id: int, week: int) -> datetime | None:
    """When rosters for this league and week were last fetched from Sleeper successfully
    (fantasy_rosters.ingested_at moves only when a roster changes)."""
    found: datetime | None = conn.execute(
        text(
            "SELECT fetched_at FROM league_fetches"
            " WHERE league_id = :l AND kind = 'rosters' AND week = :w AND ok"
        ),
        {"l": league_id, "w": week},
    ).scalar()
    return found


def sleeper_to_gsis(conn: Connection, sleeper_ids: list[str]) -> dict[str, str]:
    rows = conn.execute(
        text(
            "SELECT external_id, player_id FROM player_external_ids"
            " WHERE provider = 'sleeper' AND external_id = ANY(:ids)"
        ),
        {"ids": sleeper_ids},
    )
    return {str(e): str(p) for e, p in rows}


def leagues(conn: Connection) -> list[dict[str, Any]]:
    """Imported leagues with their rosters (owner names stay in the local database)."""
    rows = conn.execute(
        text(
            """
            SELECT l.id, l.name, l.season, l.roster_positions,
                   json_agg(json_build_object('roster_id', r.roster_id, 'owner_name', r.owner_name)
                            ORDER BY r.roster_id) AS rosters
            FROM fantasy_leagues l
            JOIN fantasy_rosters r ON r.league_id = l.id
             AND r.week = (SELECT max(week) FROM fantasy_rosters WHERE league_id = l.id)
            GROUP BY l.id ORDER BY l.id
            """
        )
    )
    return [dict(r._mapping) for r in rows]


def search_players(
    conn: Connection, query: str, position: str | None, limit: int
) -> list[dict[str, Any]]:
    """Directory search: best name matches among fantasy positions, active players first."""
    season, _ = latest_week(conn)
    rows = conn.execute(
        text(
            """
            SELECT p.id, p.full_name AS name, p.position, p.team,
                   EXISTS (SELECT 1 FROM player_week_stats s
                           WHERE s.player_id = p.id AND s.season = :season) AS active
            FROM players p
            WHERE p.position = ANY(:pos)
              AND (:q = '' OR p.search_name % :q OR :q <% p.search_name)
            ORDER BY active DESC,
                     greatest(similarity(p.search_name, :q),
                              word_similarity(:q, p.search_name)) DESC,
                     p.full_name
            LIMIT :n
            """
        ),
        {
            "season": season,
            "q": _normalize(query),
            "n": limit,
            "pos": [position] if position else list(SKILL),
        },  # fmt: skip
    )
    return [dict(r._mapping) for r in rows]


# Statuses that mean "won't play this week" (Sleeper and the NFL weekly report).
OUT_STATUSES = {"Out", "IR", "PUP", "Sus", "Suspended", "NA", "DNR"}
WARN_STATUSES = {"Doubtful", "Questionable"}


def availability(
    conn: Connection, player_ids: list[str], season: int, week: int
) -> dict[str, dict[str, Any]]:
    """Why a player may not play `week`: bye, or his latest observed injury status.

    Only players with something to report are returned. `playable` False means project 0.
    Sleeper's latest status applies to any week; an nflverse report counts only for its
    own week. Byes are only judged once that week's schedule exists.
    """
    out: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        text(
            """
            WITH playing AS (
                SELECT home_team AS team FROM schedules
                WHERE season = :s AND week = :w AND game_type = 'REG'
                UNION SELECT away_team FROM schedules
                WHERE season = :s AND week = :w AND game_type = 'REG'
            )
            SELECT p.id, p.team FROM players p
            WHERE p.id = ANY(:ids) AND EXISTS (SELECT 1 FROM playing)
              AND p.team IS NOT NULL AND p.team NOT IN (SELECT team FROM playing)
            """
        ),
        {"s": season, "w": week, "ids": player_ids},
    )
    for pid, team in rows:
        out[pid] = {
            "status": "bye",
            "playable": False,
            "note": f"{team} has no game in week {week}",
        }

    snaps = conn.execute(
        text(
            """
            SELECT DISTINCT ON (player_id, source) player_id, source, status, ingested_at
            FROM injury_snapshots
            WHERE player_id = ANY(:ids)
              AND (source = 'sleeper' OR (season = :s AND week = :w))
            ORDER BY player_id, source, ingested_at DESC, id DESC
            """
        ),
        {"s": season, "w": week, "ids": player_ids},
    )
    for pid, source, status, seen in snaps:
        # Keep the worse of the two sources; a bye or an Out already decides it.
        if not status or (pid in out and not out[pid]["playable"]):
            continue
        if status in OUT_STATUSES or status in WARN_STATUSES:
            current = out.get(pid)
            playable = status not in OUT_STATUSES
            if current is None or (current["playable"] and not playable):
                out[pid] = {"status": status, "playable": playable, "source": source,
                            "observed_at": seen}  # fmt: skip
    return out
