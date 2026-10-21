"""`fantasy-rag demo record`: build data/demo/demo.json for DEMO_MODE.

Free by default. Real NFL players' stats, projections and news come from the dev
database; the league is a synthetic "Demo League" (no Sleeper IDs, owners or names from
real leagues). Chat answers are reused from a saved Phase 6 evaluation report: the
validated answer and its cited sources exactly as produced, with progress lines rebuilt
by the same templates the live API uses (their timing is approximate). Questions with
no saved answer can be recorded live only with `--record-question` and a spending cap.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import Connection, text

from fantasy_rag.agent import tools
from fantasy_rag.agent.answer import Answer
from fantasy_rag.agent.router import WITHHELD_MESSAGE, Progress, ToolCall, Turn, tool_progress
from fantasy_rag.analytics import queries as q
from fantasy_rag.analytics.optimizer import Candidate, optimize
from fantasy_rag.api.chat import sources
from fantasy_rag.config import REPO_ROOT

OUT = REPO_ROOT / "data" / "demo" / "demo.json"
LABEL = "Sample data from week 7, 2026, not live"
DEMO_LEAGUE = 900
SAVED = {"s05": None, "n03": None, "a08": None, "a13": None, "o02": None}  # league-free
PLAYERS = [  # (name, position) of real players shown in the demo
    ("Josh Allen", "QB"), ("Bijan Robinson", "RB"), ("Jaylen Warren", "RB"),
    ("Bucky Irving", "RB"), ("Ja'Marr Chase", "WR"), ("A.J. Brown", "WR"),
    ("Puka Nacua", "WR"), ("Justin Jefferson", "WR"), ("Trey McBride", "TE"),
    ("Jahmyr Gibbs", "RB"), ("Brock Purdy", "QB"), ("Tyler Loop", "K"), ("CeeDee Lamb", "WR"),
]  # fmt: skip
ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K"] + ["BN"] * 5
DEMO_ROSTER = [
    "Josh Allen",
    "Brock Purdy",
    "Bijan Robinson",
    "Jaylen Warren",
    "Bucky Irving",
    "Ja'Marr Chase",
    "A.J. Brown",
    "Puka Nacua",
    "Trey McBride",
    "Tyler Loop",
]
START_SIT_PAIRS = [("Jaylen Warren", "Bucky Irving"), ("A.J. Brown", "Puka Nacua")]


def _events_from_turn(turn: Turn, latency_ms: int) -> list[dict[str, Any]]:
    """Progress lines from the live templates, then the recorded validated answer."""
    steps = [Progress("context", "Reading the season and your league context")]
    steps += [tool_progress(c) for c in turn.calls]
    steps += [Progress("writing", "Writing the answer"),
              Progress("checking", "Checking every number and source")]  # fmt: skip
    if turn.first_problems:
        steps += [Progress("repairing", "Fixing a failed check"),
                  Progress("checking", "Checking every number and source")]  # fmt: skip
    gap = max(150, latency_ms // (len(steps) + 1))
    out = []
    for i, p in enumerate(steps):
        data: dict[str, Any] = {"stage": p.stage, "message": p.message, "elapsed_ms": gap * i}
        if p.tool:
            data |= {"tool": p.tool, "ok": p.ok}
        out.append({"event": "progress", "at_ms": gap * i, "data": data})
    end = gap * len(steps)
    status = "repaired" if turn.first_problems else "ok"
    out.append({"event": "answer", "at_ms": end, "data": {
        "status": status, "answer": turn.answer.model_dump(), "sources": sources(turn),
        "as_of": {"season": 2026, "next_week": 7}}})  # fmt: skip
    out.append({"event": "done", "at_ms": end, "data": {
        "query_id": None, "logged": False, "progress_first_ms": 0, "answer_ms": end}})  # fmt: skip
    return out


def _from_report(path: Path, ids: list[str]) -> list[dict[str, Any]]:
    rows = {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))["rows"]}
    chats = []
    for qid in ids:
        r = rows[qid]
        if r["problems"]:
            raise ValueError(f"{qid} was withheld in that run; pick another")
        calls = [ToolCall(c["result_id"], c["tool"], c["args"], c["result"], c["error"])
                 for c in r["calls"]]  # fmt: skip
        turn = Turn(r["question"], calls, Answer.model_validate(r["answer"]),
                    r["first_draft_problems"], [], None, False, r["latency_ms"])  # fmt: skip
        chats.append({"question": r["question"], "recorded_from": f"{path.name}#{qid}",
                      "events": _events_from_turn(turn, r["latency_ms"])})  # fmt: skip
    return chats


def _scenarios() -> list[dict[str, Any]]:
    """Synthetic, labelled paths the browser tests need; not real answers."""
    progress = {"event": "progress", "at_ms": 0, "data": {
        "stage": "context", "message": "Reading the season and your league context",
        "elapsed_ms": 0}}  # fmt: skip
    done = {"event": "done", "at_ms": 400, "data": {
        "query_id": None, "logged": False, "progress_first_ms": 0, "answer_ms": None}}  # fmt: skip
    return [
        {"question": "Demo scenario: an answer that fails verification", "scenario": True,
         "events": [progress, {"event": "withheld", "at_ms": 400, "data": {
             "message": WITHHELD_MESSAGE, "reason": "verification_failed"}}, done]},
        {"question": "Demo scenario: the answer service is down", "scenario": True,
         "events": [progress, {"event": "error", "at_ms": 400, "data": {
             "code": "llm_unavailable", "retryable": True,
             "message": "The answer service is unavailable right now. Stats, projections and"
                        " lineups still work."}}, done]},
    ]  # fmt: skip


def _refs(conn: Connection) -> dict[str, q.PlayerRef]:
    return {name: q.resolve_player(conn, name, pos) for name, pos in PLAYERS}


def build(conn: Connection, report: Path, extra_chats: list[dict[str, Any]]) -> dict[str, Any]:
    refs = _refs(conn)
    season, week = q.latest_week(conn)
    half = tools.ScoringChoice(scoring="half")
    window = tools.StatsWindow(season=season, scoring="half")
    roster = [refs[n] for n in DEMO_ROSTER]
    ranked = tools.rank_start_sit(conn, roster, half)["ranked"]
    proj = {p["player"]["id"]: p for p in ranked}
    # As the live lineup tool does: a player on bye or ruled out is never started.
    playable = [r for r in roster if (proj[r.id]["availability"] or {}).get("playable", True)]
    lineup = optimize(ROSTER_POSITIONS, [
        Candidate(r.id, r.position or "", proj[r.id]["projected_points"] or 0.0) for r in playable
    ])  # fmt: skip
    names = {r.id: r for r in roster}

    def slot_player(pid: str | None) -> dict[str, Any] | None:
        if pid is None:
            return None
        return {**names[pid].model_dump(), "projected_points": proj[pid]["projected_points"],
                "availability": proj[pid]["availability"]}  # fmt: skip

    starters = {pid for _, pid, _ in lineup.slots if pid}
    now = datetime.now(UTC)
    sample_as_of = {"status": "sample", "note": "Sample data: availability is illustrative.",
                    "reasons": [], "missing_weeks": [], "applied": 0}  # fmt: skip
    waiver_refs = [refs["Justin Jefferson"], refs["Jahmyr Gibbs"], refs["CeeDee Lamb"]]
    waiver_proj = tools.rank_start_sit(conn, waiver_refs, half)["ranked"]
    data = {
        "label": LABEL,
        "recorded_at": now.isoformat(),
        "status": {
            "label": LABEL,
            "season": season,
            "latest_week": week,
            "next_week": week + 1,
            "sources": {
                k: {"last_ok": (now - timedelta(hours=h)).isoformat(), "age_hours": h}
                for k, h in [
                    ("stats", 6.0),
                    ("injuries", 6.0),
                    ("news", 1.0),
                    ("rosters", 6.0),
                    ("transactions", 1.0),
                ]
            },
        },  # fmt: skip
        "freshness": [{"source": "sample data", "last_ok": now.isoformat(), "age_hours": 0.0}],
        "players": [{**r.model_dump(), "active": True} for r in refs.values()],
        "player": {r.id: tools._player_block(conn, r, window) for r in refs.values()},
        "start_sit": {
            ",".join(sorted([refs[a].id, refs[b].id])): tools.rank_start_sit(
                conn, [refs[a], refs[b]], half
            )
            for a, b in START_SIT_PAIRS
        },
        "trending": {"add": tools.get_trending(conn, tools.GetTrending(kind="add", limit=10))},
        "leagues": [
            {
                "id": DEMO_LEAGUE,
                "name": "Demo League (sample data)",
                "season": season,
                "roster_positions": ROSTER_POSITIONS,
                "rosters": [{"roster_id": 1, "owner_name": "Demo team"}],
            }
        ],  # fmt: skip
        "lineup": {
            f"{DEMO_LEAGUE}:1": {
                "league_id": DEMO_LEAGUE,
                "roster_id": 1,
                "week": week + 1,
                "scoring": "Demo League rules (half PPR)",
                "label": LABEL,
                "rosters_as_of": {"week": week + 1, "synced_at": now.isoformat()},
                "recommended": [
                    {"slot": s, "player": slot_player(pid), "current": slot_player(pid)}
                    for s, pid, _ in lineup.slots
                ],
                "bench": [slot_player(r.id) for r in roster if r.id not in starters],
                "changes": {"start": [], "sit": []},
                "recommended_total": round(lineup.total, 2),
                "current_total": round(lineup.total, 2),
                "gain": 0.0,
                "unhandled_slots": lineup.unhandled_slots,
                "no_projection": [],
                "unscored_rules": [],
                "unavailable": [
                    {"name": r.name, **proj[r.id]["availability"]}
                    for r in roster
                    if r not in playable
                ],
                "model_version": ranked[0]["model_version"],
            }
        },  # fmt: skip
        "waivers": {
            f"{DEMO_LEAGUE}:ALL": {
                "league_id": DEMO_LEAGUE,
                "scoring": "Demo League rules (half PPR)",
                "week": week + 1,
                "label": LABEL,
                "rosters_as_of": {"week": week + 1, "synced_at": now.isoformat()},
                "availability_as_of": sample_as_of,
                "rostered_players_excluded": len(DEMO_ROSTER),
                "candidates": [
                    {
                        **p["player"],
                        "projected_points": p["projected_points"],
                        "games_used": p["games_used"],
                        "availability": p["availability"],
                        "unscored_rules": [],
                    }
                    for p in waiver_proj
                ],
                "model_version": ranked[0]["model_version"],
            }
        },  # fmt: skip
        "model_runs": [
            dict(r._mapping)
            for r in conn.execute(
                text(
                    "SELECT model_version, training_window, features, metrics, created_at"
                    " FROM model_runs ORDER BY created_at DESC"
                )
            )
        ],  # fmt: skip
        "chat": _from_report(report, list(SAVED)) + extra_chats + _scenarios(),
    }
    encoded: dict[str, Any] = jsonable_encoder(data)
    return encoded


def write(data: dict[str, Any], out: Path = OUT) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return out
